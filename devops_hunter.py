#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
devops_hunter_auto.py — zero-argument DevOps scraper + HTML report

Run once:
  pip install aiohttp feedparser beautifulsoup4

Run:
  python devops_hunter_auto.py
Outputs in ./data:
  - github_results_devops.json
  - blog_results_devops.json
  - job_results_devops.json
  - devops_combined.json
  - devops_report.html

Notes:
- "All companies" cannot be guaranteed on the public web.
  This script discovers MANY companies automatically by:
    * Crawling boards.greenhouse.io to discover company slugs, then using the Greenhouse API
    * Using Lever public search pages for devops/SRE/platform roles
  It also pulls DevOps/SRE blog posts via tag feeds and shows top GitHub repos.
"""

import os, sys, json, asyncio, logging, html, time, re
from typing import Any, Dict, List, Optional, Set
from datetime import datetime, timedelta

import aiohttp
import feedparser
from bs4 import BeautifulSoup

# ---------------------------- Config (no args needed) -------------------------

OUT_DIR = "./data"
DEVOPS_KEYWORDS = [
    "devops", "sre", "site reliability", "platform engineering",
    "kubernetes", "k8s", "terraform", "ansible", "helm",
    "prometheus", "grafana", "observability", "otel", "on-call",
    "cicd", "ci/cd", "pipeline", "aws", "gcp", "azure"
]

# Crawl limiters so a single run completes quickly
GREENHOUSE_MAX_PAGES = 8      # pages to crawl on boards.greenhouse.io (sane cap)
GREENHOUSE_MAX_SLUGS = 120    # validate at most this many slugs
LEVER_MAX_PAGES_PER_QUERY = 1 # keep tight for speed
GITHUB_PER_TERM = 8           # per search term

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=120, connect=20, sock_read=60)

# ---------------------------- Logging ----------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("devops_hunter_auto")

def ts() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def mkdir_p(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def save_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ---------------------------- HTTP helpers -----------------------------------

async def fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    try:
        async with session.get(url) as r:
            if r.status == 200:
                return await r.text()
            return ""
    except Exception:
        return ""

async def fetch_json(session: aiohttp.ClientSession, url: str) -> Dict[str, Any]:
    try:
        async with session.get(url) as r:
            if r.status == 200:
                return await r.json()
            return {}
    except Exception:
        return {}

# ---------------------------- GitHub -----------------------------------------

async def github_repos(session: aiohttp.ClientSession) -> List[Dict]:
    terms = [
        "awesome+devops",
        "awesome+sre",
        "platform+engineering",
        "site+reliability+engineering",
        "kubernetes+production+best+practices",
        "terraform+modules",
        "cicd+best+practices",
    ]
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "DevOpsHunter/auto"
    }
    results: List[Dict] = []
    for term in terms:
        url = ("https://api.github.com/search/repositories"
               f"?q={term}+in:name,description,readme&sort=stars&order=desc")
        try:
            async with session.get(url, headers=headers, timeout=HTTP_TIMEOUT) as r:
                if r.status != 200:
                    continue
                data = await r.json()
        except Exception:
            continue
        for repo in data.get("items", [])[:GITHUB_PER_TERM]:
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
        await asyncio.sleep(1.0)
    # Dedup + sort
    seen, unique = set(), []
    for r in results:
        if r["url"] not in seen:
            seen.add(r["url"])
            unique.append(r)
    unique.sort(key=lambda x: x.get("stars", 0), reverse=True)
    return unique

# ---------------------------- Blogs / Feeds ----------------------------------

def dev_tag_feeds() -> List[str]:
    feeds = []
    for kw in DEVOPS_KEYWORDS:
        tag = kw.replace(" ", "")
        feeds.append(f"https://dev.to/feed/tag/{tag}")
        feeds.append(f"https://medium.com/feed/tag/{tag}")
    # CNCF & ecosystem staples (stable)
    feeds += [
        "https://kubernetes.io/feed.xml",
        "https://kubernetes.io/blog/index.xml",
        "https://prometheus.io/blog/index.xml",
        "https://grafana.com/blog/index.xml",
        "https://www.cncf.io/feed/",
        "https://www.hashicorp.com/blog/feed.xml",
        "https://about.gitlab.com/atom.xml",
        "https://circleci.com/blog/index.xml",
    ]
    # Dedup
    return sorted(set(feeds))

def blog_posts() -> List[Dict]:
    feeds = dev_tag_feeds()
    posts: List[Dict] = []
    for url in feeds:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:15]:
                title = getattr(e, "title", "") or ""
                link = getattr(e, "link", "") or ""
                desc = getattr(e, "summary", "") or getattr(e, "description", "") or ""
                pub = getattr(e, "published", "") or getattr(e, "updated", "") or ""
                if not link or not title:
                    continue
                text = f"{title} {desc}".lower()
                score = sum(1 for k in DEVOPS_KEYWORDS if k in text)
                if score == 0:
                    continue
                excerpt = BeautifulSoup(desc, "html.parser").get_text(" ", strip=True)[:600]
                posts.append({
                    "title": title,
                    "url": link,
                    "source": url.split('/')[2],
                    "published": pub,
                    "excerpt": excerpt,
                    "relevance": score,
                    "type": "blog_post"
                })
        except Exception:
            continue
    # Dedup and sort
    seen, unique = set(), []
    for p in posts:
        if p["url"] not in seen:
            seen.add(p["url"])
            unique.append(p)
    def parse_dt(s: str):
        try:
            tup = feedparser._parse_date(s)
            if tup: return datetime(*tup[:6])
        except Exception:
            pass
    # Sort by relevance then date desc
    unique.sort(key=lambda x: (x.get("relevance", 0), x.get("published", "")), reverse=True)
    return unique

# ---------------------------- Jobs (Lever + Greenhouse) ----------------------

async def lever_jobs(session: aiohttp.ClientSession) -> List[Dict]:
    base = "https://jobs.lever.co/search?commit=1&query="
    queries = ["devops", "site%20reliability", "platform%20engineer", "sre"]
    out: List[Dict] = []
    for q in queries:
        for page in range(1, LEVER_MAX_PAGES_PER_QUERY+1):
            url = f"{base}{q}&page={page}"
            html_txt = await fetch_text(session, url)
            if not html_txt:
                continue
            soup = BeautifulSoup(html_txt, "html.parser")
            cards = soup.select("div.posting")
            if not cards:
                break
            for post in cards:
                t = post.select_one("h5")
                link = post.select_one("a.posting-btn-submit")
                company = post.select_one("div.posting-company")
                loc = post.select_one("span.location")
                title = t.get_text(strip=True) if t else ""
                if not title or not link:
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
            await asyncio.sleep(0.5)
    # Dedup
    seen, unique = set(), []
    for j in out:
        key = (j.get("company",""), j.get("title",""), j.get("url",""))
        if key not in seen:
            seen.add(key)
            unique.append(j)
    return unique

def looks_like_slug(path: str) -> bool:
    # Heuristic for greenhouse slugs (exclude obvious non-board paths)
    return (len(path) > 1 and
            not any(part in path for part in ["privacy", "terms", "blog", "post", "job", "login", "search", "about", "help"])
           )

async def discover_greenhouse_slugs(session: aiohttp.ClientSession, max_pages: int = GREENHOUSE_MAX_PAGES) -> List[str]:
    slugs: List[str] = []
    base = "https://boards.greenhouse.io/"
    # Crawl the homepage and a few paginated pages if present
    for i in range(max_pages):
        url = base if i == 0 else f"{base}?page={i+1}"
        html_txt = await fetch_text(session, url)
        if not html_txt:
            break
        soup = BeautifulSoup(html_txt, "html.parser")
        for a in soup.select('a[href^="/"]'):
            href = a.get("href","").strip()
            if not href.startswith("/") or href == "/":
                continue
            slug = href.lstrip("/").split("/")[0]
            if looks_like_slug(slug):
                slugs.append(slug)
        await asyncio.sleep(0.2)
    # Make unique
    uniq = []
    seen: Set[str] = set()
    for s in slugs:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq[:GREENHOUSE_MAX_SLUGS]

async def greenhouse_jobs(session: aiohttp.ClientSession) -> List[Dict]:
    slugs = await discover_greenhouse_slugs(session)
    out: List[Dict] = []
    for slug in slugs:
        api = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        data = await fetch_json(session, api)
        jobs = data.get("jobs") or []
        for j in jobs:
            title = j.get("title") or ""
            desc = j.get("content","") or ""
            if not title:
                continue
            text = (title + " " + desc).lower()
            if not any(k in text for k in DEVOPS_KEYWORDS):
                continue
            loc = (j.get("location", {}) or {}).get("name", "")
            url = j.get("absolute_url", "")
            out.append({
                "title": title,
                "company": slug,
                "locations": [loc] if loc else [],
                "url": url,
                "source": "greenhouse",
                "date": datetime.utcnow().strftime("%Y-%m-%d"),
                "type": "job_listing"
            })
        await asyncio.sleep(0.15)
    # Dedup
    seen, unique = set(), []
    for j in out:
        key = (j.get("company",""), j.get("title",""), j.get("url",""))
        if key not in seen:
            seen.add(key)
            unique.append(j)
    return unique

async def job_listings() -> List[Dict]:
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        lever, gh = await asyncio.gather(lever_jobs(session), greenhouse_jobs(session))
    # Combine + filter by recency (tagged with today's date)
    items = lever + gh
    cutoff = datetime.utcnow() - timedelta(days=60)
    fresh = []
    for j in items:
        try:
            d = datetime.strptime(j.get("date",""), "%Y-%m-%d")
        except Exception:
            d = datetime.utcnow()
        if d >= cutoff:
            fresh.append(j)
    # Sort by company,title
    fresh.sort(key=lambda x: (x.get("company",""), x.get("title","")))
    return fresh

# ---------------------------- HTML Report ------------------------------------

def html_head() -> str:
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
</style></head><body><div class="container">"""

