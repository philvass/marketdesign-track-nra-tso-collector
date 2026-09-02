"""GME (mercatoelettrico.org) — Italian market operator (NEMO).

Discovery: the electricity news archive (server-rendered DNN portal,
noise-filtered against daily/weekly results) plus the current DTF technical
rule documents (tracked by DTF number, so a new revision registers as an
update via the content hash).
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, extract_pdf_text, html_to_text, slugify, MAX_CONTENT_CHARS

INSTITUTION = "GME"
DOCUMENT_TYPE = "MARKET_OPERATOR"

BASE = "https://www.mercatoelettrico.org"
NEWS = f"{BASE}/it-it/Home/MediaGME/ArchivioNews"
DTF_PAGES = [
    f"{BASE}/it-it/Home/Accesso-ai-Mercati/Elettricita/MercatiElettrici/Regole/DTF-MPE",
]
PORTALS = f"{BASE}/Portals/0/Documents/it-IT/"
DATE_RE = re.compile(r"(\d{2})/(\d{2})/(20\d{2})")
NOISE_RE = re.compile(
    r"^(esiti dei mercati|dati di sintesi|prezzo medio)|newsletter gme|liquidità", re.I)


def _date(text: str) -> str | None:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(0), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def discover(session):
    found: dict[str, Candidate] = {}

    # Electricity news archive (newest 20).
    r = get_with_retry(session, NEWS, params={"Market": "E"}, timeout=45)
    soup = BeautifulSoup(r.text, "html.parser")
    for art in soup.select("section.container-archivio-news > article"):
        link = art.select_one('a[href^="?id="]')
        h4 = art.find("h4")
        if not link or not h4:
            continue
        title = " ".join(h4.get_text(" ", strip=True).split())
        if not title or NOISE_RE.search(title):
            continue
        m = re.search(r"id=(\d+)", link["href"])
        if not m:
            continue
        nid = m.group(1)
        date_el = art.select_one("span.data-news")
        found.setdefault(f"gme-news-{nid}", Candidate(
            f"gme-news-{nid}", title,
            _date(date_el.get_text(" ", strip=True) if date_el else ""),
            f"{NEWS}?id={nid}"))

    # Current DTF technical rules — stable id per DTF number; revisions update.
    for page in DTF_PAGES:
        try:
            r = get_with_retry(session, page, timeout=45)
        except CollectorError:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for doc in soup.select("div.documento"):
            onclick = doc.get("onclick") or ""
            fm = re.search(r"OpenFile\('([^']+\.pdf)'\)", onclick)
            if not fm:
                continue
            fname = fm.group(1).replace("\\", "")
            title = " ".join((doc.get("title") or fname).split())
            dm = re.match(r"(\d{8})(DTF\d+)", fname)
            key = slugify(dm.group(2) + "-" + re.sub(r"^\d{8}|rev\d+|\.pdf$", "", fname, flags=re.I)) if dm else slugify(fname)
            date = None
            if dm:
                try:
                    date = datetime.strptime(dm.group(1), "%Y%m%d").date().isoformat()
                except ValueError:
                    pass
            found.setdefault(f"gme-dtf-{key}", Candidate(
                f"gme-dtf-{key}", f"GME {title}" if not title.lower().startswith("gme") else title,
                date, PORTALS + fname))

    if not found:
        raise CollectorError("GME discovery returned no candidates")
    return f"{NEWS}?Market=E + DTF rules", list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    r = get_with_retry(session, candidate.url, timeout=90)
    ctype = (r.headers.get("content-type") or "").lower()
    if "pdf" in ctype or candidate.url.lower().endswith(".pdf"):
        text = extract_pdf_text(r.content)
        if not text.strip():
            raise CollectorError(f"GME PDF has no extractable text: {candidate.url}")
        return (f"{candidate.title}\n\n" + text)[:MAX_CONTENT_CHARS]

    soup = BeautifulSoup(r.text, "html.parser")
    module = soup.find("article") or soup.find("main") or soup
    h3 = module.find("h3")
    if h3:
        title = " ".join(h3.get_text(" ", strip=True).split())
        if len(title) > 8:
            candidate.title = title
    text = html_to_text(module)

    pdf_link = module.select_one('a[href*="/Portals/0/Documents/"][href$=".pdf"]')
    if pdf_link and len(text) < 1200:
        try:
            pr = get_with_retry(session, urljoin(BASE, pdf_link["href"].replace("\\", "")), timeout=90)
            pdf_text = extract_pdf_text(pr.content)
            if pdf_text:
                text = (text + "\n\n" + pdf_text)[:MAX_CONTENT_CHARS]
        except CollectorError:
            pass
    return text
