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

## Local usage

```
pip install -r requirements.txt
python core.py --source cre --dry-run --limit 3
python core.py --source ofgem --submit --limit 5
```

Modes: `--dry-run` (default), `--bootstrap-state` (record baseline, submit
nothing), `--submit`. State: `./state/<source>.sqlite3`.

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
