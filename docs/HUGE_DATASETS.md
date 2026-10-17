# Huge datasets: 50 billion OpenTelemetry log rows, queried in place

For the *Working with Huge Datasets* award we query the OpenTelemetry Parquet logs
from the brief directly on public S3: 50 files × 1 billion rows, one day
(2025-09-23), 1,887 GB compressed. Nothing is downloaded up front. This follows
the organisers' Q&A: a view over `read_parquet('https://…')` plus an inverted index
on the view, streamed from the source, with only the index stored locally.

Measured evidence (SQL, scope, time, bytes downloaded): [`evidence/huge_datasets.md`](../evidence/huge_datasets.md).
Query functions: [`log_analysis/otel_queries.py`](../log_analysis/otel_queries.py).

## Setup

```bash
docker compose up -d                       # SereneDB (see main README)
.venv/bin/python scripts/load_otel.py      # ~15 s catalog + ~40 s index (cold)
.venv/bin/python scripts/verify_otel.py    # regenerate evidence/huge_datasets.md
```

`load_otel.py` options: `--parts 0-9` (catalog fewer files), `--index-minutes 2`
(bigger local index, ~33M rows and ~2 GB disk per minute), `--index-minutes 0`
(remote queries only), `--threads` / `--memory-limit` (index build memory).

## How it works: three access paths

| Path | What it covers | Cost | Used for |
|---|---|---|---|
| **Catalog** (`otel_row_groups`) | All 50 files: 81,209 row groups with row range, time range, compressed bytes, read from the Parquet footers only | 15 s once, then ms | Volume trends over the whole day, planning every remote query |
| **Remote** (`read_parquet` on S3) | Any window up to 10 min (600 row groups ≈ 370M rows) | Bandwidth-bound: ~2 MB and ~0.1 s per row group for the small columns | Error rates, counts by service, error trends, top messages |
| **Local index** (`otel_logs_idx`) | One minute of part 0: 32.6M rows | Built in ~40 s; ~2 GB disk | Full-text search on `Body`, instant counts |

A time window becomes a `file_row_number` range via the catalog (the files are
sorted by time; a row group is ~1 s and ~614k rows), so the reader fetches only
those row groups and only the columns the query uses. Windows are therefore
resolved to ~1 s; every result states the exact covered range and row count.

## Measured results (cold server, fresh windows each time)

| Question | Path | Data covered | Time | Downloaded |
|---|---|---|---|---|
| Log volume per hour, whole day | catalog | 50,000,000,000 rows | 5 ms | 0 MB |
| Error share per service, 5 min | remote | 171M rows, 278 row groups | 38 s | 667 MB |
| Error rows by service, 8 min | remote | 270M rows, 439 row groups | 64 s | 1,074 MB |
| Payment errors per minute, 10 min | remote | 362M rows, 587 row groups | 86 s | 1,528 MB |
| Error trend across two files, 3 min | remote | ~100M rows, 2 files | 23 s | 375 MB |
| Top error messages (reads `Body`), 30 s | remote | 20.5M rows, 33 row groups | 16 s | 282 MB |
| Full-text search, phrase | index | 32.6M rows | 0.4–1.4 s | 0–7 MB |
| Service/severity breakdown | index | 32.6M rows | 0.3 s | 0 MB |
| Context around a row / one trace | remote | a few rows | 0.1–1.1 s | 0–13 MB |

The evidence run touches about 1 billion rows remotely and 65 million through or
next to the index; the catalog spans all 50 billion.

### Index vs. remote scan (the brief's optional trade-off)

Same question, same 32.6M rows: *how many rows contain "failed to charge card"?*

| Method | Time | Downloaded | Matches |
|---|---|---|---|
| Local index (`@@` phrase) | 0.38 s | 0 MB | 1,768,474 |
| Remote scan of `Body` (`ILIKE`) | 22.7 s | 417 MB | 1,768,474 |
| One-off index build | 39 s | `Body` of 53 row groups | — |

The index pays for itself after about 2 repeated questions on the same slice.
Remote queries stay the right tool for one-off aggregates over larger windows.

## What the data shows

The data is from the OpenTelemetry demo shop with injected failures, and the
pattern is stable across the day (every sampled hour):

- `payment` logs ~39% error rows and `frontend` ~36% (04:00–04:05: 2.41M of 6.21M
  and 18.6M of 51.5M rows), while every other service stays below 3%.
- Payment errors are flat at ~510k per minute (22:00–22:10), so this is a constant
  failure rate, not a spike.
- The dominant messages are `failed to charge card … Visa cache full` and
  `Failed to place order`, followed by `failed to convert shipping cost to currency`.
- Counts are log rows, not failed orders: one failed checkout writes several rows
  (one trace in the evidence has 3 error rows from `frontend` for one failure).

## SereneDB 26.09.1.3 pitfalls we hit, and the workarounds

1. **Filtering on the Parquet `Timestamp` column (`TIMESTAMPTZ_NS`) crashes the
   server** (SIGSEGV, container restarts). Workaround: never filter on it; time
   windows become `file_row_number` ranges from the catalog.
2. **Any other `WHERE` condition on the remote file re-downloads row groups over
   and over**: 2 row groups with `WHERE SeverityText = 'error'` took 180 s and
   3.3 GB, against 1.4 s and 11 MB when the same condition sits in
   `count(*) FILTER (WHERE …)`. Workaround: remote `WHERE` holds only the row range;
   service, severity and trace conditions go into `FILTER` / `CASE` / `HAVING`.
3. **Reading the nanosecond `Timestamp` column is expensive** (a 5-minute trend
   took 95 s). Workaround: trends read only `SeverityText`/`ServiceName`, group rows
   in blocks of 4,096 and place each block in time by interpolating within its
   row group's min/max timestamp from the catalog (5 min: about 1 s warm; per-minute
   counts within ~0.1% of the exact ones).
4. **Index build memory**: with the defaults (4 threads, 6.1 GiB limit) a 1-minute
   index with stored `Body` grew past the 7.7 GiB Docker VM and was OOM-killed.
   `SET threads = 2` and `memory_limit = '3GB'` (the loader defaults) peaked at 2 GiB.
5. **Indexed columns are postings only**: returning `ts`, `ServiceName` etc. from a
   view index re-read them from S3 (a cold breakdown: 14 s, 119 MB). `INCLUDE`
   stores them in the index (now 0.3 s, 0 MB).

## Limits

- Remote reads are bound by the network (~18 MB/s here): about 6–8 s per minute of
  telemetry for the small columns. Windows are capped at 10 minutes (60 row groups,
  ~1 min, when `Body` is read); `plan_window` refuses larger ones before reading.
- The local index covers one minute of part 0. Larger slices need ~2 GB disk and
  ~40 s per minute; the whole day would be ~2.9 TB, which is beyond a laptop.
- The larger Loghub datasets (Thunderbird, Windows, …) are published only as
  `.zip` / `.tar.gz` archives on Zenodo, which cannot be read in place; they would
  need a download and a loader like BGL's.
