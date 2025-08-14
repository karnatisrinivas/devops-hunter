#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
devops_hunter.py
A single-file, executable scraper for DevOps content:
- GitHub repos (awesome lists, platform engineering, SRE, CI/CD)
- Blogs/feeds (DevOps/SRE/Kubernetes/Terraform/Observability)
- Job listings (Greenhouse/Lever boards, HTML-friendly sources)
- Optional HTML dashboard report (--html-report)

Usage:
  chmod +x devops_hunter.py
  ./devops_hunter.py                   # scrape everything → ./data/*.json + combined
  ./devops_hunter.py --only blogs      # just blogs
  ./devops_hunter.py --out outdir      # custom output dir
  ./devops_hunter.py --html-report     # also generate devops_report.html
  GITHUB_TOKEN=xxxx ./devops_hunter.py # authenticated GitHub (higher rate limits)

Requires:
  pip install aiohttp feedparser beautifulsoup4
"""

import os
import sys
import json
import argparse
import logging
import asyncio
import aiohttp
import feedparser
import html
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from bs4 import BeautifulSoup

# ---------------------------- CLI / Logging ----------------------------------

def setup_logger(verbose: bool) -> logging.Logger:
    logging.basicConfig(
        level=(logging.DEBUG if verbose else logging.INFO),
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    return logging.getLogger("devops_hunter")

# ---------------------------- Utilities --------------------------------------

def ensure_deps(logger: logging.Logger) -> None:
    missing = []
    try:
        import aiohttp as _  # noqa
    except Exception:
        missing.append("aiohttp")
    try:
        import feedparser as _  # noqa
    except Exception:
        missing.append("feedparser")
    try:
        import bs4 as _  # noqa
    except Exception:
        missing.append("beautifulsoup4")
    if missing:
        logger.warning(
            "Missing dependencies: %s\nInstall with: pip install %s",
            ", ".join(missing), " ".join(missing)
        )

def ts() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def mkdir_p(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def save_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def dt_or_min_rss(s: str) -> datetime:
    # best-effort ordering for feed dates
    try:
        tup = feedparser._parse_date(s)
        if tup:
            return datetime(*tup[:6])
    except Exception:
        pass
    return datetime.min

# ---------------------------- Core Scraper -----------------------------------

class DevOpsHunter:
    def __init__(self, out_dir: str, verbose: bool = False) -> None:
        self.logger = setup_logger(verbose)
        ensure_deps(self.logger)
        self.out_dir = os.path.abspath(out_dir)
        mkdir_p(self.out_dir)

        self.http_timeout = aiohttp.ClientTimeout(total=120, connect=20, sock_read=60)
        self.headers = {
            "Accept": "application/vnd.github.v3+json",
            "Accept-Language": "en-US,en;q=0.8",
            "User-Agent": "DevOpsHunter/1.0 (+https://example.local; bot)",
            "Connection": "keep-alive",
        }
        token = (os.environ.get("GITHUB_TOKEN") or "").strip()
        if token:
            self.headers["Authorization"] = f"token {token}"
            self.logger.info("GitHub token detected; authenticated requests enabled.")
        else:
            self.logger.warning("No GITHUB_TOKEN set. GitHub search rate limits will be low.")

    # ------------------------ GitHub Repos -----------------------------------

    async def _get_json(self, session: aiohttp.ClientSession, url: str, total: int = 60) -> Dict:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=total)) as r:
                if r.status == 200:
                    return await r.json()
                self.logger.debug("HTTP %s for %s", r.status, url)
                return {}
        except Exception as e:
            self.logger.debug("GET failed %s: %s", url, e)
            return {}

    async def github_repos(self, per_term: int = 8) -> List[Dict]:
        terms = [
            "awesome+devops",
            "awesome+sre",
            "platform+engineering",
            "site+reliability+engineering",
            "kubernetes+production+best+practices",
            "terraform+modules",
            "cicd+best+practices",
        ]
        results: List[Dict] = []
        async with aiohttp.ClientSession(headers=self.headers, timeout=self.http_timeout) as session:
            for term in terms:
                url = ("https://api.github.com/search/repositories"
                       f"?q={term}+in:name,description,readme&sort=stars&order=desc")
                data = await self._get_json(session, url, total=60)
                for repo in data.get("items", [])[:per_term]:
                    if repo.get("stargazers_count", 0) < 20:
                        continue
                    results.append({
                        "name": repo.get("full_name"),
                        "url": repo.get("html_url"),
                        "description": repo.get("description") or "",
                        "stars": repo.get("stargazers_count", 0),
                        "language": repo.get("language") or "",
                        "topics": repo.get("topics", []),
                        "last_updated": repo.get("updated_at", ""),
                        "source": "github",
                        "term": term
                    })
                await asyncio.sleep(1.2)

        # Dedup by URL + sort
        seen, unique = set(), []
        for r in results:
            if r["url"] not in seen:
                seen.add(r["url"])
                unique.append(r)
        unique.sort(key=lambda x: x.get("stars", 0), reverse=True)
        out_path = os.path.join(self.out_dir, "github_results_devops.json")
        save_json(out_path, unique)
        self.logger.info("GitHub repos: %d → %s", len(unique), out_path)
        return unique

    # ------------------------ Blogs/Feeds ------------------------------------

    def blog_posts(self, max_per_feed: int = 20) -> List[Dict]:
        feeds = [
            # DevOps & SRE
            "https://dev.to/feed/tag/devops",
            "https://dev.to/feed/tag/sre",
            "https://martinfowler.com/feed.atom",
            "https://www.gojko.net/feed.xml",
            # Kubernetes / CNCF / Observability
            "https://kubernetes.io/feed.xml",
            "https://kubernetes.io/blog/index.xml",
            "https://prometheus.io/blog/index.xml",
            "https://grafana.com/blog/index.xml",
            "https://www.cncf.io/feed/",
            # IaC / CI-CD
            "https://www.hashicorp.com/blog/feed.xml",
            "https://about.gitlab.com/atom.xml",
            "https://circleci.com/blog/index.xml",
            # Platform engineering
            "https://platformengineering.org/rss.xml",
        ]
        keywords = [
            "devops", "sre", "site reliability", "platform engineering",
            "observability", "kubernetes", "k8s", "helm",
            "terraform", "ansible", "pulumi", "cicd", "ci/cd",
            "prometheus", "grafana", "tracing", "otel", "on-call",
            "resilience", "scalability", "cost optimization"
        ]

        posts: List[Dict] = []
        for url in feeds:
            try:
                feed = feedparser.parse(url)
                for e in feed.entries[:max_per_feed]:
                    title = getattr(e, "title", "") or ""
                    link = getattr(e, "link", "") or ""
                    desc = getattr(e, "summary", "") or getattr(e, "description", "") or ""
                    published = getattr(e, "published", "") or getattr(e, "updated", "") or ""
                    text = f"{title} {desc}".lower()
                    score = sum(1 for k in keywords if k in text)
                    if score == 0 or not link:
                        continue
                    clean = BeautifulSoup(desc, "html.parser").get_text(" ", strip=True)[:600]
                    posts.append({
                        "title": title,
                        "url": link,
                        "source": url.split("/")[2],
                        "published": published,
                        "excerpt": clean,
                        "relevance": score,
                        "type": "blog_post"
                    })
            except Exception as e:
                self.logger.debug("Feed error %s: %s", url, e)

        # Dedup by URL + sort
        seen, unique = set(), []
        for p in posts:
            if p["url"] not in seen:
                seen.add(p["url"])
                unique.append(p)
        unique.sort(key=lambda x: (x.get("relevance", 0), dt_or_min_rss(x.get("published", ""))), reverse=True)

        out_path = os.path.join(self.out_dir, "blog_results_devops.json")
        save_json(out_path, unique)
        self.logger.info("Blog posts: %d → %s", len(unique), out_path)
        return unique

    # ------------------------ Jobs ------------------------------------------

    def _devops_match(self, title: str, desc: str = "") -> bool:
        t = (title or "").lower()
        d = (desc or "").lower()
        title_terms = {
            "devops", "site reliability", "sre", "platform engineer",
            "platform engineering", "infrastructure engineer",
            "systems engineer", "cloud engineer", "build engineer",
            "release engineer"
        }
        desc_terms = {
            "kubernetes", "k8s", "terraform", "ansible", "helm",
            "prometheus", "grafana", "observability", "otel",
            "on-call", "incident", "cicd", "ci/cd", "pipeline",
            "aws", "gcp", "azure", "slo", "sla", "sli"
        }
        return any(k in t for k in title_terms) or any(k in d for k in desc_terms)

    async def _jobs_greenhouse_board(self, session: aiohttp.ClientSession, api_url: str) -> List[Dict]:
        try:
            async with session.get(api_url, headers=self.headers) as r:
                if r.status != 200:
                    return []
                data = await r.json()
                company = api_url.split("/boards/")[1].split("/")[0] if "/boards/" in api_url else "Unknown"
                out = []
                for j in data.get("jobs", []):
                    title = j.get("title") or ""
                    loc = (j.get("location", {}) or {}).get("name", "")
                    url = j.get("absolute_url", "")
                    desc = j.get("content", "") or ""
                    if not title or not url:
                        continue
                    if not self._devops_match(title, desc):
                        continue
                    out.append({
                        "title": title, "company": company,
                        "locations": [loc] if loc else [],
                        "url": url, "source": "greenhouse",
                        "date": datetime.utcnow().strftime("%Y-%m-%d"),
                        "type": "job_listing"
                    })
                return out
        except Exception:
            return []

    async def _jobs_lever_search(self, session: aiohttp.ClientSession, url: str) -> List[Dict]:
        # HTML parse (generic search page, public)
        try:
            async with session.get(url, headers=self.headers) as r:
                if r.status != 200:
                    return []
                html_txt = await r.text()
                soup = BeautifulSoup(html_txt, "html.parser")
                out = []
                for post in soup.select("div.posting"):
                    t = post.select_one("h5")
                    link = post.select_one("a.posting-btn-submit")
                    company = post.select_one("div.posting-company")
                    loc = post.select_one("span.location")
                    title = t.get_text(strip=True) if t else ""
                    if not title or not link:
                        continue
                    if not self._devops_match(title, ""):
                        continue
                    out.append({
                        "title": title,
                        "company": (company.get_text(strip=True) if company else ""),
                        "locations": [loc.get_text(strip=True)] if loc else [],
                        "url": link.get("href", ""),
                        "source": "lever",
                        "date": datetime.utcnow().strftime("%Y-%m-%d"),
                        "type": "job_listing"
                    })
                return out
        except Exception:
            return []

    async def jobs(self) -> List[Dict]:
        boards = [
            "https://boards-api.greenhouse.io/v1/boards/datadog/jobs?content=true",
            "https://boards-api.greenhouse.io/v1/boards/hashicorp/jobs?content=true",
            "https://boards-api.greenhouse.io/v1/boards/cloudflare/jobs?content=true",
        ]
        lever_searches = [
            "https://jobs.lever.co/search?commit=1&query=devops",
            "https://jobs.lever.co/search?commit=1&query=site%20reliability",
            "https://jobs.lever.co/search?commit=1&query=platform%20engineer",
        ]

        items: List[Dict] = []
        async with aiohttp.ClientSession(timeout=self.http_timeout) as session:
            tasks = [self._jobs_greenhouse_board(session, u) for u in boards]
            tasks += [self._jobs_lever_search(session, u) for u in lever_searches]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, list):
                items.extend(r)

        # Dedup by (company, title, url)
        seen, unique = set(), []
        for j in items:
            key = (j.get("company", ""), j.get("title", ""), j.get("url", ""))
            if key not in seen:
                seen.add(key)
                unique.append(j)

        # Keep last ~60 days (tagged with today)
        cutoff = datetime.utcnow() - timedelta(days=60)
        fresh = []
        for j in unique:
            try:
                d = datetime.strptime(j.get("date", ""), "%Y-%m-%d")
            except Exception:
                d = datetime.utcnow()
            if d >= cutoff:
                fresh.append(j)

        fresh.sort(key=lambda x: (x.get("company",""), x.get("title","")))
        out_path = os.path.join(self.out_dir, "job_results_devops.json")
        save_json(out_path, fresh)
        self.logger.info("Job listings: %d → %s", len(fresh), out_path)
        return fresh

    # ------------------------ HTML Report ------------------------------------

    def _html_head(self) -> str:
        return """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>DevOps Report</title>