def html_section(title: str, items_html: str, count: int) -> str:
    return f"""
<div class="header"><h2>{html.escape(title)}</h2><span class="badge">{count}</span></div>
<div class="grid">{items_html}</div>"""

def repo_card(r: Dict) -> str:
    topics = "".join(f'<span class="chip">{html.escape(t)}</span>' for t in (r.get("topics") or [])[:5])
    desc = html.escape(r.get("description","")[:200])
    return f"""
<div class="card">
  <div><a href="{html.escape(r.get('url',''))}" target="_blank"><strong>{html.escape(r.get('name',''))}</strong></a></div>
  <div class="meta">★ {r.get('stars',0)} • {html.escape(r.get('language') or '')}</div>
  <div class="small">{desc}</div>
  <div class="kv">{topics}</div>
</div>"""

def blog_card(p: Dict) -> str:
    excerpt = html.escape((p.get("excerpt") or "")[:220])
    pub = p.get("published") or ""
    src = html.escape(p.get("source",""))
    return f"""
<div class="card">
  <div><a href="{html.escape(p.get('url',''))}" target="_blank"><strong>{html.escape(p.get('title',''))}</strong></a></div>
  <div class="meta">{src} • {html.escape(pub)}</div>
  <div class="small">{excerpt}</div>
</div>"""

