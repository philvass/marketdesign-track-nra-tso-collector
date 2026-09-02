"""German TSOs (50Hertz, Amprion, TenneT DE, TransnetBW) — joint platforms.

Discovery: the DNN "LotesNewsXSP" JSON news API shared by netztransparenz.de
(all-TSO market design: capacity reserve, redispatch, Strommarktdesign) and
regelleistung.net (balancing market: FCR/aFRR/mFRR rules and consultations).
Bootstrap: scrape moduleContext JSON from the news page, POST its loginDto to
auth/Login for an anonymous session cookie, then POST newsItems/Get.
News content arrives inline as HTML (with links to PDF documents on the CDN).
"""
from __future__ import annotations

import html as html_mod
import json
import re
from datetime import datetime
from urllib.parse import urljoin, quote

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, extract_pdf_text, MAX_CONTENT_CHARS

INSTITUTION = "German TSOs"
DOCUMENT_TYPE = "TSO"

PLATFORMS = [
    ("nt", "https://www.netztransparenz.de/de-de/%C3%9Cber-uns/Aktuelles"),
    ("rl", "https://www.regelleistung.net/de-de/News/Archiv"),
]
PAGE_SIZE = 25
MODULE_RE = re.compile(r"moduleContext_\d+\s*=\s*(\{.*?\});", re.S)

_CONTENT_CACHE: dict[str, str] = {}


def _iso_date(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date().isoformat()
    except ValueError:
        return None


def _platform_news(session, key: str, bootstrap_url: str) -> list[Candidate]:
    r = get_with_retry(session, bootstrap_url, timeout=45)

    context = None
    for m in MODULE_RE.finditer(r.text):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("hubApiUrl") and data.get("loginDto"):
            context = data
            if data.get("detailsPageUrl"):
                break
    if not context:
        raise CollectorError(f"No LotesNewsXSP moduleContext found on {bootstrap_url}")

    hub = context["hubApiUrl"]
    login = session.post(urljoin(hub, "auth/Login"), json=context["loginDto"], timeout=30)
    if not login.ok:
        raise CollectorError(f"XSP auth/Login failed on {hub}: HTTP {login.status_code}")

    resp = session.post(urljoin(hub, "newsItems/Get"), json={
        "contains": "", "page": 1, "pageSize": PAGE_SIZE, "descending": True,
        "type": 0, "clientId": context.get("clientId"),
        "includeDefaultCategory": False, "languageTag": "de-DE",
        "showPublished": True, "showUnpublished": False,
    }, timeout=45)
    if not resp.ok:
        raise CollectorError(f"XSP newsItems/Get failed on {hub}: HTTP {resp.status_code}")
    items = ((resp.json() or {}).get("data") or {}).get("items") or []

    details_base = context.get("detailsPageUrl") or bootstrap_url
    out: list[Candidate] = []
    for item in items:
        contents = item.get("newsContentList") or []
        if not contents:
            continue
        first = contents[0] or {}
        title = " ".join(html_mod.unescape(str(first.get("title") or "")).split())
        if not title:
            continue
        news_id = item.get("id")
        date = _iso_date(item.get("publishStartDate")) or _iso_date(item.get("dateCreated"))
        url = f"{details_base}/{news_id}"
        sid = f"gtsos-{key}-{news_id}"
        _CONTENT_CACHE[sid] = str(first.get("content") or "")
        out.append(Candidate(sid, title, date, url))
    return out


def discover(session):
    found: dict[str, Candidate] = {}
    seen_title_date: set[tuple[str, str]] = set()
    errors = []

    for key, bootstrap_url in PLATFORMS:
        try:
            for c in _platform_news(session, key, bootstrap_url):
                # Items are frequently cross-posted on both platforms.
                fp = (c.title.lower(), c.publication_date or "")
                if fp in seen_title_date:
                    continue
                seen_title_date.add(fp)
                found.setdefault(c.source_id, c)
        except CollectorError as exc:
            errors.append(str(exc))

    if not found:
        raise CollectorError("German TSO discovery returned no candidates: " + "; ".join(errors))
    return "netztransparenz.de + regelleistung.net news APIs", list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    raw_html = _CONTENT_CACHE.get(candidate.source_id, "")
    if not raw_html:
        r = get_with_retry(session, candidate.url, timeout=45)
        raw_html = r.text

    soup = BeautifulSoup(raw_html, "html.parser")
    text = "\n".join(
        line.strip()
        for line in html_mod.unescape(soup.get_text("\n")).splitlines()
        if line.strip()
    )

    pdf_link = soup.select_one('a[href*="/cdn/files/"], a[href*="staticfiles"]')
    if pdf_link:
        href = pdf_link.get("href") or ""
        base = "https://www.regelleistung.net" if candidate.source_id.startswith("gtsos-rl") \
            else "https://www.netztransparenz.de"
        pdf_url = urljoin(base, quote(href, safe=":/?&=%"))
        try:
            pr = get_with_retry(session, pdf_url, timeout=90)
            pdf_text = extract_pdf_text(pr.content)
            if pdf_text:
                text = (text + "\n\n" + pdf_text)[:MAX_CONTENT_CHARS]
        except CollectorError:
            pass

    return text[:MAX_CONTENT_CHARS]


def is_out_of_scope(candidate: Candidate) -> bool:
    t = candidate.title.lower()
    return any(x in t for x in ("wasserstoff", "gasnetz", "stellenausschreibung", "wartung an der website"))
