"""Parameterized SQL for the log tools, over the schema in sql/01_schema.sql.

Each builder returns a Query: SQL with psycopg named placeholders plus the bound
parameters. User input is only ever bound; identifiers (tables, group-by columns,
buckets) come from the closed lists below. Run a Query with run_query(), which
adds the evidence the brief asks for: the executed SQL and filters, row count,
whether the result was truncated, and timing.

Timestamps are TIMESTAMP without time zone, as written in the logs
(BGL: 2005-06-03 .. 2006-01-04, OpenStack: 2017-05-14 .. 2017-05-17).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

DATASETS = ("bgl", "openstack")
MAX_LIMIT = 200

TABLE = {"bgl": "bgl_logs", "openstack": "openstack_logs"}
INDEX = {"bgl": "bgl_logs_idx", "openstack": "openstack_logs_idx"}

# Columns returned for individual log lines.
LINE_COLUMNS = {
    "bgl": "line_no, ts, node, alert_label, component, level, message",
    "openstack": "source_file, line_no, ts, service, component, level, request_id, instance_id, message",
}

# Group-by keys per dataset -> SQL expression (l = log table, o = ownership).
GROUP_KEYS = {
    "bgl": {
        "component": "l.component",
        "team": "o.team",
        "level": "l.level",
        "alert_label": "coalesce(l.alert_label, '(no alert)')",
        "node": "coalesce(l.node, '(none)')",
        "rack": "coalesce(split_part(l.node, '-', 1), '(none)')",
    },
    "openstack": {
        "component": "l.component",
        "team": "o.team",
        "level": "l.level",
        "service": "l.service",
        "http_status": "coalesce(l.http_status::TEXT, '(not http)')",
        "source_file": "l.source_file",
    },
}

BUCKETS = ("hour", "day", "week", "month")

# Message -> recurring pattern: UUIDs, hex values and digit-bearing tokens become <*>.
PATTERN_EXPR = (
    "regexp_replace(regexp_replace(l.message, "
    "'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '<uuid>', 'g'), "
    "'0x[0-9a-fA-F]+|\\b[0-9a-fA-F]*[0-9][0-9a-fA-F]*\\b', '<*>', 'g')"
)

# OpenStack request path -> endpoint template (tenant ids, UUIDs, numbers removed).
ENDPOINT_EXPR = (
    "regexp_replace(regexp_replace(regexp_replace(l.http_path, "
    "'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '{id}', 'g'), "
    "'/[0-9a-f]{32}', '/{tenant}', 'g'), '\\?.*$', '')"
)


@dataclass
class Query:
    name: str
    sql: str
    params: dict[str, Any] = field(default_factory=dict)
    limit: int | None = None  # row cap applied in SQL, used to flag truncation
    scope: str = ""           # human-readable data scope and filters
    # Optional separate "SELECT count(*)" over the same filters. Cheaper than
    # count(*) OVER () when a search matches hundreds of thousands of lines.
    count_sql: str | None = None


# --------------------------------------------------------------------------- helpers

def _dataset(dataset: str) -> str:
    if dataset not in DATASETS:
        raise ValueError(f"dataset must be one of {DATASETS}")
    return dataset


def _limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer between 1 and {MAX_LIMIT}")
    return limit


def _bucket(bucket: str) -> str:
    if bucket not in BUCKETS:
        raise ValueError(f"bucket must be one of {BUCKETS}")
    return bucket


class _Filters:
    """Collects WHERE conditions and their bound parameters."""

    def __init__(self, alias: str = "l"):
        self.alias = alias
        self.conditions: list[str] = []
        self.params: dict[str, Any] = {}
        self.described: list[str] = []

    def add(self, condition: str, describe: str, **params: Any) -> None:
        self.conditions.append(condition)
        self.params.update(params)
        self.described.append(describe)

    def time_range(self, start: datetime | str | None, end: datetime | str | None) -> None:
        a = self.alias
        if start is not None:
            self.add(f"{a}.ts >= %(start)s", f"ts >= {start}", start=start)
        if end is not None:
            self.add(f"{a}.ts < %(end)s", f"ts < {end}", end=end)

    def text(self, text: str | None) -> None:
        if text:
            if len(text) > 500:
                raise ValueError("text must be at most 500 characters")
            self.add(f"{self.alias}.message @@ websearch_to_tsquery(%(text)s)",
                     f"full-text {text!r}", text=text)

    def equals(self, column: str, value: Any, name: str) -> None:
        if value is not None:
            self.add(f"{self.alias}.{column} = %({name})s", f"{column} = {value!r}", **{name: value})

    def sql(self) -> str:
        return ("WHERE " + " AND ".join(self.conditions)) if self.conditions else ""

    def scope(self, dataset: str) -> str:
        return f"{dataset}: " + (", ".join(self.described) if self.described else "whole dataset")


def _common_filters(dataset: str, f: _Filters, *, level=None, component=None, team=None,
                    node=None, service=None, alert_label=None, alerts_only=False) -> None:
    f.equals("level", level, "level")
    f.equals("component", component, "component")
    if team is not None:
        f.add(f"{f.alias}.component IN (SELECT component FROM ownership "
              "WHERE dataset = %(dataset)s AND team = %(team)s)",
              f"team = {team!r}", dataset=dataset, team=team)
    if dataset == "bgl":
        f.equals("node", node, "node")
        f.equals("alert_label", alert_label, "alert_label")
        if alerts_only:
            f.add(f"{f.alias}.is_alert", "alerts only")
    else:
        f.equals("service", service, "service")


# --------------------------------------------------------------------------- engineer

def search_logs(dataset: str, text: str, start=None, end=None, *, level=None, component=None,
                team=None, node=None, service=None, alert_label=None, alerts_only=False,
                limit: int = 20) -> Query:
    """Full-text search over log messages, oldest first.

    text uses web-search syntax: plain words must ALL occur (AND), `a OR b`
    matches either, `-word` excludes, "quoted phrase" must match in order.
    Words are split at every non-alphanumeric character, so quote dotted or
    dashed identifiers ("nova.virt.libvirt") to match them as a phrase.
    run_query() also reports total_available, the number of matching lines in
    the whole filtered scope (not just this page).
    """
    ds = _dataset(dataset)
    if not text:
        raise ValueError("text is required")
    f = _Filters()
    f.text(text)
    f.time_range(start, end)
    _common_filters(ds, f, level=level, component=component, team=team, node=node,
                    service=service, alert_label=alert_label, alerts_only=alerts_only)
    cols = ", ".join(f"l.{c.strip()}" for c in LINE_COLUMNS[ds].split(","))
    # Rows and total are two statements: a window count over every match forces
    # the whole match set to be materialized and sorted (142 ms vs. 5 ms for
    # "error" on BGL), while a top-N and a plain count are both answered fast.
    sql = (f"SELECT {cols}\n"
           f"FROM {INDEX[ds]} l\n{f.sql()}\n"
           f"ORDER BY l.ts, l.line_no\nLIMIT %(limit)s")
    count_sql = f"SELECT count(*) AS total_matches\nFROM {INDEX[ds]} l\n{f.sql()}"
    return Query("search_logs", sql, {**f.params, "limit": _limit(limit)}, limit, f.scope(ds),
                 count_sql)


def nearby_events(dataset: str, line_no: int, *, source_file: str | None = None,
                  before: int = 10, after: int = 10, same_node: bool = False,
                  window_seconds: int = 300) -> Query:
    """Events around one log line, in file order.

    Default: the `before` lines preceding and `after` lines following line_no in the
    same file. same_node=True (BGL) instead returns events on the anchor line's node
    within +/- window_seconds, capped at before + after + 1 rows around the anchor.
    OpenStack needs source_file (line numbers restart in each file).
    """
    ds = _dataset(dataset)
    for v in (before, after):
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 100:
            raise ValueError("before/after must be integers between 0 and 100")
    params: dict[str, Any] = {"line_no": line_no}
    if ds == "openstack":
        if not source_file:
            raise ValueError("source_file is required for openstack")
        anchor_file = "AND a.source_file = %(source_file)s"
        file_match = "AND l.source_file = a.source_file"
        params["source_file"] = source_file
    else:
        anchor_file = file_match = ""
    cols = ", ".join(f"l.{c.strip()}" for c in LINE_COLUMNS[ds].split(","))
    if same_node:
        if ds != "bgl":
            raise ValueError("same_node is only available for bgl")
        if not 1 <= window_seconds <= 86400:
            raise ValueError("window_seconds must be between 1 and 86400")
        params.update(window=window_seconds, before=before, after=after)
        sql = (f"WITH a AS (SELECT line_no, ts, node FROM bgl_logs a WHERE a.line_no = %(line_no)s),\n"
               f"w AS (\n"
               f"  SELECT {cols}, l.line_no - a.line_no AS offset_lines,\n"
               f"         row_number() OVER (PARTITION BY sign(l.line_no - a.line_no)\n"
               f"                            ORDER BY abs(l.line_no - a.line_no)) AS rn\n"
               f"  FROM bgl_logs l, a\n"
               f"  WHERE l.node = a.node\n"
               f"    AND l.ts BETWEEN a.ts - to_seconds(%(window)s) AND a.ts + to_seconds(%(window)s))\n"
               f"SELECT * EXCLUDE (rn) FROM w\n"
               f"WHERE offset_lines = 0 OR (offset_lines < 0 AND rn <= %(before)s)\n"
               f"   OR (offset_lines > 0 AND rn <= %(after)s)\n"
               f"ORDER BY line_no")
        scope = f"bgl: node of line {line_no}, +/- {window_seconds}s, {before} before / {after} after"
    else:
        params.update(lo=line_no - before, hi=line_no + after)
        sql = (f"WITH a AS (SELECT {'source_file, ' if ds == 'openstack' else ''}line_no "
               f"FROM {TABLE[ds]} a WHERE a.line_no = %(line_no)s {anchor_file})\n"
               f"SELECT {cols}, l.line_no - a.line_no AS offset_lines\n"
               f"FROM {TABLE[ds]} l, a\n"
               f"WHERE l.line_no BETWEEN %(lo)s AND %(hi)s {file_match}\n"
               f"ORDER BY l.line_no")
        scope = f"{ds}: lines {line_no - before}..{line_no + after}" + (f" of {source_file}" if source_file else "")
    return Query("nearby_events", sql, params, None, scope)


def instance_timeline(instance_id: str, limit: int = 100) -> Query:
    """OpenStack: every log line mentioning one VM instance, in time order."""
    sql = (f"SELECT {', '.join('l.' + c.strip() for c in LINE_COLUMNS['openstack'].split(','))},\n"
           f"       l.is_anomaly, count(*) OVER () AS total_matches\n"
           f"FROM openstack_logs l\nWHERE l.instance_id = %(instance_id)s\n"
           f"ORDER BY l.ts, l.source_file, l.line_no\nLIMIT %(limit)s")
    return Query("instance_timeline", sql, {"instance_id": instance_id, "limit": _limit(limit)},
                 limit, f"openstack: instance_id = {instance_id!r}")


# --------------------------------------------------------------------------- team lead

def count_by(dataset: str, group_by: str, start=None, end=None, *, text=None, level=None,
             component=None, team=None, node=None, service=None, alert_label=None,
             alerts_only=False, limit: int = 50) -> Query:
    """Count log lines grouped by one key (component, team, level, ...) over the
    full filtered scope, with first/last occurrence and share of the total.
    Counts are log lines, not incidents."""
    ds = _dataset(dataset)
    if group_by not in GROUP_KEYS[ds]:
        raise ValueError(f"group_by must be one of {tuple(GROUP_KEYS[ds])}")
    key = GROUP_KEYS[ds][group_by]
    f = _Filters()
    f.text(text)
    f.time_range(start, end)
    _common_filters(ds, f, level=level, component=component, team=team, node=node,
                    service=service, alert_label=alert_label, alerts_only=alerts_only)
    source = INDEX[ds] if text else TABLE[ds]
    sql = (f"SELECT {key} AS {group_by}, count(*) AS lines,\n"
           f"       round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS pct,\n"
           f"       min(l.ts) AS first_seen, max(l.ts) AS last_seen,\n"
           f"       count(*) OVER () AS total_groups\n"
           f"FROM {source} l\n"
           f"LEFT JOIN ownership o ON o.dataset = '{ds}' AND o.component = l.component\n"
           f"{f.sql()}\nGROUP BY 1\nORDER BY lines DESC\nLIMIT %(limit)s")
    return Query("count_by", sql, {**f.params, "limit": _limit(limit)}, limit, f.scope(ds))


def trend(dataset: str, bucket: str = "day", start=None, end=None, *, split_by: str | None = None,
          text=None, level=None, component=None, team=None, node=None, service=None,
          alert_label=None, alerts_only=False, limit: int = MAX_LIMIT) -> Query:
    """Line counts per time bucket (hour/day/week/month), optionally split by one
    group key (e.g. team). Empty buckets are omitted."""
    ds = _dataset(dataset)
    b = _bucket(bucket)
    f = _Filters()
    f.text(text)
    f.time_range(start, end)
    _common_filters(ds, f, level=level, component=component, team=team, node=node,
                    service=service, alert_label=alert_label, alerts_only=alerts_only)
    split_sql = ""
    if split_by is not None:
        if split_by not in GROUP_KEYS[ds]:
            raise ValueError(f"split_by must be one of {tuple(GROUP_KEYS[ds])}")
        split_sql = f", {GROUP_KEYS[ds][split_by]} AS {split_by}"
    source = INDEX[ds] if text else TABLE[ds]
    sql = (f"SELECT date_trunc('{b}', l.ts) AS bucket{split_sql}, count(*) AS lines\n"
           f"FROM {source} l\n"
           f"LEFT JOIN ownership o ON o.dataset = '{ds}' AND o.component = l.component\n"
           f"{f.sql()}\nGROUP BY ALL\nORDER BY 1{', 3 DESC' if split_sql else ''}\nLIMIT %(limit)s")
    return Query("trend", sql, {**f.params, "limit": _limit(limit)}, limit, f.scope(ds) + f", per {b}")


def recurring_patterns(dataset: str, start=None, end=None, *, text=None, level=None,
                       component=None, team=None, node=None, service=None, alert_label=None,
                       alerts_only=False, limit: int = 20) -> Query:
    """Most frequent message patterns (numbers, hex and ids replaced by <*>), with
    owning team, line count, affected nodes/instances, active days and first/last seen."""
    ds = _dataset(dataset)
    f = _Filters()
    f.text(text)
    f.time_range(start, end)
    _common_filters(ds, f, level=level, component=component, team=team, node=node,
                    service=service, alert_label=alert_label, alerts_only=alerts_only)
    spread = "count(DISTINCT l.node) AS nodes" if ds == "bgl" else "count(DISTINCT l.instance_id) AS instances"
    source = INDEX[ds] if text else TABLE[ds]
    sql = (f"SELECT {PATTERN_EXPR} AS pattern, l.component, o.team,\n"
           f"       string_agg(DISTINCT l.level, ',') AS levels, count(*) AS lines, {spread},\n"
           f"       count(DISTINCT l.ts::DATE) AS active_days, min(l.ts) AS first_seen, max(l.ts) AS last_seen,\n"
           f"       count(*) OVER () AS total_patterns\n"
           f"FROM {source} l\n"
           f"LEFT JOIN ownership o ON o.dataset = '{ds}' AND o.component = l.component\n"
           f"{f.sql()}\nGROUP BY 1, 2, 3\nORDER BY lines DESC\nLIMIT %(limit)s")
    return Query("recurring_patterns", sql, {**f.params, "limit": _limit(limit)}, limit, f.scope(ds))


# --------------------------------------------------------------------------- manager

def team_priorities(dataset: str, start=None, end=None, *, recent_days: int = 7) -> Query:
    """Per owning team: problem lines (BGL alerts / OpenStack WARNING+), distinct
    patterns, affected nodes or instances, and the change between the last
    `recent_days` and the `recent_days` before that (relative to the window end,
    or the dataset's last timestamp). Measured counts only, no cost/impact."""
    ds = _dataset(dataset)
    if not 1 <= recent_days <= 90:
        raise ValueError("recent_days must be between 1 and 90")
    f = _Filters()
    f.time_range(start, end)
    problem = "l.is_alert" if ds == "bgl" else "l.level <> 'INFO'"
    spread_name = "nodes" if ds == "bgl" else "instances"
    end_expr = "%(end)s::TIMESTAMP" if end is not None else f"(SELECT max(ts) FROM {TABLE[ds]})"
    where = f.sql()
    sql = (f"WITH p AS (\n"
           f"  SELECT o.team, l.component, l.ts, l.message, {'l.node' if ds == 'bgl' else 'l.instance_id'} AS unit\n"
           f"  FROM {TABLE[ds]} l\n"
           f"  JOIN ownership o ON o.dataset = '{ds}' AND o.component = l.component\n"
           f"  {where}{' AND ' if where else 'WHERE '}{problem}\n"
           f"), bounds AS (SELECT {end_expr} AS end_ts)\n"
           f"SELECT p.team, count(*) AS problem_lines,\n"
           f"       count(DISTINCT {PATTERN_EXPR.replace('l.message', 'p.message')}) AS distinct_patterns,\n"
           f"       count(DISTINCT p.unit) AS {spread_name}, count(DISTINCT p.ts::DATE) AS active_days,\n"
           f"       min(p.ts) AS first_seen, max(p.ts) AS last_seen,\n"
           f"       count(*) FILTER (WHERE p.ts >= b.end_ts - to_days(%(recent)s)) AS recent_lines,\n"
           f"       count(*) FILTER (WHERE p.ts >= b.end_ts - to_days(2 * %(recent)s)\n"
           f"                          AND p.ts < b.end_ts - to_days(%(recent)s)) AS previous_lines\n"
           f"FROM p, bounds b\nGROUP BY p.team\nORDER BY problem_lines DESC")
    return Query("team_priorities", sql, {**f.params, "recent": recent_days}, None,
                 f.scope(ds) + f", problem = {'alert lines' if ds == 'bgl' else 'WARNING/ERROR/CRITICAL lines'}, "
                               f"recent window {recent_days}d")


# --------------------------------------------------------------------------- OpenStack

def latency_summary(group_by: str = "endpoint", start=None, end=None, *, service=None,
                    slow_threshold_s: float = 1.0, limit: int = 30) -> Query:
    """OpenStack HTTP request latency (seconds) grouped by endpoint template,
    method, status or service: count, p50/p95/p99, max and slow-request share."""
    keys = {"endpoint": f"l.http_method || ' ' || {ENDPOINT_EXPR}", "method": "l.http_method",
            "status": "l.http_status::TEXT", "service": "l.service"}
    if group_by not in keys:
        raise ValueError(f"group_by must be one of {tuple(keys)}")
    f = _Filters()
    f.add("l.latency_s IS NOT NULL", "HTTP access lines")
    f.time_range(start, end)
    f.equals("service", service, "service")
    sql = (f"SELECT {keys[group_by]} AS {group_by}, count(*) AS requests,\n"
           f"       round(avg(l.latency_s), 3) AS avg_s,\n"
           f"       round(quantile_cont(l.latency_s, 0.5), 3) AS p50_s,\n"
           f"       round(quantile_cont(l.latency_s, 0.95), 3) AS p95_s,\n"
           f"       round(quantile_cont(l.latency_s, 0.99), 3) AS p99_s,\n"
           f"       round(max(l.latency_s), 3) AS max_s,\n"
           f"       count(*) FILTER (WHERE l.latency_s > %(slow)s) AS slow_requests,\n"
           f"       count(*) FILTER (WHERE l.http_status >= 400) AS error_responses,\n"
           f"       count(*) OVER () AS total_groups\n"
           f"FROM openstack_logs l\n{f.sql()}\nGROUP BY 1\nORDER BY requests DESC\nLIMIT %(limit)s")
    return Query("latency_summary", sql, {**f.params, "slow": slow_threshold_s, "limit": _limit(limit)},
                 limit, f.scope("openstack") + f", slow > {slow_threshold_s}s")


def latency_trend(bucket: str = "hour", start=None, end=None, *, endpoint: str | None = None,
                  limit: int = MAX_LIMIT) -> Query:
    """OpenStack request count and p50/p95 latency per time bucket, optionally for
    one endpoint template as returned by latency_summary (e.g. 'POST /v2/{tenant}/servers')."""
    b = _bucket(bucket)
    f = _Filters()
    f.add("l.latency_s IS NOT NULL", "HTTP access lines")
    f.time_range(start, end)
    if endpoint:
        f.add(f"l.http_method || ' ' || {ENDPOINT_EXPR} = %(endpoint)s", f"endpoint = {endpoint!r}",
              endpoint=endpoint)
    sql = (f"SELECT date_trunc('{b}', l.ts) AS bucket, count(*) AS requests,\n"
           f"       round(quantile_cont(l.latency_s, 0.5), 3) AS p50_s,\n"
           f"       round(quantile_cont(l.latency_s, 0.95), 3) AS p95_s,\n"
           f"       round(max(l.latency_s), 3) AS max_s,\n"
           f"       count(*) FILTER (WHERE l.http_status >= 400) AS error_responses\n"
           f"FROM openstack_logs l\n{f.sql()}\nGROUP BY 1\nORDER BY 1\nLIMIT %(limit)s")
    return Query("latency_trend", sql, {**f.params, "limit": _limit(limit)}, limit,
                 f.scope("openstack") + f", per {b}")


def error_rates(group_by: str = "endpoint", start=None, end=None, *, limit: int = 30) -> Query:
    """OpenStack error rates. group_by='endpoint' or 'status': HTTP 4xx/5xx share
    of requests. group_by='component' or 'service': WARNING/ERROR/CRITICAL share of
    all log lines, with the owning team."""
    f = _Filters()
    f.time_range(start, end)
    if group_by in ("endpoint", "status"):
        key = f"l.http_method || ' ' || {ENDPOINT_EXPR}" if group_by == "endpoint" else "l.http_status::TEXT"
        f.add("l.http_status IS NOT NULL", "HTTP access lines")
        sql = (f"SELECT {key} AS {group_by}, count(*) AS requests,\n"
               f"       count(*) FILTER (WHERE l.http_status BETWEEN 400 AND 499) AS client_errors_4xx,\n"
               f"       count(*) FILTER (WHERE l.http_status >= 500) AS server_errors_5xx,\n"
               f"       round(100.0 * count(*) FILTER (WHERE l.http_status >= 400) / count(*), 2) AS error_pct\n"
               f"FROM openstack_logs l\n{f.sql()}\nGROUP BY 1\n"
               f"ORDER BY client_errors_4xx + server_errors_5xx DESC, requests DESC\nLIMIT %(limit)s")
    elif group_by in ("component", "service"):
        team = ", o.team" if group_by == "component" else ""
        sql = (f"SELECT l.{group_by}{team}, count(*) AS lines,\n"
               f"       count(*) FILTER (WHERE l.level = 'WARNING') AS warnings,\n"
               f"       count(*) FILTER (WHERE l.level = 'ERROR') AS errors,\n"
               f"       count(*) FILTER (WHERE l.level = 'CRITICAL') AS critical,\n"
               f"       round(100.0 * count(*) FILTER (WHERE l.level <> 'INFO') / count(*), 2) AS problem_pct\n"
               f"FROM openstack_logs l\n"
               f"LEFT JOIN ownership o ON o.dataset = 'openstack' AND o.component = l.component\n"
               f"{f.sql()}\nGROUP BY ALL\nORDER BY warnings + errors + critical DESC, lines DESC\nLIMIT %(limit)s")
    else:
        raise ValueError("group_by must be one of ('endpoint', 'status', 'component', 'service')")
    return Query("error_rates", sql, {**f.params, "limit": _limit(limit)}, limit, f.scope("openstack"))


def anomaly_comparison() -> Query:
    """OpenStack: lifecycle step durations ("Took N seconds to <step>") for the 4
    labelled anomalous instances versus all other instances."""
    sql = ("WITH steps AS (\n"
           "  SELECT l.is_anomaly, l.instance_id,\n"
           "         regexp_extract(l.message, 'Took [0-9.]+ seconds to (.*?)\\.?$', 1) AS step,\n"
           "         regexp_extract(l.message, 'Took ([0-9.]+) seconds', 1)::DOUBLE AS seconds\n"
           "  FROM openstack_logs l\n"
           "  WHERE l.component = 'nova.compute.manager' AND regexp_matches(l.message, 'Took [0-9.]+ seconds to ')\n"
           ")\n"
           "SELECT step, CASE WHEN is_anomaly THEN 'anomalous' ELSE 'normal' END AS instances,\n"
           "       count(*) AS samples, round(avg(seconds), 2) AS avg_s, round(min(seconds), 2) AS min_s,\n"
           "       round(quantile_cont(seconds, 0.5), 2) AS p50_s, round(quantile_cont(seconds, 0.95), 2) AS p95_s,\n"
           "       round(max(seconds), 2) AS max_s\n"
           "FROM steps\nGROUP BY ALL\nORDER BY step, instances")
    return Query("anomaly_comparison", sql, {}, None,
                 "openstack: nova.compute.manager 'Took N seconds' lines, anomalous = anomaly_labels.txt")


def slowest_instances(step: str = "spawn the instance on the hypervisor", limit: int = 10) -> Query:
    """OpenStack: instances with the longest duration for one lifecycle step, with
    their anomaly label, so labelled anomalies can be compared to the slowest normals."""
    sql = ("SELECT l.instance_id, l.is_anomaly, l.source_file, l.ts,\n"
           "       regexp_extract(l.message, 'Took ([0-9.]+) seconds', 1)::DOUBLE AS seconds,\n"
           "       count(*) OVER () AS total_matches\n"
           "FROM openstack_logs l\n"
           "WHERE l.component = 'nova.compute.manager' AND l.message LIKE '%%Took %% seconds to ' || %(step)s || '%%'\n"
           "ORDER BY seconds DESC\nLIMIT %(limit)s")
    return Query("slowest_instances", sql, {"step": step, "limit": _limit(limit)}, limit,
                 f"openstack: 'Took N seconds to {step}'")


# --------------------------------------------------------------------------- whole corpus

def corpus_overview() -> Query:
    """Whole base corpus: lines, time span, problem lines and teams per dataset."""
    sql = ("SELECT 'bgl' AS dataset, count(*) AS lines, min(ts) AS first_ts, max(ts) AS last_ts,\n"
           "       count(*) FILTER (WHERE is_alert) AS problem_lines, 'alert-labelled lines' AS problem_definition,\n"
           "       count(DISTINCT component) AS components, count(DISTINCT node) AS nodes_or_instances\n"
           "FROM bgl_logs\n"
           "UNION ALL\n"
           "SELECT 'openstack', count(*), min(ts), max(ts),\n"
           "       count(*) FILTER (WHERE level <> 'INFO'), 'WARNING/ERROR/CRITICAL lines',\n"
           "       count(DISTINCT component), count(DISTINCT instance_id)\n"
           "FROM openstack_logs")
    return Query("corpus_overview", sql, {}, None, "whole base corpus, no filters")


# --------------------------------------------------------------------------- execution

def run_query(conn, query: Query) -> dict[str, Any]:
    """Execute a Query on a psycopg connection and return rows plus evidence.

    total_available is the full number of matching rows/groups, taken from the
    query's count_sql or its total_matches / total_groups / total_patterns
    column; truncated is True when it exceeds the rows returned.
    """
    started = time.perf_counter()
    cur = conn.execute(query.sql, query.params)
    columns = [c.name for c in cur.description]
    rows = [dict(zip(columns, r)) if not isinstance(r, dict) else r for r in cur.fetchall()]
    total = None
    if query.count_sql is not None:
        count_row = conn.execute(query.count_sql, query.params).fetchone()
        total = count_row["total_matches"] if isinstance(count_row, dict) else count_row[0]
    elapsed_ms = round(1000 * (time.perf_counter() - started), 1)
    for col in TOTAL_COLUMNS:
        if col in columns:
            total = rows[0][col] if rows else 0
            for r in rows:
                del r[col]
            break
    if total is None and query.limit is not None and len(rows) >= query.limit:
        truncated = True  # no total column: hitting the LIMIT means there may be more
    else:
        truncated = total is not None and total > len(rows)
    return {
        "query": query.name,
        "scope": query.scope,
        "sql": query.sql if query.count_sql is None else f"{query.sql};\n\n{query.count_sql}",
        "params": query.params,
        "row_count": len(rows),
        "total_available": total,
        "truncated": truncated,
        "elapsed_ms": elapsed_ms,
        "rows": rows,
    }


TOTAL_COLUMNS = ("total_matches", "total_groups", "total_patterns")
