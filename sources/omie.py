"""OMIE (omie.es) — Iberian market operator (NEMO for ES/PT).

Discovery: the notas-de-prensa listing (server-rendered Drupal; newest two
pages). Items link PDFs directly — no HTML article pages. Press notes are
almost all design-relevant (MTU15, coupling, IDAs); monthly price reports
live in a different section and are never touched.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, extract_pdf_text, slugify, MAX_CONTENT_CHARS

INSTITUTION = "OMIE"
DOCUMENT_TYPE = "MARKET_OPERATOR"

BASE = "https://www.omie.es"
LISTING = f"{BASE}/es/publicaciones/notas-de-prensa"
PAGES = 2
DATE_RE = re.compile(r"(\d{2})/(\d{2})/(20\d{2})")


def discover(session):
    found: dict[str, Candidate] = {}

    for page in range(PAGES):
        try:
            r = get_with_retry(session, LISTING, params={"page": page} if page else None, timeout=45)
        except CollectorError:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        for item in soup.select("div.list-item"):
            link = item.select_one("a.list-item-title[href]")
            if not link:
                continue
            href = link["href"]
            title = " ".join(link.get_text(" ", strip=True).split())
            if not title or not href.lower().endswith(".pdf"):
                continue
            fname = href.split("/")[-1].rsplit(".", 1)[0]
            if re.search(r"[_-]en(?:[_-]|$)", fname.lower()):
                continue  # bilingual duplicate; keep the Spanish original
            date = None
            cat = item.select_one("div.category-date")
            if cat:
                m = DATE_RE.search(cat.get_text(" ", strip=True))
                if m:
                    try:
                        date = datetime.strptime(m.group(0), "%d/%m/%Y").date().isoformat()
                    except ValueError:
                        pass
            url = urljoin(BASE, href)
            sid = f"omie-{slugify(fname)}"
            found.setdefault(sid, Candidate(sid, title, date, url))

    if not found:
        raise CollectorError("OMIE discovery returned no candidates")
    return LISTING, list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    r = get_with_retry(session, candidate.url, timeout=90)
    text = extract_pdf_text(r.content)
    if not text.strip():
        raise CollectorError(f"OMIE PDF has no extractable text: {candidate.url}")
    return (f"{candidate.title}\n\n" + text)[:MAX_CONTENT_CHARS]


def is_out_of_scope(candidate: Candidate) -> bool:
    t = candidate.title.lower()
    return any(x in t for x in ("informe mensual", "informe anual", "nombramiento", "premio"))
