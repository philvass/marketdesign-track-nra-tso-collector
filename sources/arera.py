"""ARERA (arera.it) — Italian NRA.

Discovery: server-rendered TYPO3 listing of delibere and consultation
documents (DCO) filtered to the electricity sector (settore=4). Act text is
PDF-only under /fileadmin/allegati/docs/.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, extract_pdf_text, html_to_text, slugify, MAX_CONTENT_CHARS

INSTITUTION = "ARERA"
DOCUMENT_TYPE = "REGULATOR"

BASE = "https://www.arera.it"
LISTING = f"{BASE}/atti-e-provvedimenti"
TYPES = ["Delibera", "Consultazione"]
SIGLA_KEEP = re.compile(r"/R/(eel|com)\b", re.I)


def discover(session):
    found: dict[str, Candidate] = {}

    for tipologia in TYPES:
        try:
            r = get_with_retry(session, LISTING, params={"tipologia": tipologia, "settore": "4"}, timeout=45)
        except CollectorError:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for link in soup.select('a[href^="/atti-e-provvedimenti/dettaglio/"]'):
            sigla_el = link.select_one(".sigla-atto")
            title_el = link.select_one(".testo-atto p") or link.select_one(".testo-atto")
            date_el = link.select_one(".data-atto")
            sigla = " ".join(sigla_el.get_text(" ", strip=True).split()) if sigla_el else ""
            if not sigla or not SIGLA_KEEP.search(sigla):
                continue
            title = " ".join(title_el.get_text(" ", strip=True).split()) if title_el else ""
            if not title:
                continue
            date = None
            if date_el:
                m = re.search(r"(\d{2})/(\d{2})/(20\d{2})", date_el.get_text(" ", strip=True))
                if m:
                    try:
                        date = datetime.strptime(m.group(0), "%d/%m/%Y").date().isoformat()
                    except ValueError:
                        pass
            url = urljoin(BASE, link["href"])
            sid = f"arera-{slugify(sigla)}"
            found.setdefault(sid, Candidate(sid, f"{sigla} — {title}", date, url))

    if not found:
        raise CollectorError("ARERA discovery returned no candidates")
    return f"{LISTING} (Delibera+Consultazione, settore elettricità)", list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    r = get_with_retry(session, candidate.url, timeout=45)
    soup = BeautifulSoup(r.text, "html.parser")
    text = html_to_text(soup.find("main") or soup)

    pdf_link = soup.select_one('a[href^="/fileadmin/allegati/docs/"][href$=".pdf"]')
    if pdf_link:
        try:
            pr = get_with_retry(session, urljoin(BASE, pdf_link["href"]), timeout=90)
            pdf_text = extract_pdf_text(pr.content)
            if pdf_text:
                text = (pdf_text + "\n\n" + text)[:MAX_CONTENT_CHARS]
        except CollectorError:
            pass
    return text[:MAX_CONTENT_CHARS]