<style>
:root{--bg:#0b1220;--card:#121a2b;--muted:#9fb0d0;--text:#eaf0ff;--accent:#7aa2ff;--chip:#1b2742}
*{box-sizing:border-box} body{margin:0;padding:24px;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,Ubuntu}
h1{font-size:28px;margin:0 0 16px} h2{font-size:20px;margin:24px 0 12px}
.container{max-width:1100px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.card{background:var(--card);border-radius:14px;padding:16px;box-shadow:0 3px 10px rgba(0,0,0,.25)}
a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
.meta{color:var(--muted);font-size:12px;margin-top:6px}
.kv{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.chip{background:var(--chip);border-radius:999px;padding:2px 8px;font-size:12px;color:var(--muted)}
.header{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:16px}
.badge{background:#1f2a48;color:#bcd3ff;border:1px solid #2a3a69;border-radius:999px;padding:4px 10px;font-size:12px}
.small{font-size:12px;color:var(--muted)}
hr{border:none;border-top:1px solid #263252;margin:20px 0}
.footer{margin-top:26px;color:var(--muted);font-size:12px}
</style></head><body><div class="container">
"""

    def _html_section(self, title: str, items_html: str, count: int) -> str:
        return f"""
<div class="header"><h2>{html.escape(title)}</h2><span class="badge">{count}</span></div>
<div class="grid">{items_html}</div>
"""

    def _html_repo_card(self, r: Dict) -> str:
        topics = "".join(f'<span class="chip">{html.escape(t)}</span>' for t in (r.get("topics") or [])[:5])
        desc = html.escape(r.get("description","")[:180])
        return f"""
<div class="card">
  <div><a href="{html.escape(r.get('url',''))}" target="_blank"><strong>{html.escape(r.get('name',''))}</strong></a></div>
  <div class="meta">★ {r.get('stars',0)} • {html.escape(r.get('language') or '')}</div>
  <div class="small">{desc}</div>
  <div class="kv">{topics}</div>
</div>"""

    def _html_blog_card(self, p: Dict) -> str:
        excerpt = html.escape((p.get("excerpt") or "")[:200])
        pub = p.get("published") or ""
        src = html.escape(p.get("source",""))
        return f"""
<div class="card">
  <div><a href="{html.escape(p.get('url',''))}" target="_blank"><strong>{html.escape(p.get('title',''))}</strong></a></div>
  <div class="meta">{src} • {html.escape(pub)}</div>
  <div class="small">{excerpt}</div>
</div>"""

    def _html_job_card(self, j: Dict) -> str:
        locs = ", ".join(j.get("locations") or [])
        return f"""
<div class="card">
  <div><a href="{html.escape(j.get('url',''))}" target="_blank"><strong>{html.escape(j.get('title',''))}</strong></a></div>
  <div class="meta">{html.escape(j.get('company',''))} • {html.escape(locs)}</div>
  <div class="small">Source: {html.escape(j.get('source',''))} • {html.escape(j.get('date',''))}</div>
</div>"""

    def generate_html_report(self, data: Dict[str, Any], path: str) -> str:
        head = self._html_head()
        header = f'<h1>DevOps Report</h1><div class="small">Generated at {html.escape(ts())}</div><hr>'
        body = ""

        repos = data.get("github_repos", [])
        blogs = data.get("blog_posts", [])
        jobs  = data.get("job_listings", [])

        if repos:
            items = "".join(self._html_repo_card(r) for r in repos[:40])
            body += self._html_section("Top GitHub Repos (DevOps/SRE/Platform)", items, len(repos))

        if blogs:
            items = "".join(self._html_blog_card(p) for p in blogs[:40])
            body += self._html_section("Recent Blog Posts", items, len(blogs))

        if jobs:
            items = "".join(self._html_job_card(j) for j in jobs[:40])
            body += self._html_section("Job Listings", items, len(jobs))

        footer = '<div class="footer">Made with devops_hunter.py</div></div></body></html>'
        html_doc = head + header + body + footer

        with open(path, "w", encoding="utf-8") as f:
            f.write(html_doc)
        self.logger.info("HTML report → %s", path)
        return path

    # ------------------------ Orchestration ----------------------------------

    async def run(self, only: Optional[str] = None, html_report: bool = False) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if only is None or only == "github":
            out["github_repos"] = await self.github_repos()
        if only is None or only == "blogs":
            out["blog_posts"] = self.blog_posts()
        if only is None or only == "jobs":
            out["job_listings"] = await self.jobs()

        combined_path = os.path.join(self.out_dir, "devops_combined.json")
        save_json(combined_path, {"generated_at": ts(), **out})
        self.logger.info("Combined output → %s", combined_path)

        if html_report:
            report_path = os.path.join(self.out_dir, "devops_report.html")
            self.generate_html_report(out, report_path)

        return out

# ---------------------------- Entrypoint -------------------------------------

def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="DevOps Hunter — single-file DevOps scraper + HTML report")
    p.add_argument("--out", default="./data", help="Output directory (default: ./data)")
    p.add_argument("--only", choices=["github", "blogs", "jobs"], help="Run only one subsystem")
    p.add_argument("--html-report", action="store_true", help="Generate devops_report.html")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return p.parse_args(argv)

def main(argv: List[str]) -> None:
    args = parse_args(argv)
    hunter = DevOpsHunter(out_dir=args.out, verbose=args.verbose)
    try:
        asyncio.run(hunter.run(only=args.only, html_report=args.html_report))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)

if __name__ == "__main__":
    main(sys.argv[1:])

