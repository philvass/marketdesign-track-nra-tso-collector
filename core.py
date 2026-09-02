#!/usr/bin/env python3
"""MarketDesign.ai TRACK V21 — multi-source NRA/TSO collector core.

Shared machinery for discovering, normalising, deduplicating and submitting
primary-source documents to the TRACK /ingest/document contract. Each source
(institution) lives in sources/<key>.py and provides:

  INSTITUTION: str            — institution name sent to TRACK
  DOCUMENT_TYPE: str          — TRACK document_type (e.g. "REGULATOR", "TSO")
  discover(session) -> (discovery_url, list[Candidate])
  fetch_content(session, candidate) -> str
  is_out_of_scope(candidate) -> bool     (optional)
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

import requests
from pypdf import PdfReader

VERSION = "v21.0-nra-tso-automation-1"
DEFAULT_TRACK_URL = "https://marketdesign.ai/ingest/document"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151 Safari/537.36 "
    "MarketDesign.ai-TRACK/21.0"
)
MAX_CONTENT_CHARS = 60000


@dataclass
class Candidate:
    source_id: str
    title: str
    publication_date: str | None
    url: str


class CollectorError(RuntimeError):
    pass


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9,fr;q=0.8,de;q=0.8",
        "Cache-Control": "no-cache",
    })
    return s


def get_with_retry(session: requests.Session, url: str, timeout: int = 30,
                   attempts: int = 3, **kwargs) -> requests.Response:
    last: Exception | None = None
    for n in range(attempts):
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True, **kwargs)
            if r.status_code >= 500 and n + 1 < attempts:
                time.sleep(1.5 * (n + 1))
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            last = exc
            if n + 1 < attempts:
                time.sleep(1.5 * (n + 1))
    raise CollectorError(f"GET failed for {url}: {last}")


def slugify(text: str, max_len: int = 80) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug)[:max_len].strip("-")


def extract_pdf_text(pdf_bytes: bytes, max_chars: int = MAX_CONTENT_CHARS) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if reader.is_encrypted:
            reader.decrypt("")
    except Exception as exc:
        raise CollectorError(f"PDF could not be parsed: {exc}")
    parts: list[str] = []
    size = 0
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            continue
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not text:
            continue
        remaining = max_chars - size
        if remaining <= 0:
            break
        parts.append(text[:remaining])
        size += len(parts[-1])
    return "\n\n".join(parts).strip()


def html_to_text(soup) -> str:
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    return "\n".join(
        line.strip() for line in soup.get_text("\n").splitlines() if line.strip()
    )[:MAX_CONTENT_CHARS]


def build_payload(source, candidate: Candidate, content: str) -> dict:
    return {
        "institution": source.INSTITUTION,
        "document_type": source.DOCUMENT_TYPE,
        "url": candidate.url,
        "publication_date": candidate.publication_date,
        "title": candidate.title,
        "content": content,
        "source_id": candidate.source_id,
    }


def content_hash(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS acquired_documents (
          source_id TEXT PRIMARY KEY,
          content_hash TEXT NOT NULL,
          source_url TEXT NOT NULL,
          publication_date TEXT,
          title TEXT NOT NULL,
          track_disposition TEXT,
          track_response_json TEXT,
          first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          submitted_at TEXT
        )
        """
    )
    db.commit()
    return db


def is_unchanged(db: sqlite3.Connection, source_id: str, digest: str) -> bool:
    row = db.execute(
        "SELECT content_hash, submitted_at FROM acquired_documents WHERE source_id=?",
        (source_id,),
    ).fetchone()
    return bool(row and row[0] == digest and row[1])


def record_seen(db: sqlite3.Connection, candidate: Candidate, digest: str) -> None:
    db.execute(
        """
        INSERT INTO acquired_documents(source_id,content_hash,source_url,publication_date,title)
        VALUES(?,?,?,?,?)
        ON CONFLICT(source_id) DO UPDATE SET
          content_hash=excluded.content_hash,
          source_url=excluded.source_url,
          publication_date=excluded.publication_date,
          title=excluded.title,
          last_seen_at=CURRENT_TIMESTAMP,
          submitted_at=CASE
            WHEN acquired_documents.content_hash=excluded.content_hash THEN acquired_documents.submitted_at
            ELSE NULL
          END
        """,
        (candidate.source_id, digest, candidate.url, candidate.publication_date, candidate.title),
    )
    db.commit()


def submit(session: requests.Session, track_url: str, payload: dict, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = session.post(track_url, json=payload, headers=headers, timeout=300)
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:2000]}
    if not r.ok:
        raise CollectorError(f"TRACK returned HTTP {r.status_code}: {json.dumps(data, ensure_ascii=False)}")
    return data


def mark_submitted(db: sqlite3.Connection, source_id: str, response: dict) -> None:
    disposition = response.get("disposition") if isinstance(response, dict) else None
    db.execute(
        """
        UPDATE acquired_documents
        SET track_disposition=?, track_response_json=?, submitted_at=CURRENT_TIMESTAMP, last_seen_at=CURRENT_TIMESTAMP
        WHERE source_id=?
        """,
        (disposition, json.dumps(response, ensure_ascii=False), source_id),
    )
    db.commit()


def mark_bootstrapped(db: sqlite3.Connection, source_id: str) -> None:
    db.execute(
        """
        UPDATE acquired_documents
        SET track_disposition='BOOTSTRAPPED', submitted_at=CURRENT_TIMESTAMP, last_seen_at=CURRENT_TIMESTAMP
        WHERE source_id=?
        """,
        (source_id,),
    )
    db.commit()


