"""EPEX SPOT (epexspot.com) — European power exchange.

Discovery: the newsroom listing (server-rendered; 18 teasers in one request),
noise-filtered against volume/results reporting. The site's WAF returns empty
HTTP 202 responses after bursts, so article fetches are paced ~20s apart and
a WAF response raises a skippable error (the document is retried next run).
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, html_to_text, slugify, MAX_CONTENT_CHARS

INSTITUTION = "EPEX SPOT"
DOCUMENT_TYPE = "MARKET_OPERATOR"

BASE = "https://www.epexspot.com"
NEWSROOM = f"{BASE}/en/newsroom"
PACING_SECONDS = 20

NOISE_RE = re.compile(
    r"monthly power trading results|annual trading results|trading record|traded volumes|"
    r"power trading results|website display issue|system disturbance|scheduled maintenance", re.I)


def _waf_guard(r, url):
    if r.status_code == 202 or len(r.text) < 500:
        raise CollectorError(f"EPEX WAF cooldown (HTTP {r.status_code}, {len(r.text)}B): {url}")
    return r


def discover(session):
    r = _waf_guard(get_with_retry(session, NEWSROOM, timeout=45), NEWSROOM)
    soup = BeautifulSoup(r.text, "html.parser")

    found: dict[str, Candidate] = {}
    for item in soup.select("div.newsroom-item"):
        title_el = item.select_one("h3.newsroom-item-title")
        link = item.select_one('a[href^="/en/news/"]')
        if not title_el or not link:
            continue
        title = " ".join(title_el.get_text(" ", strip=True).split())
        if not title or NOISE_RE.search(title):
            continue
        date = None
        date_el = item.select_one("span.date")
        if date_el:
            m = re.search(r"20\d{2}-\d{2}-\d{2}", date_el.get_text(strip=True))
            if m:
                try:
                    date = datetime.strptime(m.group(0), "%Y-%m-%d").date().isoformat()
                except ValueError:
                    pass
        url = urljoin(BASE, link["href"])
        slug = urlparse(url).path.strip("/").split("/")[-1]
        sid = f"epex-{slugify(slug)}"
        found.setdefault(sid, Candidate(sid, title, date, url))

    if not found:
        raise CollectorError("EPEX discovery returned no candidates (possible WAF block)")
    return NEWSROOM, list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    time.sleep(PACING_SECONDS)
    r = _waf_guard(get_with_retry(session, candidate.url, timeout=45), candidate.url)
    soup = BeautifulSoup(r.text, "html.parser")

    h1 = soup.select_one("h1.cms-title")
    if h1:
        title = " ".join(h1.get_text(" ", strip=True).split())
        if len(title) > 8:
            candidate.title = title

    blocks = soup.select("div.standard-page-block.standard-page-body")
    if blocks:
        text = "\n\n".join(html_to_text(b) for b in blocks)
    else:
        text = html_to_text(soup.find("main") or soup)
    return text[:MAX_CONTENT_CHARS]


def is_out_of_scope(candidate: Candidate) -> bool:
    t = candidate.title.lower()
    return any(x in t for x in ("appoint", "vacanc", "career", "media coverage"))
