"""NESO (National Energy System Operator, GB).

Discovery: the site RSS firehose (all newly published nodes, with a noise
blocklist) plus the code-modification sitemap (CUSC/Grid Code/STC changes,
newest by lastmod). Documents are PDFs at /document/{id}/download; code-mod
pages are server-rendered HTML.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, extract_pdf_text, html_to_text, looks_like_pdf, slugify, MAX_CONTENT_CHARS

INSTITUTION = "NESO"
DOCUMENT_TYPE = "TSO"

BASE = "https://www.neso.energy"
RSS_PAGES = 2
CODEMOD_SITEMAP = f"{BASE}/code-modification/sitemap.xml"
MAX_CODEMODS = 25
SM_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

NOISE = re.compile(
    r"\b(foi|freedom of information|daily|weekly|carbon intensity|outturn|"
    r"demand forecast|wind forecast|transparency data|gas quality)\b", re.I)


def _source_id(url: str) -> str:
    path = urlparse(url).path.strip("/")
    m = re.search(r"/document/(\d+)/", url)
    if m:
        return f"neso-document-{m.group(1)}"
    return f"neso-{slugify(path.split('/')[-1])}"


def _iso(raw: str) -> str | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except (ValueError, AttributeError):
        return None


def discover(session):
    found: dict[str, Candidate] = {}

    # RSS firehose (10 items/page).
    for page in range(RSS_PAGES):
        try:
            r = get_with_retry(session, f"{BASE}/rss.xml", params={"page": page})
            root = ET.fromstring(r.content)
        except (CollectorError, ET.ParseError):
            break
        for item in root.findall(".//item"):
            link = (item.findtext("link") or "").strip()
            title = " ".join((item.findtext("title") or "").split())
            if not link or not title or NOISE.search(title):
                continue
            desc = item.findtext("description") or ""
            m = re.search(r'datetime="([^"]+)"', desc)
            date = _iso(m.group(1)) if m else None
            c = Candidate(_source_id(link), title, date, link)
            found.setdefault(c.source_id, c)

    # Code-modification sitemap — take the newest entries by lastmod.
    try:
        r = get_with_retry(session, CODEMOD_SITEMAP)
        root = ET.fromstring(r.content)
        entries = []
        for url_el in root.findall(".//sm:url", SM_NS):
            loc = (url_el.findtext("sm:loc", default="", namespaces=SM_NS) or "").strip()
            lastmod = _iso(url_el.findtext("sm:lastmod", default="", namespaces=SM_NS) or "")
            if loc:
                entries.append((lastmod or "0000-00-00", loc))
        entries.sort(reverse=True)
        for lastmod, loc in entries[:MAX_CODEMODS]:
            date = None if lastmod == "0000-00-00" else lastmod
            c = Candidate(_source_id(loc), "", date, loc)
            found.setdefault(c.source_id, c)
    except (CollectorError, ET.ParseError):
        pass

    if not found:
        raise CollectorError("NESO discovery returned no candidates")
    return f"{BASE}/rss.xml + code-modification sitemap", list(found.values())


MIN_PDF_TEXT = 200


def _pdf_text_or_refuse(session, url: str, response) -> str:
    """Extract a PDF's text, re-downloading once before giving up.

    A PDF that yields no text is either a truncated or corrupt download, which a
    second request usually fixes, or a scanned image, which nothing here can fix.
    Returning the raw stream instead is the expensive failure: on 4 Sept 2026 a
    NESO PDF whose extraction failed was stored as 86KB of object streams,
    reached the analysis models, tokenised to 174k input tokens and cost $0.67
    to produce an event that said the content was unreadable. One extra request
    is far cheaper than that, so retry, then refuse.

    Refusing raises, which core.py records as a skip. That loses the document —
    but a document TRACK cannot read is not a document it can report on.
    """
    try:
        text = extract_pdf_text(response.content)
    except CollectorError:
        text = ""
    if len(text.strip()) >= MIN_PDF_TEXT:
        return text

    retry = get_with_retry(session, url, timeout=120)
    try:
        text = extract_pdf_text(retry.content)
    except CollectorError as exc:
        raise CollectorError(f"PDF text extraction failed twice for {url}: {exc}")
    if len(text.strip()) >= MIN_PDF_TEXT:
        return text

    raise CollectorError(
        f"PDF at {url} yielded no extractable text after a retry "
        f"({len(response.content)} bytes downloaded); refusing to submit the raw stream"
    )


def fetch_content(session, candidate: Candidate) -> str:
    r = get_with_retry(session, candidate.url, timeout=90)

    # NESO serves PDFs both from /document/{id}/download and straight from
    # plain content paths (e.g. /gc0186-workgroup-consultation), so trust the
    # response rather than the URL shape.
    if looks_like_pdf(r):
        if not candidate.title:
            cd = r.headers.get("content-disposition") or ""
            m = re.search(r'filename="([^"]+)"', cd)
            if m:
                candidate.title = m.group(1).rsplit(".", 1)[0]
        return _pdf_text_or_refuse(session, candidate.url, r)

    soup = BeautifulSoup(r.text, "html.parser")
    h1 = soup.find("h1")
    if h1:
        title = " ".join(h1.get_text(" ", strip=True).split())
        if len(title) > 8:
            candidate.title = title
    if not candidate.title:
        candidate.title = candidate.source_id

    main = soup.find("article") or soup.find("main") or soup
    text = html_to_text(main)

    # NESO pages inline ~1.2MB of assets and nav; the attached PDF is the
    # authoritative content whenever one exists.
    doc_link = main.select_one('a[href*="/document/"][href$="/download"]')
    if doc_link:
        try:
            pr = get_with_retry(session, urljoin(BASE, doc_link["href"]), timeout=90)
            pdf_text = extract_pdf_text(pr.content)
            if pdf_text:
                return (f"{candidate.title}\n\n" + pdf_text)[:MAX_CONTENT_CHARS]
        except CollectorError:
            pass
    return text


# Grid Code (GC0186) and CUSC (CMP479) modification proposals. Discovery pulls
# the code-modification sitemap wholesale, so these arrive on every run; they are
# connection standards and use-of-system charging, which TRACK does not cover.
# Deliberately narrow: BSC modifications (P462, P521 — via elexon.py) and
# Capacity Market items are market design and must keep passing. Requiring
# digits stops the prefixes matching ordinary words.
CODE_MOD_OUT_OF_SCOPE = re.compile(r"\b(?:gc|cmp)\d{3,4}\b")


def is_out_of_scope(candidate: Candidate) -> bool:
    t = candidate.title.lower()
    if any(x in t for x in ("gas ", "hydrogen", "vacanc", "careers", "annual report and accounts")):
        return True
    return bool(CODE_MOD_OUT_OF_SCOPE.search(t))
