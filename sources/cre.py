"""CRE (Commission de régulation de l'énergie) — French NRA.

Discovery: server-rendered TYPO3/Solr listing pages for délibérations and
consultations publiques (newest 25 each; paginated/filtered Solr URLs are
Cloudflare-blocked for non-browsers, so only the bare first pages are used).
Document pages are metadata stubs linking the actual PDFs.
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, extract_pdf_text, html_to_text, slugify, MAX_CONTENT_CHARS

INSTITUTION = "CRE"
DOCUMENT_TYPE = "REGULATOR"

BASE = "https://www.cre.fr/"
LISTINGS = [
    "documents/deliberations.html",
    "documents/consultations-publiques.html",
]


def _iso_date(time_el) -> str | None:
    raw = (time_el.get("datetime") or "").strip() if time_el else ""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date().isoformat()
    except ValueError:
        return None


def _source_id(url: str, number: str | None) -> str:
    if number:
        return f"cre-{slugify(number.replace('°', ''))}"
    last = urlparse(url).path.strip("/").split("/")[-1]
    return f"cre-{slugify(last.removesuffix('.html'))}"


def discover(session):
    found: dict[str, Candidate] = {}

    for rel in LISTINGS:
        r = get_with_retry(session, urljoin(BASE, rel))
        soup = BeautifulSoup(r.text, "html.parser")
        for item in soup.select("#tx-solr-results > li"):
            link = item.select_one("h3.card-title a")
            if not link:
                continue
            labels = {x.get_text(" ", strip=True) for x in item.select(".card-labels .label")}
            if labels and "Électricité" not in labels:
                continue
            title = " ".join(link.get_text(" ", strip=True).split())
            url = urljoin(r.url, link["href"])
            times = item.select("p.card-data time.card-time")
            date = _iso_date(times[0]) if times else None
            number_el = item.select_one("strong.card-number")
            number = number_el.get_text(" ", strip=True) if number_el else None
            c = Candidate(_source_id(url, number), title, date, url)
            found.setdefault(c.source_id, c)

    if not found:
        raise CollectorError("CRE discovery returned no candidates")
    return urljoin(BASE, LISTINGS[0]), list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    r = get_with_retry(session, candidate.url, timeout=45)
    soup = BeautifulSoup(r.text, "html.parser")

    h1 = soup.find("h1")
    if h1:
        title = " ".join(h1.get_text(" ", strip=True).split())
        if len(title) > 8:
            candidate.title = title

    main = soup.select_one("main") or soup.select_one("#content") or soup
    stub_text = html_to_text(main)

    pdf_link = main.select_one('a[href$=".pdf"]') or soup.select_one('a[href$=".pdf"]')
    if pdf_link:
        pdf_url = urljoin(r.url, pdf_link["href"])
        try:
            pr = get_with_retry(session, pdf_url, timeout=60)
            pdf_text = extract_pdf_text(pr.content)
            if pdf_text:
                return (stub_text + "\n\n" + pdf_text)[:MAX_CONTENT_CHARS]
        except CollectorError:
            pass

    return stub_text


def is_out_of_scope(candidate: Candidate) -> bool:
    t = candidate.title.lower()
    return any(x in t for x in ("gaz naturel", "hydrogène", "biométhane", "gnl"))
