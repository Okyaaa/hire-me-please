"""Pull open roles from public ATS job-board APIs (no scraping, no login)."""
from __future__ import annotations

import html
import re

import httpx
from rich.console import Console

console = Console()
TIMEOUT = httpx.Timeout(30.0)


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(html.unescape(s))  # Greenhouse double-escapes content
    s = re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "- ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def greenhouse(client: httpx.Client, slug: str) -> list[dict]:
    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
    r.raise_for_status()
    return [
        {
            "id": f"greenhouse:{slug}:{j['id']}",
            "source": "greenhouse",
            "company": slug,
            "title": j["title"],
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url"),
            # The embed form never redirects to the company's own careers site
            # (the hosted board page does for e.g. Stripe), so the form is always there.
            "apply_url": f"https://job-boards.greenhouse.io/embed/job_app?for={slug}&token={j['id']}",
            "description": html_to_text(j.get("content")),
            "posted_at": j.get("updated_at"),
        }
        for j in r.json().get("jobs", [])
    ]


def lever(client: httpx.Client, slug: str) -> list[dict]:
    r = client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    r.raise_for_status()
    jobs = []
    for j in r.json():
        parts = [j.get("descriptionPlain", "")]
        for section in j.get("lists", []):
            parts.append(f"{section.get('text', '')}\n{html_to_text(section.get('content'))}")
        parts.append(j.get("additionalPlain", ""))
        jobs.append({
            "id": f"lever:{slug}:{j['id']}",
            "source": "lever",
            "company": slug,
            "title": j["text"],
            "location": (j.get("categories") or {}).get("location", ""),
            "url": j.get("hostedUrl"),
            "apply_url": j.get("applyUrl") or f"{j.get('hostedUrl')}/apply",
            "description": "\n\n".join(p for p in parts if p).strip(),
            "posted_at": str(j.get("createdAt", "")),
        })
    return jobs


def ashby(client: httpx.Client, slug: str) -> list[dict]:
    r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"})
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        location = j.get("location", "")
        if j.get("isRemote") and "remote" not in location.lower():
            location = f"{location} (Remote)".strip()
        jobs.append({
            "id": f"ashby:{slug}:{j['id']}",
            "source": "ashby",
            "company": slug,
            "title": j["title"],
            "location": location,
            "url": j.get("jobUrl"),
            "apply_url": j.get("applyUrl") or f"{j.get('jobUrl')}/application",
            "description": j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
            "posted_at": j.get("publishedAt"),
        })
    return jobs


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}


def fetch_all(sources: dict) -> list[dict]:
    jobs: list[dict] = []
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": "hire-me-please/0.1"}, follow_redirects=True) as client:
        for source, slugs in (sources or {}).items():
            fetch = FETCHERS.get(source)
            if not fetch:
                console.print(f"[yellow]Unknown source {source!r}, skipping[/]")
                continue
            for slug in slugs or []:
                try:
                    found = fetch(client, slug)
                    console.print(f"  {source}/{slug}: {len(found)} open roles")
                    jobs.extend(found)
                except httpx.HTTPError as e:
                    console.print(f"  [red]{source}/{slug} failed: {e}[/]")
    return jobs


def matches_search(job: dict, search: dict) -> bool:
    """Cheap keyword prefilter so only plausible roles reach Claude."""
    title = job["title"].lower()
    keywords = [k.lower() for k in search.get("title_keywords", [])]
    excludes = [k.lower() for k in search.get("exclude_title_keywords", [])]
    locations = [loc.lower() for loc in search.get("locations", [])]
    if keywords and not any(k in title for k in keywords):
        return False
    if any(k in title for k in excludes):
        return False
    if locations and not any(loc in (job.get("location") or "").lower() for loc in locations):
        return False
    return True
