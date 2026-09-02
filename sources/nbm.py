"""Nordic Balancing Model (nordicbalancingmodel.net) — joint Nordic TSO program.

Discovery: open WordPress REST API — news, publications, consultations and
implementation-guide posts, newest first. Post content arrives rendered
inline; attached PDFs live under /wp-content/uploads/.
"""
from __future__ import annotations

import html as html_mod
import re
from datetime import datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, extract_pdf_text, MAX_CONTENT_CHARS

INSTITUTION = "Nordic TSOs (NBM)"
DOCUMENT_TYPE = "TSO"

API = "https://nordicbalancingmodel.net/wp-json/wp/v2/posts"
CATEGORIES = "2,3,9,45"  # news, publications, consultations, implementation guides
PER_PAGE = 40

_CONTENT_CACHE: dict[str, str] = {}


def _clean_text(s: str) -> str:
    return " ".join(html_mod.unescape(s).replace("​", "").replace(" ", " ").split())


def discover(session):
    r = get_with_retry(session, API, params={
        "per_page": PER_PAGE, "page": 1, "categories": CATEGORIES,
        "_fields": "id,date_gmt,link,title,content",
    }, timeout=45)
    try:
        posts = r.json()
    except ValueError as exc:
        raise CollectorError(f"NBM WP API returned invalid JSON: {exc}")

    found: dict[str, Candidate] = {}
    for post in posts:
        title = _clean_text((post.get("title") or {}).get("rendered") or "")
        link = post.get("link") or ""
        pid = post.get("id")
        if not title or not link or pid is None:
            continue
        date = None
        raw = post.get("date_gmt") or ""
        try:
            date = datetime.fromisoformat(raw).date().isoformat()
        except ValueError:
            pass
        sid = f"nbm-{pid}"
        _CONTENT_CACHE[sid] = (post.get("content") or {}).get("rendered") or ""
        found.setdefault(sid, Candidate(sid, title, date, link))

    if not found:
        raise CollectorError("NBM discovery returned no candidates")
    return API, list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    raw_html = _CONTENT_CACHE.get(candidate.source_id, "")
    if not raw_html:
        r = get_with_retry(session, candidate.url, timeout=45)
        raw_html = r.text

    soup = BeautifulSoup(raw_html, "html.parser")
    text = "\n".join(
        line.strip()
        for line in _clean_text(soup.get_text("\n")).replace(". ", ".\n").splitlines()
        if line.strip()
    )

    pdf_link = soup.select_one('a[href*="/wp-content/uploads/"][href$=".pdf"]')
    if pdf_link:
        try:
            pr = get_with_retry(session, pdf_link["href"], timeout=90)
            pdf_text = extract_pdf_text(pr.content)
            if pdf_text:
                text = (text + "\n\n" + pdf_text)[:MAX_CONTENT_CHARS]
        except CollectorError:
            pass
    return text[:MAX_CONTENT_CHARS]
