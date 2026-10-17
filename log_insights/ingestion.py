"""Repeatable ingestion for the public Loghub 2,000-row CSV samples.

The tables in this module are deliberately named ``*_sample_v1``. They are a
provisional representation of the public Loghub files, not an organizer-provided
hackathon schema.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import psycopg

from .db import DatabaseConnection

EXPECTED_SAMPLE_ROWS = 2_000

BGL_TABLE = "loghub_bgl_sample_v1"
OPENSTACK_TABLE = "loghub_openstack_sample_v1"
BGL_SEARCH_INDEX = "loghub_bgl_sample_v1_fts"
OPENSTACK_SEARCH_INDEX = "loghub_openstack_sample_v1_fts"
TEXT_SEARCH_DICTIONARY = "loghub_lowercase_v1"

BGL_HEADERS = (
    "LineId",
    "Label",
    "Timestamp",
    "Date",
    "Node",
    "Time",
    "NodeRepeat",
    "Type",
    "Component",
    "Level",
    "Content",
    "EventId",
    "EventTemplate",
)

OPENSTACK_HEADERS = (
    "LineId",
    "Logrecord",
    "Date",
    "Time",
    "Pid",
    "Level",
    "Component",
    "ADDR",
    "Content",
    "EventId",
    "EventTemplate",
)

CREATE_BGL_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {BGL_TABLE} (
    line_id BIGINT PRIMARY KEY,
    label VARCHAR NOT NULL,
    timestamp_epoch BIGINT NOT NULL,
    date_text VARCHAR NOT NULL,
    node VARCHAR NOT NULL,
    time_text VARCHAR NOT NULL,
    node_repeat VARCHAR NOT NULL,
    log_type VARCHAR NOT NULL,
    component VARCHAR NOT NULL,
    severity VARCHAR NOT NULL,
    message VARCHAR NOT NULL,
    event_id VARCHAR NOT NULL,
    event_template VARCHAR NOT NULL,
    event_time TIMESTAMP NOT NULL
)
"""

