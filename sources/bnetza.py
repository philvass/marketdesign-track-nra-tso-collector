"""BNetzA (Bundesnetzagentur) — German NRA.

Discovery: Beschlusskammer 6 (electricity market/balancing) and BK8 listing
pages plus electricity press releases. All server-rendered Government Site
Builder pages; dates DD.MM.YYYY; decisions are PDFs behind
``__blob=publicationFile`` links on case basepages.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, extract_pdf_text, html_to_text, slugify, MAX_CONTENT_CHARS

INSTITUTION = "BNetzA"
DOCUMENT_TYPE = "REGULATOR"

BASE = "https://www.bundesnetzagentur.de/"
LISTINGS = [
    "DE/Beschlusskammern/BK06/BK6_01_Aktuell/BK6_Aktuelles.html",
    "DE/Beschlusskammern/BK06/BK6_11_LV/BK6_LV.html",
    "DE/Beschlusskammern/BK08/BK8_01_Aktuell/BK8_Aktuell.html",
]
PRESS = "DE/Allgemeines/Presse/Pressemitteilungen/start.html"
CASE_RE = re.compile(r"(BK\d{1,2}-\d{2}-\d{3,4}[A-Za-z]?)", re.I)
DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(20\d{2})")


def _clean_url(url: str) -> str:
    parts = urlparse(url)
    return urlunparse(parts._replace(query="", fragment=""))


def _parse_date(text: str) -> str | None:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(0), "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None


def _source_id(url: str, title: str) -> str:
    case = CASE_RE.search(url) or CASE_RE.search(title)
    if case:
        return f"bnetza-{case.group(1).lower()}"
    path = urlparse(url).path.strip("/").removesuffix(".html").split("/")
    return f"bnetza-{slugify('-'.join(path[-2:]))}"


def _abs_url(href: str) -> str:
    # BNetzA hrefs are site-root paths without a leading slash ("DE/...",
    # "SharedDocs/..."); joining against the listing page URL mis-nests them.
    return _clean_url(urljoin(BASE, href.lstrip("/")))


def _rows_from_listing(html: str, page_url: str) -> list[Candidate]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Candidate] = []
    for row in soup.select("#content table tr"):
        link = row.select_one("a[href]")
        if not link:
            continue
        cells = row.find_all("td")
        title = " ".join(link.get_text(" ", strip=True).replace("\xad", "").split())
        if not title or len(title) < 8:
            continue
        url = _abs_url(link["href"])
        if "bundesnetzagentur.de" not in url:
            continue
        date = None
        for td in cells:
            date = _parse_date(td.get_text(" ", strip=True))
            if date:
                break
        out.append(Candidate(_source_id(url, title), title, date, url))
    return out


def discover(session):
    found: dict[str, Candidate] = {}

    for rel in LISTINGS:
        try:
            r = get_with_retry(session, urljoin(BASE, rel))
        except CollectorError:
            continue
        for c in _rows_from_listing(r.text, r.url):
            found.setdefault(c.source_id, c)

    # Electricity press releases (sector label in second cell).
    try:
        r = get_with_retry(session, urljoin(BASE, PRESS))
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select("#content table tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            sector = cells[1].get_text(" ", strip=True)
            if not sector.lower().startswith("elektrizität"):
                continue
            link = cells[1].select_one("a[href]")
            if not link:
                continue
            title = " ".join(link.get_text(" ", strip=True).replace("\xad", "").split())
            url = _abs_url(link["href"])
            date = _parse_date(cells[0].get_text(" ", strip=True))
            c = Candidate(_source_id(url, title), title, date, url)
            found.setdefault(c.source_id, c)
    except CollectorError:
        pass

    if not found:
        raise CollectorError("BNetzA discovery returned no candidates")
    return urljoin(BASE, LISTINGS[0]), list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    r = get_with_retry(session, candidate.url, timeout=45)
    ctype = (r.headers.get("content-type") or "").lower()
    if "pdf" in ctype or candidate.url.lower().endswith(".pdf"):
        return extract_pdf_text(r.content).replace("\xad", "")

    soup = BeautifulSoup(r.text, "html.parser")
    h1 = soup.find("h1")
    if h1:
        title = " ".join(h1.get_text(" ", strip=True).replace("\xad", "").split())
        if len(title) > 8:
            candidate.title = title
    if not candidate.publication_date:
        candidate.publication_date = _parse_date(soup.get_text(" ", strip=True)[:3000])

    content_div = soup.select_one("#content") or soup
    page_text = html_to_text(content_div).replace("\xad", "")

    # Case basepages link the actual decision/consultation PDFs.
    pdf_link = content_div.select_one('a[href*="__blob=publicationFile"]')
    if pdf_link and len(page_text) < 4000:
        pdf_url = urljoin(r.url, pdf_link["href"])
        try:
            pr = get_with_retry(session, pdf_url, timeout=60)
            pdf_text = extract_pdf_text(pr.content).replace("\xad", "")
            if pdf_text:
                page_text = (page_text + "\n\n" + pdf_text)[:MAX_CONTENT_CHARS]
        except CollectorError:
            pass

    return page_text


def is_out_of_scope(candidate: Candidate) -> bool:
    t = candidate.title.lower()
    return any(x in t for x in ("wasserstoff", "gasnetz", "gasmarkt", "telekommunikation", "post", "eisenbahn"))
