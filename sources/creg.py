"""CREG (creg.be) — Belgian NRA.

Discovery: server-rendered Drupal faceted publications listing (French),
polled across the electricity market-design themes. Full text lives in PDFs
linked from each publication page.
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, extract_pdf_text, html_to_text, slugify, MAX_CONTENT_CHARS

INSTITUTION = "CREG"
DOCUMENT_TYPE = "REGULATOR"

BASE = "https://www.creg.be"
LISTING = f"{BASE}/fr/publications"
# thema facet ids: market functioning, interconnection, CRM, market model,
# ancillary services, flexibility
THEMES = ["222", "250", "313", "254", "261", "212"]


def _source_id(url: str) -> str:
    path = urlparse(url).path.strip("/")
    tail = path.split("publications/", 1)[-1] if "publications/" in path else path
    return f"creg-{slugify(tail.replace('/', '-'))}"


def discover(session):
    found: dict[str, Candidate] = {}

    for theme in THEMES:
        try:
            r = get_with_retry(session, LISTING, params={"f[0]": f"thema:{theme}"}, timeout=45)
        except CollectorError:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for item in soup.select("article.search-result"):
            link = item.select_one("header a[href]")
            if not link:
                continue
            title = " ".join(link.get_text(" ", strip=True).split())
            url = urljoin(BASE, link["href"])
            date = None
            time_el = item.select_one("time.datetime[datetime]") or item.find("time", attrs={"datetime": True})
            if time_el:
                try:
                    date = datetime.fromisoformat(time_el["datetime"].replace("Z", "+00:00")).date().isoformat()
                except ValueError:
                    pass
            c = Candidate(_source_id(url), title, date, url)
            found.setdefault(c.source_id, c)

    if not found:
        raise CollectorError("CREG discovery returned no candidates")
    return f"{LISTING} (electricity market themes)", list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    r = get_with_retry(session, candidate.url, timeout=45)
    soup = BeautifulSoup(r.text, "html.parser")

    h1 = soup.find("h1")
    if h1:
        title = " ".join(h1.get_text(" ", strip=True).split())
        if len(title) > 8:
            candidate.title = title

    body = soup.select_one(".field--body") or soup.find("main") or soup
    text = html_to_text(body)

    pdf_link = soup.select_one('a[href$=".pdf"]')
    if pdf_link:
        try:
            pr = get_with_retry(session, urljoin(BASE, pdf_link["href"]), timeout=90)
            pdf_text = extract_pdf_text(pr.content)
            if pdf_text:
                text = (text + "\n\n" + pdf_text)[:MAX_CONTENT_CHARS]
        except CollectorError:
            pass
    return text


def is_out_of_scope(candidate: Candidate) -> bool:
    t = candidate.title.lower()
    return any(x in t for x in ("gaz naturel", "hydrogène", "gnl", "stockage de gaz"))