def choose(candidates: Iterable[Candidate], match: str | None, limit: int) -> list[Candidate]:
    items = list(candidates)
    if match:
        needle = match.lower()
        items = [c for c in items if needle in c.title.lower() or needle in c.source_id.lower()]
    items.sort(key=lambda c: (c.publication_date or "0000-00-00", c.source_id), reverse=True)
    return items[:limit]


def load_source(key: str):
    try:
        return importlib.import_module(f"sources.{key}")
    except ModuleNotFoundError as exc:
        raise CollectorError(f"Unknown source {key!r}: {exc}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="MarketDesign.ai TRACK multi-source NRA/TSO collector")
    p.add_argument("--source", required=True, help="Source key, e.g. cre, bnetza, ofgem, rte, neso, german-tsos")
    p.add_argument("--submit", action="store_true", help="POST new/changed documents into TRACK")
    p.add_argument("--dry-run", action="store_true", help="Discover + fetch + normalise, but never submit")
    p.add_argument("--bootstrap-state", action="store_true", help="Record current documents as baseline without submitting")
    p.add_argument("--limit", type=int, default=1, help="Maximum documents to process (default: 1)")
    p.add_argument("--match", help="Only process candidates whose title/source_id contains this text")
    p.add_argument("--track-url", default=os.getenv("TRACK_INGEST_URL", DEFAULT_TRACK_URL))
    p.add_argument("--token", default=os.getenv("TRACK_INGEST_TOKEN"))
    p.add_argument("--state", default=None, help="SQLite state path (default ./state/<source>.sqlite3)")
    p.add_argument("--json", action="store_true", help="Emit machine-readable summary JSON")
    args = p.parse_args(argv)

    selected_modes = sum(bool(x) for x in (args.submit, args.dry_run, args.bootstrap_state))
    if selected_modes > 1:
        p.error("choose only one of --submit, --dry-run, or --bootstrap-state")
    if selected_modes == 0:
        args.dry_run = True

    source_key = args.source.replace("-", "_")
    source = load_source(source_key)
    state_path = Path(args.state or os.getenv("COLLECTOR_STATE") or f"./state/{args.source}.sqlite3")

    session = make_session()
    db = init_db(state_path)
    discovery_url, discovered = source.discover(session)
    selected = choose(discovered, args.match, max(1, args.limit))
    if not selected:
        raise CollectorError(f"No {source.INSTITUTION} candidate matched {args.match!r}")

    out_of_scope = getattr(source, "is_out_of_scope", lambda c: False)

    results = []
    for candidate in selected:
        try:
            if out_of_scope(candidate):
                results.append({
                    "candidate": asdict(candidate),
                    "skipped": True,
                    "skip_reason": "out_of_scope",
                    "submitted": False,
                    "track_response": None,
                })
                continue

            content = source.fetch_content(session, candidate)
            if len(content) < 200:
                raise CollectorError(f"Too little source text extracted from {candidate.url}")
        except Exception as exc:
            results.append({
                "candidate": asdict(candidate),
                "skipped": True,
                "skip_reason": f"fetch_error: {exc}",
                "submitted": False,
                "track_response": None,
            })
            continue

        payload = build_payload(source, candidate, content)
        digest = content_hash(payload)
        duplicate = is_unchanged(db, candidate.source_id, digest)
        record_seen(db, candidate, digest)
        item = {
            "candidate": asdict(candidate),
            "content_chars": len(content),
            "content_hash": digest,
            "duplicate": duplicate,
            "submitted": False,
            "track_response": None,
        }
        if args.bootstrap_state:
            if not duplicate:
                mark_bootstrapped(db, candidate.source_id)
            item["bootstrapped"] = not duplicate
        elif args.submit and not duplicate:
            response = submit(session, args.track_url, payload, args.token)
            mark_submitted(db, candidate.source_id, response)
            item["submitted"] = True
            item["track_response"] = response
            time.sleep(2.0)
        results.append(item)

    summary = {
        "ok": True,
        "collector_version": VERSION,
        "source": args.source,
        "institution": source.INSTITUTION,
        "mode": "BOOTSTRAP" if args.bootstrap_state else ("SUBMIT" if args.submit else "DRY_RUN"),
        "discovery_source": discovery_url,
        "discovered_count": len(discovered),
        "processed_count": len(results),
        "results": results,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"{VERSION} | {args.source} | {summary['mode']}")
        print(f"Discovery: {discovery_url}")
        print(f"Discovered: {len(discovered)} | Processed: {len(results)}")
        for r in results:
            c = r["candidate"]
            print(f"- {c['source_id']} | {c['publication_date']} | {c['title']}")
            print(f"  source: {c['url']}")
            if r.get("skipped"):
                print(f"  SKIPPED: {r['skip_reason']}")
                continue
            print(f"  chars: {r['content_chars']} | hash: {r['content_hash'][:16]}… | duplicate: {r['duplicate']}")
            if r["submitted"]:
                print(f"  TRACK: {json.dumps(r['track_response'], ensure_ascii=False)[:400]}")
        if args.bootstrap_state:
            print("BOOTSTRAP: baseline recorded locally; nothing was submitted to TRACK.")
        elif args.dry_run:
            print("DRY RUN: nothing was submitted to TRACK.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "collector_version": VERSION, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