def job_card(j: Dict) -> str:
    locs = ", ".join(j.get("locations") or [])
    return f"""
<div class="card">
  <div><a href="{html.escape(j.get('url',''))}" target="_blank"><strong>{html.escape(j.get('title',''))}</strong></a></div>
  <div class="meta">{html.escape(j.get('company',''))} • {html.escape(locs)}</div>
  <div class="small">Source: {html.escape(j.get('source',''))} • {html.escape(j.get('date',''))}</div>
</div>"""

def generate_html_report(data: Dict[str, Any], path: str) -> str:
    head = html_head()
    header = f'<h1>DevOps Report</h1><div class="small">Generated at {html.escape(ts())}</div><hr>'
    body = ""
    blogs = data.get("blog_posts", [])
    repos = data.get("github_repos", [])
    jobs  = data.get("job_listings", [])
    
    if blogs:
        items = "".join(blog_card(p) for p in blogs[:40])
        body += html_section("Recent Blog Posts", items, len(blogs))


    if repos:
        items = "".join(repo_card(r) for r in repos[:40])
        body += html_section("Top GitHub Repos (DevOps/SRE/Platform)", items, len(repos))


    if jobs:
        items = "".join(job_card(j) for j in jobs[:40])
        body += html_section("Job Listings", items, len(jobs))

    footer = '<div class="footer">Made with devops_hunter_auto.py</div></div></body></html>'
    doc = head + header + body + footer
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path

# ---------------------------- Orchestration ----------------------------------

async def main() -> None:
    mkdir_p(OUT_DIR)
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        gh_repos = await github_repos(session)
    blogs = blog_posts()
    jobs = await job_listings()

    save_json(os.path.join(OUT_DIR, "github_results_devops.json"), gh_repos)
    save_json(os.path.join(OUT_DIR, "blog_results_devops.json"), blogs)
    save_json(os.path.join(OUT_DIR, "job_results_devops.json"), jobs)

    combined = {"generated_at": ts(),"blog_posts": blogs, "github_repos": gh_repos,  "job_listings": jobs}
    save_json(os.path.join(OUT_DIR, "devops_combined.json"), combined)

    report_path = os.path.join(OUT_DIR, "devops_report.html")
    generate_html_report(combined, report_path)
    print(f"\n✅ Done. Open: {report_path}\n")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted.")
