"""Energinet (en.energinet.dk) — Danish TSO.

Discovery: the Umbraco FacetedEnerListApi JSON search endpoint, polled for
general news and ancillary-services news (where electricity market
consultations are posted). Article bodies are embedded server-side as
HTML-escaped JSON in a data-model attribute.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, extract_pdf_text, html_to_text, slugify, MAX_CONTENT_CHARS

INSTITUTION = "Energinet"
DOCUMENT_TYPE = "TSO"

BASE = "https://en.energinet.dk"
API = f"{BASE}/umbraco/api/FacetedEnerListApi/Search"
NODES = [61094, 60274]  # general news, ancillary-services news


def _post_with_retry(session, url, payload, attempts=3, timeout=45):
    last = None
    for n in range(attempts):
        try:
            r = session.post(url, json=payload, timeout=timeout)
            if r.status_code >= 500 and n + 1 < attempts:
                time.sleep(1.5 * (n + 1))
                continue
            r.raise_for_status()
            return r
        except Exception as exc:  # requests.RequestException
            last = exc
            if n + 1 < attempts:
                time.sleep(1.5 * (n + 1))
    raise CollectorError(f"POST failed for {url}: {last}")


def _parse_list_date(raw: str) -> str | None:
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.\s*(20\d{2})", raw or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date().isoformat()
    except ValueError:
        return None


def discover(session):
    found: dict[str, Candidate] = {}

    for node in NODES:
        try:
            r = _post_with_retry(session, API, {
                "RootNodeId": node, "FacetFilters": [], "SortOrder": 3,
                "PageSize": 20, "PageNumber": 1,
            })
            results = (r.json() or {}).get("searchResults") or []
        except (CollectorError, ValueError):
            continue
        for item in results:
            link = (item.get("link") or {}).get("url") or ""
            title = " ".join(str(item.get("headline") or (item.get("link") or {}).get("name") or "").split())
            if not link or not title:
                continue
            url = urljoin(BASE, link)
            slug = urlparse(url).path.strip("/").split("/")[-1]
            c = Candidate(f"energinet-{slugify(slug)}", title, _parse_list_date(str(item.get("date") or "")), url)
            found.setdefault(c.source_id, c)

    if not found:
        raise CollectorError("Energinet discovery returned no candidates")
    return API, list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    r = get_with_retry(session, candidate.url, timeout=45)
    soup = BeautifulSoup(r.text, "html.parser")

    model_el = soup.select_one("div.js-news-page[data-model]")
    text = ""
    if model_el:
        try:
            model = json.loads(html_mod.unescape(model_el["data-model"]))
            headline = str(model.get("Headline") or "")
            teaser = str(model.get("TeaserText") or "")
            body_html = str(model.get("BodyContent") or "")
            body = BeautifulSoup(body_html, "html.parser").get_text("\n", strip=True)
            text = "\n\n".join(x for x in (headline, teaser, body) if x.strip())
            if headline and len(headline) > 8:
                candidate.title = " ".join(headline.split())
        except (ValueError, KeyError):
            pass
    if not text:
        text = html_to_text(soup.find("main") or soup)

    pdf_link = soup.select_one('a[href*="/media/"][href$=".pdf"]')
    if pdf_link and len(text) < 4000:
        try:
            pr = get_with_retry(session, urljoin(BASE, pdf_link["href"]), timeout=90)
            pdf_text = extract_pdf_text(pr.content)
            if pdf_text:
                text = (text + "\n\n" + pdf_text)[:MAX_CONTENT_CHARS]
        except CollectorError:
            pass
    return text[:MAX_CONTENT_CHARS]


def is_out_of_scope(candidate: Candidate) -> bool:
    t = candidate.title.lower()
    return any(x in t for x in ("hydrogen", "brint", "gas storage", "ceo", "vacanc"))
