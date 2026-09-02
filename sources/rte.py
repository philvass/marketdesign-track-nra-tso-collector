"""RTE (Réseau de Transport d'Électricité) — French TSO.

Discovery: the services-rte.com news JSON API (high market-design signal:
SIDC/XBID, capacity mechanism, balancing/system-services rules). Requires a
JSESSIONID cookie obtained by hitting any page first. Secondary: concerte.fr
RSS (RTE's stakeholder concertation platform).
"""
from __future__ import annotations

import email.utils
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, extract_pdf_text, html_to_text, slugify, MAX_CONTENT_CHARS

INSTITUTION = "RTE"
DOCUMENT_TYPE = "TSO"

SERVICES_BASE = "https://www.services-rte.com"
NEWS_API = f"{SERVICES_BASE}/cms/public/v1/news?locale=fr"
CONCERTE_RSS = "https://www.concerte.fr/rss.xml"
MAX_NEWS = 40


def _source_id(url: str) -> str:
    last = urlparse(url).path.strip("/").split("/")[-1]
    return f"rte-{slugify(last.removesuffix('.html'))}"


def discover(session):
    found: dict[str, Candidate] = {}

    # Prime the session cookie, then hit the JSON API.
    get_with_retry(session, f"{SERVICES_BASE}/fr/actualites")
    r = get_with_retry(session, NEWS_API)
    try:
        news = (r.json() or {}).get("news") or []
    except ValueError as exc:
        raise CollectorError(f"services-rte news API returned invalid JSON: {exc}")

    for item in news[:MAX_NEWS]:
        path = (item.get("path") or "").strip()
        title = " ".join(str(item.get("title") or "").split())
        if not path or not title:
            continue
        url = urljoin(SERVICES_BASE, path)
        date = None
        raw = str(item.get("date") or "")
        if raw:
            try:
                date = datetime.fromisoformat(raw).date().isoformat()
            except ValueError:
                pass
        c = Candidate(_source_id(url), title, date, url)
        found.setdefault(c.source_id, c)

    # Concertation platform RSS (10 newest items).
    try:
        rss = get_with_retry(session, CONCERTE_RSS)
        root = ET.fromstring(rss.content)
        for item in root.findall(".//item"):
            link = (item.findtext("link") or "").strip()
            title = " ".join((item.findtext("title") or "").split())
            if not link or not title:
                continue
            date = None
            pub = item.findtext("pubDate")
            if pub:
                try:
                    date = email.utils.parsedate_to_datetime(pub).date().isoformat()
                except (TypeError, ValueError):
                    pass
            c = Candidate(f"rte-concerte-{slugify(urlparse(link).path.strip('/').split('/')[-1])}",
                          f"Concertation RTE: {title}", date, link)
            found.setdefault(c.source_id, c)
    except (CollectorError, ET.ParseError):
        pass

    if not found:
        raise CollectorError("RTE discovery returned no candidates")
    return NEWS_API, list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    r = get_with_retry(session, candidate.url, timeout=45)
    soup = BeautifulSoup(r.text, "html.parser")

    body = soup.select_one("div.newsText")
    h1 = soup.select_one("h1.c-title__title-one") or soup.find("h1")
    # The news API title is canonical; keep the page h1 as extra context only.
    h1_text = " ".join(h1.get_text(" ", strip=True).split()) if h1 else ""
    if not candidate.title and len(h1_text) > 8:
        candidate.title = h1_text

    if body:
        text = html_to_text(body)
        if h1_text and h1_text not in text:
            text = h1_text + "\n\n" + text
        # Attached rule documents (often PDFs without a .pdf extension).
        doc_link = body.select_one('a[href*="documentsLibrary"]')
        if doc_link:
            doc_url = urljoin(SERVICES_BASE, doc_link["href"])
            try:
                pr = get_with_retry(session, doc_url, timeout=90)
                if "pdf" in (pr.headers.get("content-type") or "").lower():
                    pdf_text = extract_pdf_text(pr.content)
                    if pdf_text:
                        text = (text + "\n\n" + pdf_text)[:MAX_CONTENT_CHARS]
            except CollectorError:
                pass
        return text

    # concerte.fr event pages and other layouts.
    main = soup.select_one("#main-content") or soup.select_one("main") or soup
    return html_to_text(main)
