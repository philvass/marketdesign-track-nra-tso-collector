"""European Commission DG CLIMA — EU ETS / carbon markets.

Discovery: the climate.ec.europa.eu news listing (same Europa CMS/ECL
framework as the existing DG ENER collector), keyword-filtered to carbon
market topics, plus CLIMA initiatives from the Better Regulation API.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, html_to_text, slugify, MAX_CONTENT_CHARS

INSTITUTION = "European Commission (DG CLIMA)"
DOCUMENT_TYPE = "REGULATOR"

BASE = "https://climate.ec.europa.eu"
NEWS = f"{BASE}/news-other-reads/news_en"
BRP_SEARCH = "https://ec.europa.eu/info/law/better-regulation/brpapi/searchInitiatives"
BRP_DETAIL = "https://ec.europa.eu/info/law/better-regulation/brpapi/groupInitiatives"
NEWS_PAGES = 2

CARBON_RE = re.compile(
    r"\b(ets\d?|emission|carbon|allowance|auction|msr|market stability reserve|"
    r"cbam|effort sharing|cap[- ]and[- ]trade)\b", re.I)


def _iso(el) -> str | None:
    raw = (el.get("datetime") or "") if el else ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def discover(session):
    found: dict[str, Candidate] = {}

    for page in range(NEWS_PAGES):
        r = get_with_retry(session, NEWS, params={"page": page} if page else None)
        soup = BeautifulSoup(r.text, "html.parser")
        for article in soup.select("article.ecl-content-item"):
            link = article.select_one("a[data-ecl-title-link][href]")
            if not link:
                continue
            title = " ".join(link.get_text(" ", strip=True).split())
            if not title or not CARBON_RE.search(title):
                continue
            url = urljoin(r.url, link["href"])
            if "climate.ec.europa.eu" not in url:
                continue
            date = _iso(article.find("time"))
            slug = url.split("?", 1)[0].strip("/").split("/")[-1]
            c = Candidate(f"dgclima-news-{slugify(slug.removesuffix('_en'))}", title, date, url)
            found.setdefault(c.source_id, c)

    # CLIMA initiatives (ETS review, ETS2, MSR...) via the Better Regulation API.
    try:
        r = get_with_retry(session, BRP_SEARCH, params={
            "topic": "CLIMA", "language": "EN", "page": 0, "size": 20})
        content = (((r.json() or {}).get("initiativeResultDtoPage") or {}).get("content")) or []
        for item in content:
            iid = item.get("id")
            title = " ".join(str(item.get("shortTitle") or "").split())
            if iid is None or not title:
                continue
            iid = int(float(iid))
            url = f"https://ec.europa.eu/info/law/better-regulation/have-your-say/initiatives/{iid}"
            c = Candidate(f"dgclima-initiative-{iid}", title, None, url)
            found.setdefault(c.source_id, c)
    except (CollectorError, ValueError, TypeError):
        pass

    if not found:
        raise CollectorError("DG CLIMA discovery returned no candidates")
    return NEWS, list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    m = re.search(r"/initiatives/(\d+)", candidate.url)
    if m:
        r = get_with_retry(session, f"{BRP_DETAIL}/{m.group(1)}", params={"language": "EN"}, timeout=45)
        try:
            data = r.json()
        except ValueError as exc:
            raise CollectorError(f"BRP API invalid JSON for initiative {m.group(1)}: {exc}")
        if data.get("shortTitle"):
            candidate.title = " ".join(str(data["shortTitle"]).split())
        parts = []
        for key, label in (("reference", "Reference"), ("unit", "Unit"),
                           ("shortTitle", "Initiative"), ("dossierSummary", "Summary")):
            if data.get(key):
                parts.append(f"{label}: {data[key]}")
        for pub in data.get("publications") or []:
            bits = []
            for key, label in (("type", "Type"), ("stage", "Stage"), ("title", "Title"),
                               ("publishedDate", "Published"), ("endDate", "End date"),
                               ("receivingFeedbackStatus", "Feedback status"),
                               ("feedbackPeriod", "Feedback period"),
                               ("consultationObjective", "Consultation objective")):
                v = pub.get(key)
                if v not in (None, "", [], {}):
                    bits.append(f"{label}: {v}")
            if bits:
                parts.append("\n".join(bits))
        return "\n\n".join(parts)[:MAX_CONTENT_CHARS]

    r = get_with_retry(session, candidate.url, timeout=45)
    soup = BeautifulSoup(r.text, "html.parser")
    h1 = soup.find("h1")
    if h1:
        title = " ".join(h1.get_text(" ", strip=True).split())
        if len(title) > 8:
            candidate.title = title
    main = soup.find("main") or soup
    return html_to_text(main)
