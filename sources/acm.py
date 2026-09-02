"""ACM (acm.nl) — Dutch NRA (energy department).

Discovery: server-rendered Drupal search filtered to energy publications with
the electricity keyword, newest first. The WAF intermittently 302s to a 403
page — a Referer header plus one retry handles it.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, extract_pdf_text, html_to_text, slugify, MAX_CONTENT_CHARS

INSTITUTION = "ACM"
DOCUMENT_TYPE = "REGULATOR"

BASE = "https://www.acm.nl"
SEARCH = f"{BASE}/nl/zoeken"
PAGES = 2


def _get(session, url, **kwargs):
    kwargs.setdefault("headers", {})["Referer"] = SEARCH
    r = get_with_retry(session, url, **kwargs)
    if "/system/403" in r.url:
        r = get_with_retry(session, url, **kwargs)
        if "/system/403" in r.url:
            raise CollectorError(f"ACM WAF blocked {url}")
    return r


def discover(session):
    found: dict[str, Candidate] = {}

    for page in range(PAGES):
        params = {
            "search": "",
            "mixed_content_type[publication]": "publication",
            "field_subjects_name[Energie]": "Energie",
            "keyword": "Elektriciteit",
            "sort_by": "created",
        }
        if page:
            params["page"] = str(page)
        try:
            r = _get(session, SEARCH, params=params, timeout=45)
        except CollectorError:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        for card in soup.select(".m-card"):
            link = card.select_one("h2.m-card__title a[href]")
            if not link:
                continue
            title = " ".join(link.get_text(" ", strip=True).split())
            url = urljoin(BASE, link["href"])
            date = None
            meta = card.select_one(".m-card__meta")
            if meta:
                m = re.search(r"(\d{2})-(\d{2})-(20\d{2})", meta.get_text(" ", strip=True))
                if m:
                    try:
                        date = datetime.strptime(m.group(0), "%d-%m-%Y").date().isoformat()
                    except ValueError:
                        pass
            slug = urlparse(url).path.strip("/").split("/")[-1]
            c = Candidate(f"acm-{slugify(slug)}", title, date, url)
            found.setdefault(c.source_id, c)

    if not found:
        raise CollectorError("ACM discovery returned no candidates")
    return f"{SEARCH} (Energie/Elektriciteit publications)", list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    r = _get(session, candidate.url, timeout=45)
    soup = BeautifulSoup(r.text, "html.parser")

    h1 = soup.find("h1")
    if h1:
        title = " ".join(h1.get_text(" ", strip=True).split())
        if len(title) > 8:
            candidate.title = title
    time_el = soup.find("time", attrs={"datetime": True})
    if time_el:
        try:
            candidate.publication_date = datetime.fromisoformat(
                time_el["datetime"].replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            pass

    main = soup.find("main") or soup
    text = html_to_text(main)

    pdf_link = main.select_one('a[href^="/system/files/documents/"]')
    if pdf_link:
        try:
            pr = _get(session, urljoin(BASE, pdf_link["href"]), timeout=90)
            if "pdf" in (pr.headers.get("content-type") or "").lower() or pdf_link["href"].lower().endswith(".pdf"):
                pdf_text = extract_pdf_text(pr.content)
                if pdf_text:
                    text = (text + "\n\n" + pdf_text)[:MAX_CONTENT_CHARS]
        except CollectorError:
            pass
    return text


def is_out_of_scope(candidate: Candidate) -> bool:
    t = candidate.title.lower()
    return any(x in t for x in ("gasnet", "waterstof", "warmte", "drinkwater", "telecom", "post"))