CREATE_OPENSTACK_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {OPENSTACK_TABLE} (
    line_id BIGINT PRIMARY KEY,
    log_record VARCHAR NOT NULL,
    date_text VARCHAR NOT NULL,
    time_text VARCHAR NOT NULL,
    pid BIGINT NOT NULL,
    severity VARCHAR NOT NULL,
    component VARCHAR NOT NULL,
    address_context VARCHAR NOT NULL,
    message VARCHAR NOT NULL,
    event_id VARCHAR NOT NULL,
    event_template VARCHAR NOT NULL,
    event_time TIMESTAMP NOT NULL
)
"""

CREATE_TEXT_SEARCH_DICTIONARY_SQL = f"""
CREATE TEXT SEARCH DICTIONARY {TEXT_SEARCH_DICTIONARY} (
    template = 'text',
    locale = 'en_US.UTF-8',
    case = 'lower',
    stemming = false,
    accent = false,
    frequency = true,
    position = true,
    offset = true
)
"""

CREATE_BGL_SEARCH_INDEX_SQL = f"""
CREATE INDEX {BGL_SEARCH_INDEX} ON {BGL_TABLE}
USING inverted (
    line_id,
    message {TEXT_SEARCH_DICTIONARY},
    event_time,
    component,
    severity,
    node,
    label,
    event_id
)
"""

CREATE_OPENSTACK_SEARCH_INDEX_SQL = f"""
CREATE INDEX {OPENSTACK_SEARCH_INDEX} ON {OPENSTACK_TABLE}
USING inverted (
    line_id,
    message {TEXT_SEARCH_DICTIONARY},
    event_time,
    component,
    severity,
    event_id
)
"""

INSERT_BGL_SQL = f"""
INSERT INTO {BGL_TABLE} (
    line_id, label, timestamp_epoch, date_text, node, time_text,
    node_repeat, log_type, component, severity, message, event_id,
    event_template, event_time
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

INSERT_OPENSTACK_SQL = f"""
INSERT INTO {OPENSTACK_TABLE} (
    line_id, log_record, date_text, time_text, pid, severity, component,
    address_context, message, event_id, event_template, event_time
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


@dataclass(frozen=True)
class LoadResult:
    """Counts returned after a successful two-table load."""

    bgl_rows: int
    openstack_rows: int


def read_bgl_csv(path: Path, expected_rows: int = EXPECTED_SAMPLE_ROWS) -> list[tuple[Any, ...]]:
    """Read and validate the exact public BGL structured sample schema."""

    rows = _read_csv(path, BGL_HEADERS, expected_rows)
    _validate_required_fields(
        rows, ("LineId", "Time", "Component", "Level", "Content", "EventId"), path
    )
    _validate_unique_line_ids(rows, path)
    return [_convert_bgl_row(row, path) for row in rows]


def read_openstack_csv(
    path: Path, expected_rows: int = EXPECTED_SAMPLE_ROWS
) -> list[tuple[Any, ...]]:
    """Read and validate the exact public OpenStack structured sample schema."""

    rows = _read_csv(path, OPENSTACK_HEADERS, expected_rows)
    _validate_required_fields(
        rows,
        ("LineId", "Date", "Time", "Pid", "Component", "Level", "Content", "EventId"),
        path,
    )
    _validate_unique_line_ids(rows, path)
    return [_convert_openstack_row(row, path) for row in rows]


def load_samples(
    connection: DatabaseConnection,
    bgl_path: Path,
    openstack_path: Path,
    expected_rows: int = EXPECTED_SAMPLE_ROWS,
) -> LoadResult:
    """Replace both project-owned sample tables and rebuild their search indexes.

    CSV validation happens before any database mutation. The table replacement is
    transactional, so a failed insert cannot leave one sample partially loaded.
    """

    bgl_rows = read_bgl_csv(bgl_path, expected_rows)
    openstack_rows = read_openstack_csv(openstack_path, expected_rows)

    _create_tables(connection)
    _drop_search_indexes(connection)

    try:
        with connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {BGL_TABLE}")
            cursor.execute(f"DELETE FROM {OPENSTACK_TABLE}")
            cursor.executemany(INSERT_BGL_SQL, bgl_rows)
            cursor.executemany(INSERT_OPENSTACK_SQL, openstack_rows)
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    _ensure_text_search_dictionary(connection)
    _create_search_indexes(connection)

    result = LoadResult(
        bgl_rows=_table_count(connection, BGL_TABLE),
        openstack_rows=_table_count(connection, OPENSTACK_TABLE),
    )
    if result.bgl_rows != expected_rows or result.openstack_rows != expected_rows:
        raise RuntimeError(
            "Post-load row-count validation failed: "
            f"BGL={result.bgl_rows}, OpenStack={result.openstack_rows}, "
            f"expected={expected_rows}"
        )
    return result


def validation_report(connection: DatabaseConnection) -> dict[str, Any]:
    """Return non-secret validation evidence for both loaded sample tables."""

    return {
        "bgl": _dataset_validation(connection, BGL_TABLE),
        "openstack": _dataset_validation(connection, OPENSTACK_TABLE),
        "full_text_search": {
            "bgl_core_matches": _search_count(connection, BGL_SEARCH_INDEX, "core"),
            "openstack_instance_matches": _search_count(
                connection, OPENSTACK_SEARCH_INDEX, "instance"
            ),
        },
        "aggregations": {
            "bgl_top_component_severity": _component_severity_counts(
                connection, BGL_TABLE
            ),
            "openstack_top_component_severity": _component_severity_counts(
                connection, OPENSTACK_TABLE
            ),
        },
    }


def _read_csv(
    path: Path, expected_headers: Sequence[str], expected_rows: int
) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"CSV sample not found: {path}")

    with path.open("r", encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        actual_headers = tuple(reader.fieldnames or ())
        if actual_headers != tuple(expected_headers):
            raise ValueError(
                f"Unexpected headers in {path}: expected {tuple(expected_headers)!r}, "
                f"got {actual_headers!r}"
            )
        rows = list(reader)

    if len(rows) != expected_rows:
        raise ValueError(
            f"Unexpected row count in {path}: expected {expected_rows}, got {len(rows)}"
        )
    return rows


def _validate_required_fields(
    rows: Iterable[Mapping[str, str]], required_fields: Sequence[str], path: Path
) -> None:
    for row_number, row in enumerate(rows, start=2):
        missing = [field for field in required_fields if not row.get(field)]
        if missing:
            raise ValueError(
                f"Missing required values in {path} at CSV row {row_number}: "
                f"{', '.join(missing)}"
            )


def _validate_unique_line_ids(rows: Sequence[Mapping[str, str]], path: Path) -> None:
    line_ids = [row["LineId"] for row in rows]
    if len(set(line_ids)) != len(line_ids):
        raise ValueError(f"Duplicate LineId values found in {path}")


def _convert_bgl_row(row: Mapping[str, str], path: Path) -> tuple[Any, ...]:
    try:
        event_time = datetime.strptime(row["Time"], "%Y-%m-%d-%H.%M.%S.%f")
        return (
            int(row["LineId"]),
            row["Label"],
            int(row["Timestamp"]),
            row["Date"],
            row["Node"],
            row["Time"],
            row["NodeRepeat"],
            row["Type"],
            row["Component"],
            row["Level"],
            row["Content"],
            row["EventId"],
            row["EventTemplate"],
            event_time,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid typed BGL value in {path}, LineId={row.get('LineId')}") from error


def _convert_openstack_row(row: Mapping[str, str], path: Path) -> tuple[Any, ...]:
    try:
        event_time = datetime.fromisoformat(f"{row['Date']}T{row['Time']}")
        return (
            int(row["LineId"]),
            row["Logrecord"],
            row["Date"],
            row["Time"],
            int(row["Pid"]),
            row["Level"],
            row["Component"],
            row["ADDR"],
            row["Content"],
            row["EventId"],
            row["EventTemplate"],
            event_time,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"Invalid typed OpenStack value in {path}, LineId={row.get('LineId')}"
        ) from error


def _create_tables(connection: DatabaseConnection) -> None:
    try:
        with connection.cursor() as cursor:
            cursor.execute(CREATE_BGL_TABLE_SQL)
            cursor.execute(CREATE_OPENSTACK_TABLE_SQL)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _drop_search_indexes(connection: DatabaseConnection) -> None:
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP INDEX IF EXISTS {BGL_SEARCH_INDEX}")
            cursor.execute(f"DROP INDEX IF EXISTS {OPENSTACK_SEARCH_INDEX}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _ensure_text_search_dictionary(connection: DatabaseConnection) -> None:
    try:
        with connection.cursor() as cursor:
            cursor.execute(CREATE_TEXT_SEARCH_DICTIONARY_SQL)
        connection.commit()
    except psycopg.Error as error:
        connection.rollback()
        if error.sqlstate != "42710":  # duplicate_object
            raise


def _create_search_indexes(connection: DatabaseConnection) -> None:
    try:
        with connection.cursor() as cursor:
            cursor.execute(CREATE_BGL_SEARCH_INDEX_SQL)
            cursor.execute(CREATE_OPENSTACK_SEARCH_INDEX_SQL)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _table_count(connection: DatabaseConnection, table: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) AS row_count FROM {table}")
        result = cursor.fetchone()
    connection.commit()
    if result is None:
        raise RuntimeError(f"No count returned for {table}")
    return int(result["row_count"])


def _dataset_validation(connection: DatabaseConnection, table: str) -> dict[str, Any]:
    query = f"""
    SELECT
        COUNT(*) AS row_count,
        COUNT(DISTINCT line_id) AS distinct_line_ids,
        COUNT(DISTINCT event_id) AS distinct_event_ids,
        SUM(CASE WHEN message = '' THEN 1 ELSE 0 END) AS empty_messages,
        SUM(CASE WHEN component = '' THEN 1 ELSE 0 END) AS empty_components,
        SUM(CASE WHEN severity = '' THEN 1 ELSE 0 END) AS empty_severities,
        SUM(CASE WHEN event_id = '' THEN 1 ELSE 0 END) AS empty_event_ids,
        SUM(CASE WHEN event_time IS NULL THEN 1 ELSE 0 END) AS missing_event_times,
        MIN(event_time) AS earliest_event_time,
        MAX(event_time) AS latest_event_time
    FROM {table}
    """
    with connection.cursor() as cursor:
        cursor.execute(query)
        result = cursor.fetchone()
    connection.commit()
    if result is None:
        raise RuntimeError(f"No validation result returned for {table}")
    report = dict(result)
    for field in (
        "row_count",
        "distinct_line_ids",
        "distinct_event_ids",
        "empty_messages",
        "empty_components",
        "empty_severities",
        "empty_event_ids",
        "missing_event_times",
    ):
        report[field] = int(report[field])
    return report


def _search_count(connection: DatabaseConnection, index: str, term: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT COUNT(*) AS match_count FROM {index} WHERE message @@ %s", (term,)
        )
        result = cursor.fetchone()
    connection.commit()
    if result is None:
        raise RuntimeError(f"No search result returned for {index}")
    return int(result["match_count"])


def _component_severity_counts(
    connection: DatabaseConnection, table: str
) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT component, severity, COUNT(*) AS record_count
            FROM {table}
            GROUP BY component, severity
            ORDER BY record_count DESC, component, severity
            LIMIT 5
            """
        )
        results = cursor.fetchall()
    connection.commit()
    return [dict(result) for result in results]
