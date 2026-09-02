"""TenneT (tennet.eu) — Dutch/German TSO.

Discovery: /news is the only path TenneT's Cloudflare WAF serves to
non-browsers, but its __NEXT_DATA__ JSON embeds all news items with teaser,
themes and timestamps. Article pages are bot-blocked, so the teaser (plus
title/themes/keywords) is the submitted content — enough for the TRACK
relevance gate and resolver; editors follow the link for the full text.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from core import Candidate, CollectorError, get_with_retry, slugify, MAX_CONTENT_CHARS

INSTITUTION = "TenneT"
DOCUMENT_TYPE = "TSO"

BASE = "https://www.tennet.eu"
NEWS = f"{BASE}/news"
MAX_ITEMS = 30

_META_CACHE: dict[str, dict] = {}


def _epoch_ms_date(raw) -> str | None:
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def discover(session):
    r = get_with_retry(session, NEWS, timeout=45)
    soup = BeautifulSoup(r.text, "html.parser")
    script = soup.select_one("script#__NEXT_DATA__")
    if not script or not script.string:
        raise CollectorError("TenneT /news has no __NEXT_DATA__ payload")
    try:
        data = json.loads(script.string)
        edges = data["props"]["pageProps"]["news"]["edges"]
    except (ValueError, KeyError, TypeError) as exc:
        raise CollectorError(f"TenneT __NEXT_DATA__ shape changed: {exc}")

    found: dict[str, Candidate] = {}
    items = []
    for edge in edges:
        node = (edge or {}).get("node") or {}
        items.append(node)
    items.sort(key=lambda n: n.get("publishedAt") or 0, reverse=True)

    for node in items[:MAX_ITEMS]:
        path = node.get("path") or ""
        title = " ".join(str(node.get("title") or "").split())
        if not path or not title:
            continue
        url = urljoin(BASE, path)
        slug = path.strip("/").split("/")[-1]
        sid = f"tennet-{slugify(slug)}"
        _META_CACHE[sid] = node
        found.setdefault(sid, Candidate(sid, title, _epoch_ms_date(node.get("publishedAt")), url))

    if not found:
        raise CollectorError("TenneT discovery returned no candidates")
    return NEWS, list(found.values())


def fetch_content(session, candidate: Candidate) -> str:
    node = _META_CACHE.get(candidate.source_id) or {}
    parts = [candidate.title]
    desc = " ".join(str(node.get("description") or "").split())
    if desc:
        parts.append(desc)
    for key, label in (("newsType", "Type"), ("themes", "Themes"),
                       ("keywords", "Keywords"), ("tags", "Tags")):
        v = node.get(key)
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v if x)
        if v:
            parts.append(f"{label}: {v}")
    updated = _epoch_ms_date(node.get("updatedAt"))
    if updated:
        parts.append(f"Last updated: {updated}")
    parts.append(
        "Note: TenneT article pages are served only to browsers; this record "
        "carries the official teaser. Full text at the source URL."
    )
    text = "\n\n".join(parts)
    if len(text) < 200:
        text = text + "\n" + re.sub(r"\s+", " ", json.dumps(node)[:500])
    return text[:MAX_CONTENT_CHARS]


def is_out_of_scope(candidate: Candidate) -> bool:
    t = candidate.title.lower()
    return any(x in t for x in ("half-year", "annual report", "financial", "appoint", "vacanc", "hydrogen"))
