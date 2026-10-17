from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, cast

import pytest

from log_insights.db import DatabaseConnection
from log_insights.ownership import (
    CONFIRMED_COMPONENTS,
    CREATE_OWNERSHIP_TABLE_SQL,
    DEFAULT_OWNERSHIP_PATH,
    INSERT_OWNERSHIP_SQL,
    OWNERSHIP_HEADERS,
    OWNERSHIP_TABLE,
    load_ownership_mapping,
    read_ownership_mapping,
)


class RecordingCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.inserted: list[tuple[str, str, str, str, str]] = []

    def __enter__(self) -> RecordingCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, parameters: Any = None) -> None:
        self.executed.append((sql, parameters))

    def executemany(
        self, sql: str, parameters: list[tuple[str, str, str, str, str]]
    ) -> None:
        self.executed.append((sql, parameters))
        self.inserted = list(parameters)

    def fetchone(self) -> dict[str, int]:
        return {"row_count": len(self.inserted)}


class RecordingConnection:
    def __init__(self) -> None:
        self.cursor_instance = RecordingCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> RecordingCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_versioned_mapping_contains_exactly_the_confirmed_components() -> None:
    records = read_ownership_mapping()

    assert len(records) == 15
    assert {(record.dataset, record.component) for record in records} == set(
        CONFIRMED_COMPONENTS
    )
    assert all(record.team != "UNMAPPED" for record in records)
    assert all(record.mapping_version == "v1" for record in records)
    assert all("Fictional" in record.provenance for record in records)


def test_loader_is_repeatable_and_only_replaces_the_ownership_table() -> None:
    connection = RecordingConnection()

    first = load_ownership_mapping(cast(DatabaseConnection, connection))
    second = load_ownership_mapping(cast(DatabaseConnection, connection))

    statements = [sql.strip() for sql, _ in connection.cursor_instance.executed]
    assert statements.count(CREATE_OWNERSHIP_TABLE_SQL.strip()) == 2
    assert statements.count(f"DELETE FROM {OWNERSHIP_TABLE}") == 2
    assert statements.count(INSERT_OWNERSHIP_SQL.strip()) == 2
    assert first == second
    assert first.row_count == 15
    assert connection.commits == 2
    assert connection.rollbacks == 0
    assert not any(
        "loghub_bgl" in sql or "loghub_openstack" in sql for sql in statements
    )


def test_mapping_reader_rejects_an_incomplete_component_set(tmp_path: Path) -> None:
    records = read_ownership_mapping()
    path = tmp_path / "incomplete.csv"
    with path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=OWNERSHIP_HEADERS)
        writer.writeheader()
        for record in records[:-1]:
            writer.writerow(record.__dict__)

    with pytest.raises(ValueError, match="exactly the 15 confirmed components"):
        read_ownership_mapping(path)


def test_default_mapping_path_is_repository_versioned() -> None:
    assert DEFAULT_OWNERSHIP_PATH.name == "fictional_component_ownership_v1.csv"
    assert DEFAULT_OWNERSHIP_PATH.is_file()
