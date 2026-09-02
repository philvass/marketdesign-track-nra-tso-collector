"""Nord Pool (nordpoolgroup.com) — pan-European power exchange.

Discovery: the exchange-message-list RSS feed (press releases / exchange
messages — the design-signal channel; ~1-5 items per quarter). The feed's
description field carries the full article body, so polling needs no
follow-up fetch. Operational messages and UMMs are never touched.
"""
from __future__ import annotations

import email.utils
import html as html_mod
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, html_to_text, slugify, MAX_CONTENT_CHARS

INSTITUTION = "Nord Pool"
DOCUMENT_TYPE = "MARKET_OPERATOR"

BASE = "https://www.nordpoolgroup.com"
RSS = f"{BASE}/en/message-center-container/newsroom/exchange-message-list/Rss/"
SIGNAL_PREFIX = "/newsroom/exchange-message-list/"

_CONTENT_CACHE: dict[str, str] = {}


def discover(session):
    r = get_with_retry(session, RSS, timeout=45)
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as exc:
        raise CollectorError(f"Nord Pool RSS parse failed: {exc}")

    found: dict[str, Candidate] = {}
    for item in root.findall(".//item"):
        link = (item.findtext("link") or "").strip()
        title = " ".join(html_mod.unescape(item.findtext("title") or "").split())
        if not link or not title or SIGNAL_PREFIX not in link:
            continue
        url = urljoin(BASE, link)
        date = None
        pub = item.findtext("pubDate")
        if pub:
            try:
                date = email.utils.parsedate_to_datetime(pub).date().isoformat()
            except (TypeError, ValueError):
                pass
        slug = urlparse(url).path.strip("/").split("/")[-1]
        sid = f"nordpool-{slugify(slug)}"
        desc = item.findtext("description") or ""
        if desc:
            _CONTENT_CACHE[sid] = desc
        found.setdefault(sid, Candidate(sid, title, date, url))

    if not found:
        raise CollectorError("Nord Pool discovery returned no candidates")
    return RSS, list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    raw = _CONTENT_CACHE.get(candidate.source_id, "")
    if raw:
        soup = BeautifulSoup(html_mod.unescape(raw), "html.parser")
        text = "\n".join(
            line.strip().replace("\xa0", " ")
            for line in soup.get_text("\n").splitlines() if line.strip()
        )
        if len(text) >= 200:
            return text[:MAX_CONTENT_CHARS]

    r = get_with_retry(session, candidate.url, timeout=45)
    soup = BeautifulSoup(r.text, "html.parser")
    h1 = soup.select_one("div.message-header h1")
    if h1:
        title = " ".join(h1.get_text(" ", strip=True).split())
        if len(title) > 8:
            candidate.title = title
    body = soup.select_one("div.article-column-inner") or soup.find("main") or soup
    for junk in body.select("a.message-link, div.message-header"):
        junk.decompose()
    return html_to_text(body)


def is_out_of_scope(candidate: Candidate) -> bool:
    t = candidate.title.lower()
    return any(x in t for x in ("appoint", "vacanc", "career", "christmas", "office closed"))
