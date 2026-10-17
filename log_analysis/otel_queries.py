"""Queries over the OpenTelemetry Parquet logs (50 files x 1 billion rows on public S3),
queried in place. Set up with scripts/load_otel.py.

Two access paths:

* Remote: read_parquet on the S3 file, restricted to the row groups that cover the
  requested time window. plan_window() looks the window up in the local
  otel_row_groups catalog and returns file_row_number ranges, so the reader only
  fetches those row groups and only the columns the query uses. Windows are
  resolved at row-group granularity (~1 s, ~600k rows); the exact covered time
  range is reported in the scope.

  Two SereneDB 26.09.1.3 behaviours shape every remote query here:
  - a WHERE on the Parquet Timestamp column (TIMESTAMPTZ_NS) crashes the server
    (SIGSEGV), so time windows are row-number ranges instead;
  - any other WHERE condition on the remote file (e.g. SeverityText = 'error')
    makes the reader re-download the row groups over and over (2 row groups:
    180 s and 3.3 GB instead of 1.4 s and 11 MB). So WHERE holds only the
    file_row_number range, and service/severity/trace conditions are applied
    inside the aggregation (FILTER / CASE / HAVING), which is not pushed down.

* Index: otel_logs_idx, a local inverted index over a time slice of part 0
  (see otel_index_slice). Full-text search on Body and counts come from the index;
  other columns are fetched from S3 by row number.

Every builder returns a queries.Query, so queries.run_query() executes it and adds
the evidence (SQL, filters, totals, truncation, timing). The scope string states how
much data the query covers (rows, row groups, compressed bytes it may read).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .queries import MAX_LIMIT, Query, _limit

PART_COUNT = 50
INDEX_PART = 0
URL = "https://public-pme.s3.eu-west-3.amazonaws.com/text_bench/part_{:03d}.parquet"

# Guard rails for remote reads. Small columns (service, severity) cost ~0.2 MB per
# row group, Body ~5.6 MB. 600 row groups ~ 10 minutes ~ 370M rows.
MAX_ROW_GROUPS = 600
MAX_BODY_ROW_GROUPS = 60

GROUP_KEYS = {
    "service": "ServiceName",
    "severity": "lower(SeverityText)",
    "service_severity": "ServiceName || ' / ' || lower(SeverityText)",
}
# Body -> message pattern: ids, hex, numbers and quoted values become <*>.
PATTERN_EXPR = ("regexp_replace(regexp_replace(left(Body, 300), "
                "'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '<uuid>', 'g'), "
                "'0x[0-9a-fA-F]+|\\b[0-9a-fA-F]*[0-9][0-9a-fA-F]*\\b', '<*>', 'g')")


def part_url(part: int) -> str:
    if isinstance(part, bool) or not isinstance(part, int) or not 0 <= part < PART_COUNT:
        raise ValueError(f"part must be an integer 0..{PART_COUNT - 1}")
    return URL.format(part)


@dataclass
class Segment:
    part: int
    first_row: int
    end_row: int  # exclusive
    row_groups: int
    rows: int
    bytes_total: int
    bytes_body: int


@dataclass
class Window:
    start: str
    end: str
    covered_from: datetime
    covered_to: datetime
    segments: list[Segment]

    @property
    def rows(self) -> int:
        return sum(s.rows for s in self.segments)

    @property
    def row_groups(self) -> int:
        return sum(s.row_groups for s in self.segments)

    def describe(self, body: bool = False) -> str:
        nbytes = sum(s.bytes_body if body else 0 for s in self.segments)
        parts = ", ".join(f"part {s.part} rows {s.first_row:,}..{s.end_row - 1:,}" for s in self.segments)
        extra = f", Body column ≤ {nbytes / 1e6:,.0f} MB compressed" if body else ""
        return (f"otel remote: {self.rows:,} log rows in {self.row_groups} row groups "
                f"({self.covered_from} .. {self.covered_to}; {parts}){extra}")


def plan_window(conn, start: str | datetime, end: str | datetime, *,
                max_row_groups: int = MAX_ROW_GROUPS) -> Window:
    """Resolve [start, end) to the row groups that overlap it, per part."""
    rows = conn.execute(
        "SELECT part, min(first_row), max(first_row + num_rows), count(*), sum(num_rows)::BIGINT, "
        "       sum(total_bytes)::BIGINT, sum(body_bytes)::BIGINT, min(ts_min), max(ts_max) "
        "FROM otel_row_groups WHERE ts_max >= %s::TIMESTAMPTZ AND ts_min < %s::TIMESTAMPTZ "
        "GROUP BY part ORDER BY part", (start, end)).fetchall()
    if not rows:
        raise ValueError("no catalogued data in that window (dataset covers 2025-09-23; "
                         "run scripts/load_otel.py for the parts you need)")
    segs = [Segment(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows]
    w = Window(str(start), str(end), min(r[7] for r in rows), max(r[8] for r in rows), segs)
    if w.row_groups > max_row_groups:
        raise ValueError(f"window covers {w.row_groups} row groups ({w.rows:,} rows); the limit is "
                         f"{max_row_groups}. Narrow the time range.")
    return w


def _remote_union(w: Window, columns: str) -> tuple[str, dict[str, Any]]:
    """UNION ALL of one pruned read_parquet per part. URLs are literals (the reader
    does not accept bound URLs); row bounds are bound parameters. WHERE holds the
    row range only, see the module docstring."""
    selects, params = [], {}
    for i, s in enumerate(w.segments):
        params[f"lo{i}"], params[f"hi{i}"] = s.first_row, s.end_row
        selects.append(f"SELECT {columns} FROM read_parquet('{part_url(s.part)}', file_row_number = true)\n"
                       f"  WHERE file_row_number >= %(lo{i})s AND file_row_number < %(hi{i})s")
    return "\n  UNION ALL\n  ".join(selects), params


def _condition(service: str | None, severity: str | None, params: dict[str, Any]) -> str:
    """Boolean SQL for the optional filters, to use inside FILTER / CASE (never WHERE)."""
    cond = ["TRUE"]
    if service is not None:
        cond.append("ServiceName = %(service)s")
        params["service"] = service
    if severity is not None:
        cond.append("lower(SeverityText) = lower(%(severity)s)")
        params["severity"] = severity
    return " AND ".join(cond)


def _describe_filters(service, severity) -> str:
    f = [f"service = {service!r}" if service else "", f"severity = {severity!r}" if severity else ""]
    return "".join(f", {x}" for x in f if x)


# --------------------------------------------------------------------------- catalog

def otel_overview() -> Query:
    """What is catalogued and indexed: parts, log rows, compressed size, time span."""
    sql = ("SELECT 'catalogued (queryable remotely)' AS scope, count(DISTINCT part) AS parts,\n"
           "       count(*) AS row_groups, sum(num_rows) AS log_rows,\n"
           "       round(sum(total_bytes) / 1e9, 1) AS compressed_gb, min(ts_min) AS ts_from, max(ts_max) AS ts_to\n"
           "FROM otel_row_groups\n"
           "UNION ALL\n"
           "SELECT 'indexed locally (full-text)', 1, row_groups, log_rows, round(body_bytes / 1e9, 2), ts_from, ts_to\n"
           "FROM otel_index_slice")
    return Query("otel_overview", sql, {}, None, "otel: catalog and index metadata")


# --------------------------------------------------------------------------- remote aggregation

def otel_count_by(conn, group_by: str, start, end, *, service: str | None = None,
                  severity: str | None = None, limit: int = 50) -> Query:
    """Log rows per service, severity or service/severity in [start, end), read
    remotely (only the small ServiceName/SeverityText columns)."""
    if group_by not in GROUP_KEYS:
        raise ValueError(f"group_by must be one of {tuple(GROUP_KEYS)}")
    w = plan_window(conn, start, end)
    params: dict[str, Any] = {}
    cond = _condition(service, severity, params)
    union, p = _remote_union(w, "ServiceName, SeverityText")
    params.update(p)
    sql = (f"SELECT {group_by}, log_rows, round(100.0 * log_rows / sum(log_rows) OVER (), 2) AS pct,\n"
           f"       count(*) OVER () AS total_groups\n"
           f"FROM (\n"
           f"  SELECT {GROUP_KEYS[group_by]} AS {group_by}, count(*) FILTER (WHERE {cond}) AS log_rows\n"
           f"  FROM (\n  {union}\n  ) t\n"
           f"  GROUP BY 1\n  HAVING count(*) FILTER (WHERE {cond}) > 0\n) g\n"
           f"ORDER BY log_rows DESC\nLIMIT %(limit)s")
    params["limit"] = _limit(limit)
    return Query("otel_count_by", sql, params, limit, w.describe() + _describe_filters(service, severity))


def otel_error_rates(conn, start, end, *, limit: int = 30) -> Query:
    """Per service: total rows, error/fatal rows, warn rows and error share in [start, end)."""
    w = plan_window(conn, start, end)
    union, params = _remote_union(w, "ServiceName, SeverityText")
    sql = (f"SELECT ServiceName AS service, count(*) AS log_rows,\n"
           f"       count(*) FILTER (WHERE lower(SeverityText) IN ('error', 'fatal')) AS error_rows,\n"
           f"       count(*) FILTER (WHERE lower(SeverityText) IN ('warn', 'warning')) AS warn_rows,\n"
           f"       round(100.0 * count(*) FILTER (WHERE lower(SeverityText) IN ('error', 'fatal')) / count(*), 2) AS error_pct\n"
           f"FROM (\n  {union}\n) t\nGROUP BY 1\nORDER BY error_rows DESC, log_rows DESC\nLIMIT %(limit)s")
    params["limit"] = _limit(limit)
    return Query("otel_error_rates", sql, params, limit, w.describe())


def otel_volume_trend(start=None, end=None, *, bucket: str = "hour", limit: int = MAX_LIMIT) -> Query:
    """Log rows per minute/hour from the catalog alone: no S3 reads, so it covers the
    whole day (50 billion rows) in milliseconds. Each row group (~1 s) is counted
    in the bucket of its first timestamp."""
    if bucket not in ("minute", "hour"):
        raise ValueError("bucket must be 'minute' or 'hour'")
    cond, params, desc = [], {"limit": _limit(limit)}, []
    if start is not None:
        cond.append("ts_max >= %(start)s::TIMESTAMPTZ"); params["start"] = start; desc.append(f"from {start}")
    if end is not None:
        cond.append("ts_min < %(end)s::TIMESTAMPTZ"); params["end"] = end; desc.append(f"to {end}")
    where = ("WHERE " + " AND ".join(cond) + "\n") if cond else ""
    sql = (f"SELECT date_trunc('{bucket}', ts_min) AS bucket, sum(num_rows)::BIGINT AS log_rows,\n"
           f"       count(*) AS row_groups, round(sum(total_bytes) / 1e9, 2) AS compressed_gb\n"
           f"FROM otel_row_groups\n{where}GROUP BY 1\nORDER BY 1\nLIMIT %(limit)s")
    return Query("otel_volume_trend", sql, params, limit,
                 "otel catalog (all catalogued parts, no S3 reads)" + (": " + ", ".join(desc) if desc else "")
                 + f", per {bucket}, row-group granularity")


def otel_trend(conn, start, end, *, bucket: str = "minute", service: str | None = None,
               severity: str | None = None, limit: int = MAX_LIMIT) -> Query:
    """Log rows and error rows per second or minute in [start, end), optionally for
    one service/severity. Reads only ServiceName/SeverityText remotely; rows are
    placed in time through the catalog (blocks of 4,096 rows, a few ms each),
    which avoids reading the expensive nanosecond Timestamp column."""
    if bucket not in ("second", "minute"):
        raise ValueError("bucket must be 'second' or 'minute'")
    w = plan_window(conn, start, end)
    params: dict[str, Any] = {}
    cond = _condition(service, severity, params)
    selects = []
    for i, s in enumerate(w.segments):
        params[f"lo{i}"], params[f"hi{i}"] = s.first_row, s.end_row
        selects.append(
            f"SELECT {s.part} AS part, file_row_number // 4096 AS block,\n"
            f"         count(*) FILTER (WHERE {cond}) AS n,\n"
            f"         count(*) FILTER (WHERE {cond} AND lower(SeverityText) IN ('error', 'fatal')) AS err\n"
            f"  FROM read_parquet('{part_url(s.part)}', file_row_number = true)\n"
            f"  WHERE file_row_number >= %(lo{i})s AND file_row_number < %(hi{i})s\n"
            f"  GROUP BY 1, 2")
    union = "\n  UNION ALL\n  ".join(selects)
    # Interpolate each block's time inside its row group from the group's min/max timestamps.
    sql = (f"WITH blocks AS (\n  {union}\n),\n"
           f"placed AS (\n"
           f"  SELECT b.n, b.err, g.ts_min + (g.ts_max - g.ts_min) *\n"
           f"         ((b.block * 4096 - g.first_row)::DOUBLE / g.num_rows) AS ts\n"
           f"  FROM blocks b JOIN otel_row_groups g\n"
           f"    ON g.part = b.part AND b.block * 4096 >= g.first_row AND b.block * 4096 < g.first_row + g.num_rows\n"
           f")\n"
           f"SELECT date_trunc('{bucket}', ts) AS bucket, sum(n)::BIGINT AS log_rows, sum(err)::BIGINT AS error_rows\n"
           f"FROM placed\nGROUP BY 1\nHAVING sum(n) > 0\nORDER BY 1\nLIMIT %(limit)s")
    params["limit"] = _limit(limit)
    return Query("otel_trend", sql, params, limit,
                 w.describe() + _describe_filters(service, severity) + f", per {bucket} (time placed via catalog)")


def otel_top_messages(conn, start, end, *, service: str | None = None, severity: str | None = "error",
                      limit: int = 20) -> Query:
    """Most frequent message patterns in [start, end) (reads Body remotely, so the
    window is capped at MAX_BODY_ROW_GROUPS row groups, about one minute)."""
    w = plan_window(conn, start, end, max_row_groups=MAX_BODY_ROW_GROUPS)
    params: dict[str, Any] = {}
    cond = _condition(service, severity, params)
    union, p = _remote_union(w, "ServiceName, SeverityText, Body")
    params.update(p)
    # Rows outside the filter get a NULL pattern and are dropped after grouping.
    sql = (f"SELECT CASE WHEN {cond} THEN {PATTERN_EXPR} END AS pattern,\n"
           f"       ServiceName AS service, lower(SeverityText) AS severity,\n"
           f"       count(*) AS log_rows, count(*) OVER () AS total_patterns\n"
           f"FROM (\n  {union}\n) t\nGROUP BY 1, 2, 3\nHAVING pattern IS NOT NULL\n"
           f"ORDER BY log_rows DESC\nLIMIT %(limit)s")
    params["limit"] = _limit(limit)
    return Query("otel_top_messages", sql, params, limit,
                 w.describe(body=True) + _describe_filters(service, severity))


def otel_context(part: int, file_row_number: int, *, before: int = 10, after: int = 10) -> Query:
    """Rows just before/after one row of a part (the files are sorted by time), read remotely."""
    for v in (before, after):
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 100:
            raise ValueError("before/after must be integers between 0 and 100")
    lo, hi = max(0, file_row_number - before), file_row_number + after + 1
    sql = (f"SELECT file_row_number, file_row_number - %(anchor)s AS offset_rows,\n"
           f"       Timestamp::TIMESTAMPTZ AS ts, ServiceName, SeverityText, TraceId, left(Body, 500) AS body\n"
           f"FROM read_parquet('{part_url(part)}', file_row_number = true)\n"
           f"WHERE file_row_number >= %(lo)s AND file_row_number < %(hi)s\nORDER BY file_row_number")
    return Query("otel_context", sql, {"anchor": file_row_number, "lo": lo, "hi": hi}, None,
                 f"otel remote: part {part} rows {lo:,}..{hi - 1:,}")


def otel_trace(part: int, trace_id: str, around_row: int, *, window_rows: int = 1_000_000,
               limit: int = 100) -> Query:
    """All rows of one trace near a known row (a trace spans milliseconds, so
    +/- window_rows, ~1.6 s each way by default, around the hit is enough)."""
    if not 1 <= window_rows <= 5_000_000:
        raise ValueError("window_rows must be between 1 and 5,000,000")
    lo, hi = max(0, around_row - window_rows), around_row + window_rows
    # The TraceId match is grouped on (not a WHERE), see the module docstring.
    sql = (f"SELECT hit AS file_row_number, ts, ServiceName, SeverityText, SpanId, body,\n"
           f"       count(*) OVER () AS total_matches\n"
           f"FROM (\n"
           f"  SELECT CASE WHEN TraceId = %(trace)s THEN file_row_number END AS hit,\n"
           f"         any_value(Timestamp::TIMESTAMPTZ) AS ts, any_value(ServiceName) AS ServiceName,\n"
           f"         any_value(SeverityText) AS SeverityText, any_value(SpanId) AS SpanId,\n"
           f"         any_value(left(Body, 300)) AS body\n"
           f"  FROM read_parquet('{part_url(part)}', file_row_number = true)\n"
           f"  WHERE file_row_number >= %(lo)s AND file_row_number < %(hi)s\n"
           f"  GROUP BY 1\n  HAVING hit IS NOT NULL\n) t\n"
           f"ORDER BY file_row_number\nLIMIT %(limit)s")
    return Query("otel_trace", sql, {"lo": lo, "hi": hi, "trace": trace_id, "limit": _limit(limit)}, limit,
                 f"otel remote: part {part} rows {lo:,}..{hi - 1:,}, TraceId = {trace_id!r}")


# --------------------------------------------------------------------------- local index

def otel_search(text: str, start=None, end=None, *, service: str | None = None,
                severity: str | None = None, limit: int = 20) -> Query:
    """Full-text search on Body over the locally indexed slice (otel_index_slice),
    oldest first. Same web-search syntax as queries.search_logs. Body and other
    non-indexed columns are fetched from S3 for the returned rows only."""
    if not text or len(text) > 500:
        raise ValueError("text must contain 1-500 characters")
    cond = ["Body @@ websearch_to_tsquery(%(text)s)"]
    params: dict[str, Any] = {"text": text}
    desc = [f"full-text {text!r}"]
    if start is not None:
        cond.append("ts >= %(start)s::TIMESTAMPTZ"); params["start"] = start; desc.append(f"ts >= {start}")
    if end is not None:
        cond.append("ts < %(end)s::TIMESTAMPTZ"); params["end"] = end; desc.append(f"ts < {end}")
    if service is not None:
        cond.append("ServiceName = %(service)s"); params["service"] = service; desc.append(f"service = {service!r}")
    if severity is not None:
        cond.append("SeverityText = %(severity)s"); params["severity"] = severity; desc.append(f"severity = {severity!r}")
    where = "WHERE " + " AND ".join(cond)
    sql = (f"SELECT file_row_number, ts, ServiceName, SeverityText, TraceId, left(Body, 500) AS body\n"
           f"FROM otel_logs_idx\n{where}\nORDER BY file_row_number\nLIMIT %(limit)s")
    count_sql = f"SELECT count(*) AS total_matches\nFROM otel_logs_idx\n{where}"
    params["limit"] = _limit(limit)
    return Query("otel_search", sql, params, limit,
                 "otel index (part 0 slice, see otel_index_slice): " + ", ".join(desc), count_sql)


def otel_index_count_by(group_by: str, *, text: str | None = None, limit: int = 50) -> Query:
    """Counts per service/severity over the whole indexed slice, optionally only
    rows matching a full-text query. Answered from the index, no S3 reads."""
    keys = {"service": "ServiceName", "severity": "SeverityText",
            "service_severity": "ServiceName || ' / ' || SeverityText"}
    if group_by not in keys:
        raise ValueError(f"group_by must be one of {tuple(keys)}")
    params: dict[str, Any] = {"limit": _limit(limit)}
    where = ""
    if text:
        where, params["text"] = "WHERE Body @@ websearch_to_tsquery(%(text)s)\n", text
    sql = (f"SELECT {keys[group_by]} AS {group_by}, count(*) AS log_rows,\n"
           f"       round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS pct, count(*) OVER () AS total_groups\n"
           f"FROM otel_logs_idx\n{where}GROUP BY 1\nORDER BY log_rows DESC\nLIMIT %(limit)s")
    return Query("otel_index_count_by", sql, params, limit,
                 "otel index (part 0 slice)" + (f": full-text {text!r}" if text else ": all indexed rows"))
