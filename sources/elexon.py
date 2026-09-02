"""Elexon (elexon.co.uk) — GB Balancing and Settlement Code administrator.

Discovery: the BSC subsite's open WordPress REST API for modification
proposals (ordered by last modified, so lifecycle progress resubmits via the
content hash) and consultations, plus main-site news posts. Detail pages are
server-rendered; mod pages carry the lifecycle phase.
"""
from __future__ import annotations

from datetime import datetime

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, html_to_text, slugify, MAX_CONTENT_CHARS

INSTITUTION = "Elexon"
DOCUMENT_TYPE = "MARKET_OPERATOR"

BSC_API = "https://www.elexon.co.uk/bsc/wp-json/wp/v2"
MAIN_API = "https://www.elexon.co.uk/wp-json/wp/v2"


def _date(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date().isoformat()
    except ValueError:
        return None


def _wp_items(session, url, params, prefix, title_prefix=""):
    out = []
    try:
        r = get_with_retry(session, url, params=params, timeout=45)
        posts = r.json()
    except (CollectorError, ValueError):
        return out
    for post in posts if isinstance(posts, list) else []:
        slug = post.get("slug") or ""
        link = post.get("link") or ""
        title = " ".join(BeautifulSoup(
            (post.get("title") or {}).get("rendered") or "", "html.parser"
        ).get_text(" ", strip=True).split())
        if not slug or not link or not title:
            continue
        date = _date(post.get("modified_gmt") or post.get("date_gmt"))
        out.append(Candidate(f"{prefix}-{slugify(slug)}", f"{title_prefix}{title}", date, link))
    return out


def discover(session):
    found: dict[str, Candidate] = {}

    for c in _wp_items(session, f"{BSC_API}/mod-proposal",
                       {"per_page": 25, "orderby": "modified", "order": "desc"},
                       "elexon-mod", "BSC Modification "):
        found.setdefault(c.source_id, c)
    for c in _wp_items(session, f"{BSC_API}/consultation",
                       {"per_page": 15, "orderby": "modified", "order": "desc"},
                       "elexon-consultation", "BSC Consultation: "):
        found.setdefault(c.source_id, c)
    for c in _wp_items(session, f"{MAIN_API}/posts",
                       {"per_page": 15}, "elexon-news"):
        found.setdefault(c.source_id, c)

    if not found:
        raise CollectorError("Elexon discovery returned no candidates")
    return f"{BSC_API} (mods + consultations) + news", list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    r = get_with_retry(session, candidate.url, timeout=45)
    soup = BeautifulSoup(r.text, "html.parser")

    # Current lifecycle phase on modification pages.
    phase = soup.select_one("div.current-status .list-item.current .list-item-label")
    phase_text = f"Current phase: {phase.get_text(' ', strip=True)}\n\n" if phase else ""

    body = soup.find("article") or soup.find("main") or soup
    return (phase_text + html_to_text(body))[:MAX_CONTENT_CHARS]


def is_out_of_scope(candidate: Candidate) -> bool:
    t = candidate.title.lower()
    return any(x in t for x in ("vacanc", "careers", "webinar recording", "annual report and accounts"))
