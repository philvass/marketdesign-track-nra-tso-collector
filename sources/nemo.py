"""NEMO Committee (nemo-committee.eu) — SDAC/SIDC market coupling governance.

Discovery: three server-rendered listing pages (news, publications, public
consultations) with identical markup; full history on single pages, so the
dedupe state does the change detection. Most items link PDFs directly.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, extract_pdf_text, html_to_text, slugify, MAX_CONTENT_CHARS

INSTITUTION = "NEMO Committee"
DOCUMENT_TYPE = "MARKET_OPERATOR"

BASE = "https://www.nemo-committee.eu/"
LISTINGS = ["news", "publications", "public_consultations"]
SKIP_EXT = re.compile(r"\.(png|jpe?g|gif|xlsx?|docx?|zip)$", re.I)


def _parse_date(text: str) -> str | None:
    m = re.search(r"(\d{2})/(\d{2})/(20\d{2})", text or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(0), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def discover(session):
    found: dict[str, Candidate] = {}

    diagnostics = []
    for rel in LISTINGS:
        try:
            r = get_with_retry(session, urljoin(BASE, rel), timeout=60)
        except CollectorError as exc:
            diagnostics.append(f"{rel}: {exc}")
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        if not soup.select("div.consultation"):
            title_el = soup.find("title")
            diagnostics.append(
                f"{rel}: HTTP {r.status_code}, {len(r.text)}B, no items, "
                f"title={title_el.get_text(strip=True)[:60] if title_el else '?'}")
        for item in soup.select("div.consultation"):
            link = item.select_one("a.read-more[href]") or item.select_one("a[href]")
            h2 = item.find("h2")
            if not link or not h2:
                continue
            title = " ".join(h2.get_text(" ", strip=True).split())
            href = link["href"]
            if not title or SKIP_EXT.search(href):
                continue
            url = urljoin(BASE, href)
            date_el = item.select_one("p.date")
            date = _parse_date(date_el.get_text(" ", strip=True) if date_el else "")
            c = Candidate(f"nemo-{slugify(title)}", title, date, url)
            found.setdefault(c.source_id, c)

    if not found:
        raise CollectorError("NEMO Committee discovery returned no candidates: " + "; ".join(diagnostics))
    return urljoin(BASE, "news"), list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    r = get_with_retry(session, candidate.url, timeout=90)
    ctype = (r.headers.get("content-type") or "").lower()
    if "pdf" in ctype or candidate.url.lower().endswith(".pdf"):
        return re.sub(r"[ \t]{2,}", " ", extract_pdf_text(r.content))

    soup = BeautifulSoup(r.text, "html.parser")
    banner = soup.select_one("div.consultation-banner")
    if banner:
        h = banner.find(["h1", "h2"])
        if h:
            title = " ".join(h.get_text(" ", strip=True).split())
            if len(title) > 8:
                candidate.title = title
        if not candidate.publication_date:
            date_el = banner.select_one("p.date")
            candidate.publication_date = _parse_date(date_el.get_text(" ", strip=True) if date_el else "")

    body = soup.select_one("div.consult-info")
    text = html_to_text(body) if body else ""
    if not text.strip():
        # Soft 404: the CMS returns an empty template with HTTP 200.
        raise CollectorError(f"Empty NEMO Committee page (soft 404?): {candidate.url}")

    pdf_link = None
    for a in (body or soup).select('a[href*="assets/files/"]'):
        href = a.get("href") or ""
        if "privacy" in href or "terms" in href or not href.lower().endswith(".pdf"):
            continue
        pdf_link = href
        break
    if pdf_link:
        try:
            pr = get_with_retry(session, urljoin(BASE, pdf_link), timeout=90)
            pdf_text = re.sub(r"[ \t]{2,}", " ", extract_pdf_text(pr.content))
            if pdf_text:
                text = (text + "\n\n" + pdf_text)[:MAX_CONTENT_CHARS]
        except CollectorError:
            pass
    return text
