"""AIB (Association of Issuing Bodies) — EECS Guarantees of Origin scheme.

Discovery: /news teasers (only 3 shown — polled every run) plus sitemap.xml
lastmod tracking for the substantive pages (EECS rules, residual mix,
consultations, membership). Page changes are caught by the content hash;
sitemap lastmod provides the publication date signal.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, extract_pdf_text, html_to_text, slugify, MAX_CONTENT_CHARS

INSTITUTION = "AIB"
DOCUMENT_TYPE = "MARKET_OPERATOR"

BASE = "https://www.aib-net.org"
SM_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
PAGE_ALLOW = re.compile(
    r"/(eecs|facts/european-residual-mix|aib/governance/consultation|"
    r"news-events/third-party-consultations|facts/aib-member-countries)", re.I)


def _iso(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def discover(session):
    found: dict[str, Candidate] = {}

    # Latest news teasers (only three are ever listed).
    try:
        r = get_with_retry(session, f"{BASE}/news", timeout=45)
        soup = BeautifulSoup(r.text, "html.parser")
        for teaser in soup.select("article.news-teaser"):
            h3 = teaser.find("h3")
            parent_link = teaser.find_parent("a", href=True)
            link = parent_link or teaser.select_one("a[href]")
            if not h3 or not link:
                continue
            title = " ".join(h3.get_text(" ", strip=True).split())
            url = urljoin(BASE, link["href"])
            time_el = teaser.find("time", attrs={"datetime": True})
            date = _iso(time_el["datetime"] if time_el else None)
            m = re.search(r"/node/(\d+)", url)
            sid = f"aib-node-{m.group(1)}" if m else f"aib-{slugify(title)}"
            found.setdefault(sid, Candidate(sid, title, date, url))
    except CollectorError:
        pass

    # Substantive pages tracked via sitemap lastmod + content hash.
    try:
        r = get_with_retry(session, f"{BASE}/sitemap.xml", timeout=45)
        root = ET.fromstring(r.content)
        for url_el in root.findall(".//sm:url", SM_NS):
            loc = (url_el.findtext("sm:loc", default="", namespaces=SM_NS) or "").strip()
            if not loc or not PAGE_ALLOW.search(urlparse(loc).path):
                continue
            lastmod = _iso(url_el.findtext("sm:lastmod", default="", namespaces=SM_NS))
            slug = slugify(urlparse(loc).path.strip("/").replace("/", "-"))
            sid = f"aib-page-{slug}"
            found.setdefault(sid, Candidate(sid, f"AIB: {slug.replace('-', ' ')}", lastmod, loc))
    except (CollectorError, ET.ParseError):
        pass

    if not found:
        raise CollectorError("AIB discovery returned no candidates")
    return f"{BASE}/news + sitemap.xml", list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    r = get_with_retry(session, candidate.url, timeout=60)
    soup = BeautifulSoup(r.text, "html.parser")

    h1 = soup.find("h1")
    if h1:
        title = " ".join(h1.get_text(" ", strip=True).split())
        if len(title) > 8 and not candidate.source_id.startswith("aib-node-"):
            candidate.title = f"AIB: {title}"
        elif len(title) > 8:
            candidate.title = title
    if not candidate.publication_date:
        time_el = soup.find("time", attrs={"datetime": True})
        if time_el:
            candidate.publication_date = _iso(time_el["datetime"])

    article = soup.find("article")
    body = (article.select_one(".text") if article else None) \
        or soup.select_one("div.col-lg-9") or soup.find("main") or soup
    text = html_to_text(body)

    pdf_link = body.select_one('a[href*="/sites/default/files/"][href$=".pdf"], div.item-download a[href$=".pdf"]')
    if pdf_link and len(text) < 4000:
        try:
            pr = get_with_retry(session, urljoin(BASE, pdf_link["href"]), timeout=90)
            pdf_text = extract_pdf_text(pr.content)
            if pdf_text:
                text = (text + "\n\n" + pdf_text)[:MAX_CONTENT_CHARS]
        except CollectorError:
            pass
    return text
