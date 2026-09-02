"""JAO (Joint Allocation Office) — single allocation platform for cross-zonal capacity.

Discovery: resource-center (EU HAR and allocation/auction rules, PDFs),
public consultations listing, and the JAO news RSS stream (noise-filtered:
the stream is dominated by operational curtailment/session notices).
PDF URLs change on every revision, so rule documents are tracked by title;
the content hash detects revisions.
"""
from __future__ import annotations

import email.utils
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, extract_pdf_text, html_to_text, slugify, MAX_CONTENT_CHARS

INSTITUTION = "JAO"
DOCUMENT_TYPE = "MARKET_OPERATOR"

BASE = "https://www.jao.eu"
RESOURCE_CENTER = f"{BASE}/resource-center"
CONSULTATIONS = f"{BASE}/public-consultation"
NEWS_RSS = f"{BASE}/news/messageboard/jao/feed/rss.rss?roles_target_id_group=jao"

NEWS_NOISE = re.compile(
    r"\b(curtailment|session|maintenance|unavailability|postponed|delay|delayed|"
    r"auction results?|technical issue|fire|outage|incident|decoupling of|reminder)\b", re.I)


def discover(session):
    found: dict[str, Candidate] = {}

    # Rule documents: one server-rendered library page.
    r = get_with_retry(session, RESOURCE_CENTER, timeout=45)
    soup = BeautifulSoup(r.text, "html.parser")
    for link in soup.select("a.rc-attachment__link[href]"):
        title = " ".join(link.get_text(" ", strip=True).split())
        if not title:
            continue
        url = urljoin(BASE, link["href"])
        date = None
        time_el = link.find_next("time", attrs={"datetime": True})
        if time_el:
            try:
                date = datetime.fromisoformat(time_el["datetime"][:10]).date().isoformat()
            except ValueError:
                pass
        c = Candidate(f"jao-rc-{slugify(title)}", title, date, url)
        found.setdefault(c.source_id, c)

    # Public consultations.
    try:
        r = get_with_retry(session, CONSULTATIONS, timeout=45)
        soup = BeautifulSoup(r.text, "html.parser")
        for link in soup.select('a[href^="/public-consultation/"]'):
            title = " ".join(link.get_text(" ", strip=True).split())
            slug = urlparse(link["href"]).path.strip("/").split("/")[-1]
            if not title or not slug:
                continue
            url = urljoin(BASE, link["href"])
            c = Candidate(f"jao-consultation-{slugify(slug)}", f"JAO consultation: {title}", None, url)
            found.setdefault(c.source_id, c)
    except CollectorError:
        pass

    # News stream, minus operational noise.
    try:
        r = get_with_retry(session, NEWS_RSS, timeout=45)
        root = ET.fromstring(r.content)
        for item in root.findall(".//item"):
            link = (item.findtext("link") or "").strip().replace("http://", "https://")
            title = " ".join((item.findtext("title") or "").split())
            if not link or not title or NEWS_NOISE.search(title):
                continue
            date = None
            pub = item.findtext("pubDate")
            if pub:
                try:
                    date = email.utils.parsedate_to_datetime(pub).date().isoformat()
                except (TypeError, ValueError):
                    pass
            slug = urlparse(link).path.strip("/").split("/")[-1]
            c = Candidate(f"jao-news-{slugify(slug)}", title, date, link)
            found.setdefault(c.source_id, c)
    except (CollectorError, ET.ParseError):
        pass

    if not found:
        raise CollectorError("JAO discovery returned no candidates")
    return f"{RESOURCE_CENTER} + consultations + news RSS", list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    r = get_with_retry(session, candidate.url, timeout=90)
    ctype = (r.headers.get("content-type") or "").lower()
    if "pdf" in ctype or candidate.url.lower().endswith(".pdf"):
        return extract_pdf_text(r.content)
    if candidate.url.lower().endswith((".docx", ".xlsx")):
        raise CollectorError(f"Unsupported binary document type: {candidate.url}")

    soup = BeautifulSoup(r.text, "html.parser")
    article = soup.find("article") or soup.find("main") or soup
    text = html_to_text(article)

    pdf_link = article.select_one('a[href*="/sites/default/files/"][href$=".pdf"]')
    if pdf_link:
        try:
            pr = get_with_retry(session, urljoin(BASE, pdf_link["href"]), timeout=90)
            pdf_text = extract_pdf_text(pr.content)
            if pdf_text:
                text = (text + "\n\n" + pdf_text)[:MAX_CONTENT_CHARS]
        except CollectorError:
            pass
    return text
