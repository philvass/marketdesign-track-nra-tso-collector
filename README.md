# MarketDesign.ai TRACK — NRA/TSO multi-source collector

Discovers market-design publications from European NRAs and TSOs, normalises
them to the TRACK `/ingest/document` contract, deduplicates by source_id +
content hash, and submits new/changed documents for Claude analysis and human
editorial review.

## Sources

| Key | Institution | Discovery |
|---|---|---|
| `cre` | CRE (FR NRA) | cre.fr délibérations + consultations listings (server-rendered; first 25 each) |
| `bnetza` | BNetzA (DE NRA) | BK6/BK8 Beschlusskammer listings + electricity press releases |
| `ofgem` | Ofgem (GB NRA) | Drupal JSON listing API, Generation & Wholesale sector, decision/consultation/call-for-input/code-mod types |
| `rte` | RTE (FR TSO) | services-rte.com news JSON API (cookie-primed) + concerte.fr RSS |
| `neso` | NESO (GB SO) | rss.xml firehose (noise-filtered) + code-modification sitemap |
| `german-tsos` | 50Hertz/Amprion/TenneT/TransnetBW | netztransparenz.de + regelleistung.net LotesNewsXSP JSON APIs (anonymous bootstrap login) |
| `jao` | JAO | resource-center rules library + consultations + news RSS (noise-filtered) |
| `nemo` | NEMO Committee | news/publications/consultations listings (full history, single pages) |
| `aib` | AIB (EECS/GO) | news teasers + sitemap lastmod tracking of rules/residual-mix pages |
| `nbm` | Nordic TSOs (NBM) | WordPress REST API (news, publications, consultations, guides) |
| `dgclima` | EC DG CLIMA (EU ETS) | news listing (carbon-keyword filter) + Better Regulation API initiatives |
| `creg` | CREG (BE NRA) | Drupal faceted publications listing, electricity market themes |
| `acm` | ACM (NL NRA) | Drupal search (Energie + Elektriciteit filters; WAF needs Referer + retry) |
| `elexon` | Elexon (GB BSC) | BSC WordPress REST API: mod-proposals (by modified) + consultations + news |
| `arera` | ARERA (IT NRA) | atti-e-provvedimenti listing (Delibera+Consultazione, settore=4, /R/eel+/R/com) |
| `ceer` | CEER | WordPress REST API: electricity publications (excl. national monitoring) + consultations |
| `recs` | RECS International | WP REST `news` CPT (NOT `posts` — spam-compromised) + /documents inline JSON |
| `energinet` | Energinet (DK TSO) | Umbraco FacetedEnerListApi JSON (news + ancillary-services nodes) |
| `tennet` | TenneT (NL/DE TSO) | /news __NEXT_DATA__ JSON (teaser-only: article pages are WAF-blocked) |
| `nordpool` | Nord Pool | exchange-message-list RSS (full bodies inline; UMM/operational feeds never touched) |
| `omie` | OMIE | notas-de-prensa listing (PDFs; monthly price reports excluded) |
| `gme` | GME | electricity news archive (results-noise filtered) + DTF technical rules |
| `epex` | EPEX SPOT | newsroom listing (noise-filtered; 20s pacing + empty-202 WAF guard) |

Deferred: **Elia** (elia.be) — hard Cloudflare JS challenge on every path; needs a
real browser (Playwright) to scrape. Most Elia rule changes surface via CREG
approvals anyway.

## Local usage

```
pip install -r requirements.txt
python core.py --source cre --dry-run --limit 3
python core.py --source ofgem --submit --limit 5
```

Modes: `--dry-run` (default), `--bootstrap-state` (record baseline, submit
nothing), `--submit`. State: `./state/<source>.sqlite3`.

### Freshness gate

TRACK is a monitor, not an archive, so a document first seen more than
`--max-age-days` (default 30) after publication is baselined rather than
submitted. When discovery already knows the publication date, that decision is
made *before* the fetch: the document is recorded as `STALE_SKIPPED_NOT_FETCHED`
with an empty content hash and never requested. The skip is sticky, so it costs
one decision rather than one request per run, which matters on sources that
rate-limit (EPEX serves an empty HTTP 202 once an IP exceeds its budget).

Documents already fetched at least once keep being re-fetched, so change
detection on anything in TRACK is unaffected. An adapter whose `fetch_content`
may overwrite a publication date supplied by discovery must set
`DATE_REFINED_ON_FETCH = True` to opt out of the pre-fetch skip (currently only
`acm`).

### Content guards

Before submission, `core.py` drops any extraction shorter than 200 characters
or one that `looks_like_binary_text` flags — a PDF, ZIP or Office file served
from a URL that looks like a page and therefore parsed as HTML. Such documents
appear in the run report as `fetch_error: Binary payload extracted as text`.
Adapters should route on `looks_like_pdf(response)` rather than the URL suffix,
since several sources serve PDFs from plain content paths.

## Production behavior (GitHub Actions)

- Matrix job over all six sources every 6 hours (minute 37), max 2 in parallel.
- Scheduled runs only execute when repo variable `TRACK_AUTOMATION_ENABLED=true`.
- SQLite dedupe state persists via the Actions cache (`state-<source>-*`).
- Run reports are retained as artifacts for 14 days.
- `workflow_dispatch` accepts mode (dry-run/submit/bootstrap), a single source
  or `all`, and a per-source limit.

## Safe first activation

1. Dispatch mode=**submit**, limit=**2** — seeds TRACK with the 2 newest
   documents per source for immediate editorial review.
2. Dispatch mode=**bootstrap**, limit=**100** — records everything else
   currently discoverable as baseline; nothing submitted.
3. Dispatch mode=**submit**, limit=**25** — expected: all duplicates, 0 submitted.
4. Set `TRACK_AUTOMATION_ENABLED=true`. The 6-hour schedule is live.

Do not bootstrap after monitoring is live: bootstrap marks currently
discovered documents as already handled.

## Config

- Variable `TRACK_INGEST_URL` — TRACK ingest endpoint (default in core.py).
- Variable `TRACK_AUTOMATION_ENABLED` — must be exactly `true` for scheduled runs.
- Secret `TRACK_INGEST_TOKEN` — optional bearer token if ingestion is protected.
