"""RECS International (recs.org) — EAC/GO market association.

Discovery: the WordPress REST `news` custom post type (the default `posts`
feed is spam-compromised — never ingest it) plus the documents catalog
embedded as inline JSON on /documents/. Members-only items are skipped.
"""
from __future__ import annotations

import html as html_mod
import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, extract_pdf_text, html_to_text, slugify, MAX_CONTENT_CHARS

INSTITUTION = "RECS International"
DOCUMENT_TYPE = "MARKET_OPERATOR"

BASE = "https://recs.org"
NEWS_API = f"{BASE}/wp-json/wp/v2/news"
DOCUMENTS = f"{BASE}/documents/"

_CONTENT_CACHE: dict[str, str] = {}


def _date(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace(" ", "T")).date().isoformat()
    except ValueError:
        return None


def discover(session):
    found: dict[str, Candidate] = {}

    r = get_with_retry(session, NEWS_API, params={"per_page": 30}, timeout=45)
    try:
        posts = r.json()
    except ValueError as exc:
        raise CollectorError(f"RECS news API returned invalid JSON: {exc}")
    for post in posts if isinstance(posts, list) else []:
        raw_title = (post.get("title") or {}).get("rendered") or ""
        title = " ".join(BeautifulSoup(html_mod.unescape(raw_title), "html.parser")
                         .get_text(" ", strip=True).split())
        link = post.get("link") or ""
        pid = post.get("id")
        if not title or not link or pid is None:
            continue
        if "hacked by" in title.lower() or "members only" in title.lower():
            continue
        sid = f"recs-news-{pid}"
        _CONTENT_CACHE[sid] = (post.get("content") or {}).get("rendered") or ""
        found.setdefault(sid, Candidate(sid, title, _date(post.get("date_gmt")), link))

    # Documents catalog: inline JSON on the listing page.
    try:
        r = get_with_retry(session, DOCUMENTS, timeout=45)
        m = re.search(r"const items = (\[.*?\]);", r.text, re.S)
        if m:
            for item in json.loads(m.group(1)):
                if item.get("protected"):
                    continue
                title = " ".join(html_mod.unescape(str(item.get("title") or "")).split())
                url = str(item.get("url") or "").replace("http://", "https://")
                if not title or not url.lower().endswith(".pdf"):
                    continue
                sid = f"recs-doc-{slugify(title)}"
                found.setdefault(sid, Candidate(sid, title, _date(item.get("date")), url))
    except (CollectorError, ValueError):
        pass

    if not found:
        raise CollectorError("RECS discovery returned no candidates")
    return f"{NEWS_API} + {DOCUMENTS}", list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    if candidate.url.lower().endswith(".pdf"):
        r = get_with_retry(session, candidate.url, timeout=90)
        text = extract_pdf_text(r.content)
        if not text.strip():
            raise CollectorError(f"PDF has no text layer (needs OCR): {candidate.url}")
        return text

    raw_html = _CONTENT_CACHE.get(candidate.source_id, "")
    if not raw_html:
        r = get_with_retry(session, candidate.url, timeout=45)
        raw_html = r.text
    soup = BeautifulSoup(raw_html, "html.parser")
    return html_to_text(soup)[:MAX_CONTENT_CHARS]
