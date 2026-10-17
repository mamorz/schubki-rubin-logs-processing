#!/usr/bin/env python3
"""Set up the OpenTelemetry Parquet dataset (50 x 1 billion rows on public S3) for querying in place.

    .venv/bin/python scripts/load_otel.py                       # catalog all 50 parts, index first minute of part 0
    .venv/bin/python scripts/load_otel.py --parts 0-9           # catalog parts 0..9 only
    .venv/bin/python scripts/load_otel.py --index-minutes 1     # smaller/larger local full-text index
    .venv/bin/python scripts/load_otel.py --index-minutes 0     # catalog only, no index

Nothing is downloaded up front. Two things are created:

1. otel_row_groups: one row per Parquet row group (from the files' footers only):
   part, row range, time range, compressed bytes. Queries use it to turn a time window
   into a file_row_number range, so the reader fetches just those row groups.
   (Filtering on the Parquet Timestamp column itself, TIMESTAMPTZ_NS, crashes
   SereneDB 26.09.1.3 with SIGSEGV; filtering on file_row_number does not.)

2. otel_logs (view) + otel_logs_idx (inverted index) over a time slice of one part.
   The index is streamed from S3 and stored locally; Body is searchable with @@,
   other columns are materialized live from S3 by row number.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from log_analysis import otel_queries as oq  # noqa: E402
from verify_queries import connect  # noqa: E402


def parse_parts(spec: str) -> list[int]:
    parts: set[int] = set()
    for chunk in spec.split(","):
        a, _, b = chunk.partition("-")
        parts.update(range(int(a), int(b or a) + 1))
    if not parts or min(parts) < 0 or max(parts) >= oq.PART_COUNT:
        raise SystemExit(f"--parts must be within 0-{oq.PART_COUNT - 1}")
    return sorted(parts)


def build_catalog(conn, parts: list[int]) -> None:
    conn.execute("DROP TABLE IF EXISTS otel_row_groups")
    conn.execute("""
        CREATE TABLE otel_row_groups (
            part         INTEGER     NOT NULL, -- 0..49, file part_NNN.parquet
            row_group_id INTEGER     NOT NULL,
            first_row    BIGINT      NOT NULL, -- file_row_number of the group's first row
            num_rows     BIGINT      NOT NULL,
            ts_min       TIMESTAMPTZ NOT NULL, -- Timestamp column statistics
            ts_max       TIMESTAMPTZ NOT NULL,
            body_bytes   BIGINT      NOT NULL, -- compressed size of the Body column chunk
            total_bytes  BIGINT      NOT NULL  -- compressed size of all column chunks
        )""")
    # parquet_metadata takes a literal list; URLs are built from validated part numbers only.
    for i in range(0, len(parts), 10):
        batch = parts[i:i + 10]
        urls = ", ".join(f"'{oq.part_url(p)}'" for p in batch)
        started = time.perf_counter()
        conn.execute(f"""
            INSERT INTO otel_row_groups
            WITH m AS (SELECT * FROM parquet_metadata([{urls}])),
            g AS (
                SELECT file_name, row_group_id, any_value(row_group_num_rows) AS num_rows,
                       max(stats_min) FILTER (WHERE path_in_schema = 'Timestamp') AS ts_min,
                       max(stats_max) FILTER (WHERE path_in_schema = 'Timestamp') AS ts_max,
                       sum(total_compressed_size) FILTER (WHERE path_in_schema = 'Body') AS body_bytes,
                       sum(total_compressed_size) AS total_bytes
                FROM m GROUP BY 1, 2
            )
            SELECT regexp_extract(file_name, 'part_(\\d+)\\.parquet', 1)::INTEGER,
                   row_group_id,
                   sum(num_rows) OVER (PARTITION BY file_name ORDER BY row_group_id) - num_rows,
                   num_rows, ts_min::TIMESTAMPTZ, ts_max::TIMESTAMPTZ, body_bytes, total_bytes
            FROM g""")
        print(f"catalog parts {batch[0]}-{batch[-1]}: {time.perf_counter() - started:.1f} s", flush=True)
    rows = conn.execute("SELECT count(DISTINCT part), count(*), sum(num_rows)::BIGINT, sum(total_bytes)::DOUBLE, "
                        "min(ts_min), max(ts_max) FROM otel_row_groups").fetchone()
    print(f"catalog: {rows[0]} parts, {rows[1]:,} row groups, {rows[2]:,} log rows, "
          f"{rows[3] / 1e9:,.1f} GB compressed, {rows[4]} .. {rows[5]}")


def build_index(conn, minutes: float, threads: int, memory_limit: str) -> None:
    conn.execute("DROP INDEX IF EXISTS otel_logs_idx")
    conn.execute("DROP VIEW IF EXISTS otel_logs")
    conn.execute("DROP TABLE IF EXISTS otel_index_slice")
    if minutes <= 0:
        print("index: skipped (--index-minutes 0)")
        return
    part = oq.INDEX_PART
    last = conn.execute(
        "SELECT max(first_row + num_rows) FROM otel_row_groups "
        "WHERE part = %s AND ts_max <= (SELECT min(ts_min) FROM otel_row_groups WHERE part = %s) + to_seconds(%s)",
        (part, part, int(minutes * 60))).fetchone()[0]
    if last is None:
        raise SystemExit(f"part {part} is not in the catalog; include it in --parts")
    conn.execute("DROP TEXT SEARCH DICTIONARY IF EXISTS otel_dict")
    conn.execute("CREATE TEXT SEARCH DICTIONARY otel_dict (template = 'split_by_non_alpha', case = 'lower', "
                 "frequency = true, position = true)")
    # Plain view over one reader call with a WHERE on the row number: stays on the
    # fast path, so non-indexed columns can be materialized from S3 by row number.
    conn.execute(f"""
        CREATE VIEW otel_logs AS
        SELECT file_row_number, Timestamp::TIMESTAMPTZ AS ts, ServiceName, SeverityText,
               SeverityNumber, TraceId, SpanId, Body
        FROM read_parquet('{oq.part_url(part)}', file_row_number = true)
        WHERE file_row_number < {int(last)}""")
    # With the defaults (4 threads, 6.1 GiB) a 1-minute build with INCLUDE (Body)
    # grew past the 7.7 GiB Docker VM and was OOM-killed; 2 threads / 3GB peaked at 2 GiB.
    conn.execute(f"SET threads = {int(threads)}")
    conn.execute("SELECT set_config('memory_limit', %s, false)", (memory_limit,))
    started = time.perf_counter()
    # Indexed columns hold postings only; returning their values re-reads the rows
    # from S3 (cold: a 5-row search 5-8 s / 43-94 MB, a service/severity breakdown
    # 14 s / 119 MB). INCLUDE stores every returned column in the index itself.
    conn.execute("CREATE INDEX otel_logs_idx ON otel_logs USING inverted "
                 "(file_row_number, ts, ServiceName, SeverityText, SeverityNumber, TraceId, Body otel_dict) "
                 "INCLUDE (ts, ServiceName, SeverityText, TraceId, Body)")
    seconds = time.perf_counter() - started
    conn.execute("""
        CREATE TABLE otel_index_slice AS
        SELECT %s::INTEGER AS part, 0::BIGINT AS first_row, %s::BIGINT AS end_row,
               min(ts_min) AS ts_from, max(ts_max) AS ts_to, sum(num_rows) AS log_rows,
               count(*) AS row_groups, sum(body_bytes) AS body_bytes, %s::DOUBLE AS build_seconds,
               now() AS built_at
        FROM otel_row_groups WHERE part = %s AND first_row < %s""", (part, last, seconds, part, last))
    s = conn.execute("SELECT * FROM otel_index_slice").fetchone()
    print(f"index: part {part}, rows 0..{last - 1:,} ({s[3]} .. {s[4]}), {s[5]:,} log rows, "
          f"{s[6]} row groups, built in {seconds:.1f} s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--parts", default=f"0-{oq.PART_COUNT - 1}", help="parts to catalog, e.g. 0-49 or 0,1,5")
    ap.add_argument("--index-minutes", type=float, default=1,
                    help="minutes of part 0 to index locally, ~33M rows and ~1.8 GB disk per minute (0 = none)")
    ap.add_argument("--skip-catalog", action="store_true", help="reuse the existing otel_row_groups")
    ap.add_argument("--threads", type=int, default=2, help="threads for the index build (fewer = less memory)")
    ap.add_argument("--memory-limit", default="3GB", help="SereneDB memory_limit for the index build")
    args = ap.parse_args()
    with connect() as conn:
        if not args.skip_catalog:
            build_catalog(conn, parse_parts(args.parts))
        build_index(conn, args.index_minutes, args.threads, args.memory_limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
