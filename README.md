# Telecom Core / Backbone IP Ranges

A completely free, open-source project for building a database of IP ranges belonging to
the **core / backbone infrastructure** of telecom operators in:
- Southeast Asia
- Central Asia
- Africa
- Eastern Europe
- Isolated islands (Pacific, Caribbean, Indian Ocean)
- South America

It **deliberately excludes** end-user ranges (Residential / Broadband / CGNAT).

## How does the classification work?

There is no API that classifies an IP address directly as "Core" or "User". So the project
relies on classifying at the **ASN (Autonomous System)** level first, via PeeringDB (type
`NSP`/`Route Server`, or keywords in the network name such as
backbone/transit/carrier), then fetches only the prefixes of the ASNs accepted as
infrastructure. Each ASN gets a 0-100 confidence score rather than a pure yes/no —
stronger evidence (a matching PeeringDB type, a matching keyword, or both) scores higher;
see "Output structure" below.

## Data sources used (all free, no API key required)
- [RIPEstat API](https://stat.ripe.net/docs/data_api) — ASNs by country + announced prefixes
- [PeeringDB API](https://www.peeringdb.com/apidocs/) — network type classification

## Running locally

```bash
pip install -r requirements.txt

python src/fetch_asns.py --region southeast_asia --resume
python src/classify_asn.py --region southeast_asia --resume
python src/fetch_prefixes.py --region southeast_asia --resume
python src/build_output.py
```

Or run all 4 steps in one command with `run_pipeline.py`:
```bash
python run_pipeline.py --region southeast_asia --resume
```
It stops immediately if any step fails, same as running them one by one and checking
each. GitHub Actions still runs the 4 steps separately (see below) so each stage gets
its own timing/logs in the Actions UI — `run_pipeline.py` is just for convenience when
running by hand, e.g. from Termux on Android.

Use `--region all` to process all regions, or a specific region name (see the file names
in `data/regions/`).

## Reliability

Every network call (RIPEstat and PeeringDB alike) goes through a shared retry helper
(`src/utils/http.py`) that retries connection errors, timeouts, HTTP 429, and HTTP
5xx with exponential backoff, so one slow or overloaded moment doesn't need a full
30-day cache-expiry cycle to be retried. Non-retryable 4xx errors (like 404) fail
immediately instead of wasting retry attempts on something that won't change. The
pacing delay between requests (`rate_limit.*_delay_seconds` in `config.yml`) now always
applies, even when a request fails — it used to be skipped on failure, which let a run
of failing requests blast an API with zero pacing. Prefixes that don't parse as valid
CIDR notation are discarded before they'd reach the output CSVs (`build_output.py`
reports how many, if any). ASN values are normalized through `_clean_asn()` before use —
confirmed against the live RIPEstat API, its lod=1 ("most detailed") response wraps at
least some entries as e.g. `AsnSingle(215040)` instead of a plain `215040`, which broke
every PeeringDB lookup downstream until this was caught and fixed. PeeringDB lookups are
batched (`asn__in`, up to `peeringdb_batch_size` ASNs per request — PeeringDB's own
documented limit is 150) instead of one request per ASN, per PeeringDB's own guidance —
querying one ASN at a time is what triggered their per-IP rate limit (20 req/min
unauthenticated) in production. Name-keyword matching (`name_keywords_accept`) requires
a whole word, not a substring — confirmed-real production data had PeeringDB-labeled
`Cable/DSL/ISP` networks (i.e. explicitly not core, by PeeringDB's own classification)
get accepted only because "ix" or "core" happened to appear inside a longer word
(`MATRONIX`, `NCORE`) before this was fixed.

## Running tests

```bash
python -m unittest discover -s tests -v
```

Covers the classification decision logic, the ASN-parsing helper, the cache/checkpoint
layer, and the min-prefix output filter — all with mocked network calls, so no API access
is needed to run them.

## Automatic run on GitHub

Two separate workflows:
- **`.github/workflows/test.yml`** — runs the test suite on every push/PR. Fast (no
  network calls), so it gives quick feedback on code changes.
- **`.github/workflows/update.yml`** — the actual data pipeline. Runs once a week (or
  manually via the Actions tab), with a 300-minute (5-hour) time limit per run and its
  own test-suite run as a safety gate first — if a test fails, the run stops before
  making any real API calls.

  **A single run loops the fetch → classify → fetch-prefixes → build cycle repeatedly**
  (each pass still respects `max_countries_per_run` / `max_asns_per_run` from
  `config.yml`), committing and pushing progress after every pass, until either nothing
  changed in a pass (everything is covered) or the run gets within its time budget's
  edge (~240 minutes, leaving a buffer under the 300-minute job limit). This means one
  manual "Run workflow" click, or one weekly scheduled run, can make hours of progress
  on its own instead of needing to be re-triggered by hand after every single pass.

## Why doesn't it take forever to run?

- **Persistent cache** in `data/asn_cache/` — queries for the same ASN aren't repeated
  within 30 days (configurable in `config.yml`).
- **Batch processing** — each run processes a limited number of countries/ASNs
  (`config.yml`), then stops safely.
- **Resume system** via `data/checkpoints/` — any later run with `--resume` continues
  from where it left off, without repeating work.
- **No excessive files** — one combined output file + one file per region, not a file
  per country or ASN.

## Output structure

```
output/all_core_ranges.csv          # accepted ASNs, all regions, one row per prefix
output/by_region/<region>.csv       # the same data split by region
output/classification_report.csv    # every evaluated ASN — accepted AND excluded —
                                     # one row each, with its score and reason
```

`all_core_ranges.csv` / `by_region/*.csv` columns:
`region, country, asn, org_name, org_id, info_type, score, prefix`

`classification_report.csv` columns:
`region, country, asn, org_name, org_id, info_type, accepted, score, prefix_count, reason, sources`

`org_id` is PeeringDB's organisation ID — ASNs sharing the same `org_id` belong to the
same company (useful for grouping, e.g. several ASNs that are all really the same
national carrier). `score` is 0-100, not just accept/reject — `accepted` in the main
files is just `score >= 45`, but `classification_report.csv` lets you review anything
below that cutoff yourself, or apply a stricter or looser threshold than the built-in one.

## Note on accuracy

There is no perfect, 100% automatic classification. Some small networks may be
misclassified, and some regional transit providers may not be registered in PeeringDB at
all — those get a low-but-nonzero score (`not_in_peeringdb`, score 10) rather than being
silently indistinguishable from a confirmed non-core network, precisely so you can find
and review them in `classification_report.csv`. Check `config.yml` to adjust the
classification rules (`classification.accept_info_types` and `name_keywords_accept`) to
fit your needs.

## Where this could go next

Classification currently relies on PeeringDB alone. The single highest-value addition
would be [CAIDA ASRank](https://asrank.caida.org/) — a free, academically-maintained API
that ranks ASNs by actual topological importance in the global routing system (based on
customer-cone size), which is a meaningfully different and complementary signal to
PeeringDB's self-reported network type. It isn't wired in yet because it needs testing
against its live API/schema before it's trustworthy to run unattended and weekly — a
good next step, done as its own isolated change with its own tests, the same way the
current scoring logic was built.

## License

MIT — see `LICENSE`. Fill in your name/handle in the copyright line before publishing.
