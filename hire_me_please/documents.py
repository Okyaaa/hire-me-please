"""Render tailored Markdown to PDF with headless Chromium (no extra system libraries)."""
from __future__ import annotations

import re
from pathlib import Path

import markdown
from playwright.sync_api import sync_playwright

from .config import OUTPUT_DIR

CSS = """
@page { size: Letter; margin: 0.55in 0.6in; }
body { font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; font-size: 10.5pt; line-height: 1.35; color: #111; }
h1 { font-size: 20pt; margin: 0 0 2pt; }
h2 { font-size: 11.5pt; text-transform: uppercase; letter-spacing: .06em; border-bottom: 1px solid #999;
     margin: 12pt 0 4pt; padding-bottom: 2pt; }
h3 { font-size: 10.5pt; margin: 8pt 0 2pt; }
p { margin: 3pt 0; }
ul { margin: 2pt 0 4pt 16pt; padding: 0; }
li { margin: 1pt 0; }
a { color: #111; text-decoration: none; }
.letter { font-size: 11pt; line-height: 1.5; }
.letter p { margin: 0 0 9pt; }
"""


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def job_dir(job) -> Path:
    d = OUTPUT_DIR / f"{slug(job['company'])}-{slug(job['title'])}-{job['id'].rsplit(':', 1)[-1][:12]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def to_html(md: str, letter: bool = False) -> str:
    body = markdown.markdown(md, extensions=["extra", "sane_lists"])
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head>" \
           f"<body class='{'letter' if letter else ''}'>{body}</body></html>"


def render(docs: list[tuple[str, Path, bool]]) -> None:
    """docs: (markdown, pdf_path, is_letter)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        for md, path, letter in docs:
            path.with_suffix(".md").write_text(md)
            page.set_content(to_html(md, letter), wait_until="load")
            page.pdf(path=str(path), print_background=True, prefer_css_page_size=True)
        browser.close()
