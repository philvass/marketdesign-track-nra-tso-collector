"""Ofgem — GB NRA.

Discovery: the Drupal JSON listing API behind /publications, filtered to the
Generation & Wholesale Market sector and decision/consultation/call-for-input/
code-modification types, sorted newest first. Items arrive as rendered HTML in
the ``markup`` field. Document pages are HTML with PDF attachments.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, extract_pdf_text, html_to_text, slugify, MAX_CONTENT_CHARS

INSTITUTION = "Ofgem"
DOCUMENT_TYPE = "REGULATOR"

BASE = "https://www.ofgem.gov.uk"
API = f"{BASE}/api/listing/533"
SECTOR_WHOLESALE = "1605"
PUB_TYPES = ["1601", "1602", "2396", "6439"]  # consultation, decision, call for input, code modification
PAGES = 3
DATE_RE = re.compile(
    r"Publication date:\s*(\d{1,2})\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(20\d{2})", re.I,
)


def _parse_display_date(day: str, month: str, year: str) -> str:
    return datetime.strptime(f"{day} {month} {year}", "%d %B %Y").date().isoformat()


def _source_id(url: str) -> str:
    parts = url.split("?", 1)[0].strip("/").split("/")
    return f"ofgem-{slugify('-'.join(parts[-2:]))}"


def discover(session):
    found: dict[str, Candidate] = {}

    for page in range(PAGES):
        params = {
            "page": str(page),
            "sort[field_published][path]": "field_published",
            "sort[field_published][direction]": "desc",
            "filter[facet_industry_sector][path]": "field_industry_sector",
            "filter[facet_industry_sector][value][]": SECTOR_WHOLESALE,
            "filter[facet_case_publication_type][path]": "field_case_publication_type",
        }
        r = get_with_retry(session, API, params={
            **params,
            "filter[facet_case_publication_type][value][]": PUB_TYPES,
        })
        try:
            data = r.json()
        except ValueError as exc:
            raise CollectorError(f"Ofgem listing API returned invalid JSON: {exc}")

        items = data.get("items") or []
        if not items:
            break
        for item in items:
            markup = item.get("markup") or ""
            soup = BeautifulSoup(markup, "html.parser")
            link = soup.select_one("a[href]")
            h3 = soup.find("h3")
            if not link or not h3:
                continue
            url = urljoin(BASE, link["href"])
            title = " ".join(h3.get_text(" ", strip=True).split())
            date = None
            m = DATE_RE.search(soup.get_text(" ", strip=True))
            if m:
                date = _parse_display_date(m.group(1), m.group(2), m.group(3))
            c = Candidate(_source_id(url), title, date, url)
            found.setdefault(c.source_id, c)

    if not found:
        raise CollectorError("Ofgem discovery returned no candidates")
    return f"{API} (sector=wholesale)", list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    r = get_with_retry(session, candidate.url, timeout=45)
    soup = BeautifulSoup(r.text, "html.parser")

    h1 = soup.find("h1")
    if h1:
        title = " ".join(h1.get_text(" ", strip=True).split())
        if len(title) > 8:
            candidate.title = title
    time_el = soup.find("time", attrs={"datetime": True})
    if time_el and not candidate.publication_date:
        try:
            candidate.publication_date = datetime.fromisoformat(
                time_el["datetime"].replace("Z", "+00:00")
            ).date().isoformat()
        except ValueError:
            pass

    main = soup.find("main") or soup
    page_text = html_to_text(main)

    pdf_link = main.select_one('a[href^="/sites/default/files/"][href$=".pdf"]')
    if pdf_link:
        pdf_url = urljoin(BASE, pdf_link["href"])
        try:
            pr = get_with_retry(session, pdf_url, timeout=60)
            pdf_text = extract_pdf_text(pr.content)
            if pdf_text:
                return (page_text + "\n\n" + pdf_text)[:MAX_CONTENT_CHARS]
        except CollectorError:
            pass

    return page_text


def is_out_of_scope(candidate: Candidate) -> bool:
    t = candidate.title.lower()
    return any(x in t for x in ("gas transmission", "gas distribution", "hydrogen", "heat network", "smart meter rollout"))
