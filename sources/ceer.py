"""CEER (ceer.eu) — Council of European Energy Regulators.

Discovery: WordPress REST API — electricity publications (excluding the
high-volume national monitoring reports) and public consultations. Full
documents are PDFs linked from the publication pages.
"""
from __future__ import annotations

from datetime import datetime

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, html_to_text, extract_pdf_text, slugify, MAX_CONTENT_CHARS

INSTITUTION = "CEER"
DOCUMENT_TYPE = "REGULATOR"

API = "https://www.ceer.eu/wp-json/wp/v2"
ELECTRICITY_TOPIC = "26"
NATIONAL_MONITORING = 286  # publication_category to exclude (noise)


def _date(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date().isoformat()
    except ValueError:
        return None


def _collect(session, endpoint, params, prefix):
    out = []
    try:
        r = get_with_retry(session, f"{API}/{endpoint}", params=params, timeout=45)
        posts = r.json()
    except (CollectorError, ValueError):
        return out
    for post in posts if isinstance(posts, list) else []:
        if NATIONAL_MONITORING in (post.get("publication_category") or []):
            continue
        slug = post.get("slug") or ""
        link = post.get("link") or ""
        title = " ".join(BeautifulSoup(
            (post.get("title") or {}).get("rendered") or "", "html.parser"
        ).get_text(" ", strip=True).split())
        if not slug or not link or not title:
            continue
        out.append(Candidate(f"{prefix}-{slugify(slug)}", title, _date(post.get("date_gmt")), link))
    return out


def discover(session):
    found: dict[str, Candidate] = {}
    for c in _collect(session, "publication",
                      {"per_page": 20, "orderby": "date", "order": "desc",
                       "key_topic": ELECTRICITY_TOPIC}, "ceer-publication"):
        found.setdefault(c.source_id, c)
    for c in _collect(session, "public_consultation",
                      {"per_page": 10, "orderby": "date", "order": "desc"}, "ceer-consultation"):
        found.setdefault(c.source_id, c)

    if not found:
        raise CollectorError("CEER discovery returned no candidates")
    return f"{API}/publication (electricity)", list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    r = get_with_retry(session, candidate.url, timeout=45)
    soup = BeautifulSoup(r.text, "html.parser")
    text = html_to_text(soup.find("main") or soup)

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
