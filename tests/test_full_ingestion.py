from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, cast
import zipfile

import pytest

from log_insights.full_ingestion import (
    BGL_COLUMNS,
    DEFAULT_SOURCE_MANIFEST,
    OPENSTACK_COLUMNS,
    SelectionPolicy,
    StrictIngestionError,
    load_archive,
    load_source_manifest,
    parse_bgl_line,
    parse_openstack_line,
    verify_archive,
)
from log_insights.db import DatabaseConnection
from scripts.load_full_data import _parse_args as parse_full_load_args


class _FakeCopy:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def __enter__(self) -> _FakeCopy:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def write_row(self, row: tuple[Any, ...]) -> None:
        self.rows.append(row)


class _FakeCursor:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection
        self._count_requested = False

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, parameters: object = None) -> None:
        if sql.startswith("DELETE FROM"):
            self.connection.rows.clear()
        self._count_requested = sql.startswith("SELECT COUNT(*)")

    def copy(self, _: str) -> _FakeCopy:
        return _FakeCopy(self.connection.rows)

    def fetchone(self) -> dict[str, int] | None:
        if self._count_requested:
            return {"row_count": len(self.connection.rows)}
        return None


class _FakeConnection:
    def __init__(self) -> None:
        self.rows: list[tuple[Any, ...]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _as_mapping(columns: tuple[str, ...], row: tuple[object, ...]) -> dict[str, object]:
    return dict(zip(columns, row, strict=True))


def test_parse_bgl_preserves_raw_fields_and_uses_null_event_fields() -> None:
    raw = (
        "- 1117838570 2005.06.03 R02-M1-N0-C:J12-U11 "
        "2005-06-03-15.42.50.363779 R02-M1-N0-C:J12-U11 RAS KERNEL INFO "
        "instruction cache parity error corrected\n"
    )
    actual = _as_mapping(
        BGL_COLUMNS,
        parse_bgl_line(
            raw,
            source_file="BGL.log",
            source_line_number=1,
            archive_sha256="abc",
        ),
    )

    assert actual["line_id"] == 1
    assert actual["raw_line"] == raw.rstrip("\n")
    assert actual["anomaly_label"] == "-"
    assert actual["is_anomaly"] is False
    assert actual["component"] == "KERNEL"
    assert actual["severity"] == "INFO"
    assert actual["message"] == "instruction cache parity error corrected"
    assert actual["event_time"] == datetime(2005, 6, 3, 15, 42, 50, 363779)
    assert actual["event_id"] is None
    assert actual["event_template"] is None


def test_parse_bgl_handles_missing_and_irregular_node_repeat_without_shifting() -> None:
    missing_repeat = (
        "- 1119415930 2005.06.21 - 2005-06-21-21.52.10.214285 "
        "RAS KERNEL FATAL Kill job 20251 timed out. Block freed."
    )
    irregular_repeat = (
        "- 1133447861 2005.12.01 - 2005-12-01-06.37.41.417709 "
        "time for a single instance of a correctable ddr. RAS KERNEL INFO "
        "0 microseconds spent in the rbs signal handler"
    )
    null_log_type = (
        "- 1118241709 2005.06.08 R33-M1-ND 2005-06-08-07.41.49.839299 "
        "R33-M1-ND NULL HARDWARE WARNING PrepareForService is being done"
    )

    missing = _as_mapping(
        BGL_COLUMNS,
        parse_bgl_line(
            missing_repeat,
            source_file="BGL.log",
            source_line_number=885024,
            archive_sha256="abc",
        ),
    )
    irregular = _as_mapping(
        BGL_COLUMNS,
        parse_bgl_line(
            irregular_repeat,
            source_file="BGL.log",
            source_line_number=4344697,
            archive_sha256="abc",
        ),
    )
    null_type = _as_mapping(
        BGL_COLUMNS,
        parse_bgl_line(
            null_log_type,
            source_file="BGL.log",
            source_line_number=191408,
            archive_sha256="abc",
        ),
    )

    assert missing["node_repeat"] is None
    assert missing["component"] == "KERNEL"
    assert missing["severity"] == "FATAL"
    assert missing["message"] == "Kill job 20251 timed out. Block freed."
    assert irregular["node_repeat"] == "time for a single instance of a correctable ddr."
    assert irregular["component"] == "KERNEL"
    assert irregular["severity"] == "INFO"
    assert null_type["log_type"] == "NULL"
    assert null_type["component"] == "HARDWARE"


def test_parse_openstack_extracts_optional_context_request_and_instance() -> None:
    raw = (
        "nova-compute.log.1 2017-05-16 00:00:04.500 2931 INFO "
        "nova.compute.manager [req-3ea4052c-895d-4b64-9e2d-04d64c4d94ab "
        "- - - - -] [instance: b9000564-fe1a-409b-b8cc-1e88b294cd1d] "
        "VM Started (Lifecycle Event)"
    )
    actual = _as_mapping(
        OPENSTACK_COLUMNS,
        parse_openstack_line(
            raw,
            source_file="openstack_abnormal.log",
            source_line_number=7,
            archive_sha256="def",
            file_ordinal=1,
            anomaly_instance_ids=frozenset(
                {"b9000564-fe1a-409b-b8cc-1e88b294cd1d"}
            ),
        ),
    )

    assert actual["line_id"] == 1_000_000_007
    assert actual["source_class"] == "abnormal"
    assert actual["request_id"] == "req-3ea4052c-895d-4b64-9e2d-04d64c4d94ab"
    assert actual["instance_id"] == "b9000564-fe1a-409b-b8cc-1e88b294cd1d"
    assert actual["is_labeled_anomaly_instance"] is True
    assert actual["message"].startswith("[instance:")
    assert actual["event_id"] is None
    assert actual["event_template"] is None


def test_parse_openstack_preserves_traceback_without_request_context() -> None:
    raw = (
        "nova-compute.log.1 2017-05-16 03:19:45.356 2931 ERROR "
        "oslo_service.periodic_task Traceback (most recent call last):"
    )
    actual = _as_mapping(
        OPENSTACK_COLUMNS,
        parse_openstack_line(
            raw,
            source_file="openstack_normal1.log",
            source_line_number=27180,
            archive_sha256="def",
            file_ordinal=2,
        ),
    )

    assert actual["line_id"] == 2_000_027_180
    assert actual["address_context"] is None
    assert actual["request_id"] is None
    assert actual["message"] == "Traceback (most recent call last):"


@pytest.mark.parametrize(
    ("parser", "raw"),
    [
        ("bgl", "not a BGL record"),
        ("openstack", "not an OpenStack record"),
    ],
)
def test_parsers_reject_records_without_fixed_prefix(parser: str, raw: str) -> None:
    with pytest.raises(ValueError):
        if parser == "bgl":
            parse_bgl_line(
                raw,
                source_file="BGL.log",
                source_line_number=1,
                archive_sha256="abc",
            )
        else:
            parse_openstack_line(
                raw,
                source_file="openstack_normal1.log",
                source_line_number=1,
                archive_sha256="def",
                file_ordinal=2,
            )


def test_selection_policy_validates_bounds_and_is_deterministic() -> None:
    policy = SelectionPolicy(stride=3, max_records_per_file=2)
    assert [line for line in range(1, 10) if policy.includes(line, (line - 1) // 3)] == [1, 4]
    with pytest.raises(ValueError):
        SelectionPolicy(stride=0)
    with pytest.raises(ValueError):
        SelectionPolicy(max_records_per_file=0)


def test_versioned_manifest_matches_recorded_archive_metadata() -> None:
    manifest = load_source_manifest()
    assert DEFAULT_SOURCE_MANIFEST.is_file()
    assert manifest["manifest_version"] == "v1"
    assert manifest["sources"]["bgl"]["archive_size_bytes"] == 57_489_019
    assert len(manifest["sources"]["openstack"]["members"]) == 4


def test_archive_verification_detects_size_and_checksum_changes(tmp_path: Path) -> None:
    path = tmp_path / "source.bin"
    path.write_bytes(b"official source bytes")
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()

    verify_archive(path, path.stat().st_size, checksum)
    with pytest.raises(ValueError, match="size mismatch"):
        verify_archive(path, path.stat().st_size + 1, checksum)
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        verify_archive(path, path.stat().st_size, "0" * 64)


def test_manifest_is_valid_json() -> None:
    json.loads(DEFAULT_SOURCE_MANIFEST.read_text(encoding="utf-8"))


def test_complete_load_cli_requires_explicit_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["load_full_data"])
    with pytest.raises(SystemExit) as raised:
        parse_full_load_args()
    assert raised.value.code == 2

    monkeypatch.setattr(
        sys,
        "argv",
        ["load_full_data", "--dataset", "bgl", "--confirm-full-load"],
    )
    args = parse_full_load_args()
    assert args.dataset == "bgl"
    assert args.confirm_full_load is True


def test_streaming_loader_reports_rejects_and_strict_mode_rolls_back(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "BGL.zip"
    valid = (
        "- 1117838570 2005.06.03 R02-M1-N0-C:J12-U11 "
        "2005-06-03-15.42.50.363779 R02-M1-N0-C:J12-U11 RAS KERNEL INFO ok"
    )
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("BGL.log", f"{valid}\nmalformed\n")
    archive_bytes = archive_path.read_bytes()
    member_bytes = f"{valid}\nmalformed\n".encode()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "manifest_version": "v1",
                "sources": {
                    "bgl": {
                        "url": "https://example.invalid/BGL.zip",
                        "archive_path": str(archive_path),
                        "archive_size_bytes": len(archive_bytes),
                        "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
                        "members": [
                            {
                                "name": "BGL.log",
                                "size_bytes": len(member_bytes),
                                "sha256": hashlib.sha256(member_bytes).hexdigest(),
                                "role": "raw_log",
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    strict_connection = _FakeConnection()

    with pytest.raises(StrictIngestionError) as raised:
        load_archive(
            cast(DatabaseConnection, strict_connection),
            dataset="bgl",
            target="staging",
            strict=True,
            manifest_path=manifest_path,
            create_search_index=False,
        )

    strict_report = raised.value.report
    assert strict_report.attempted_records == 2
    assert strict_report.parsed_records == 1
    assert strict_report.rejected_records == 1
    assert strict_report.parsing_error_count == 1
    assert strict_report.inserted_records == 0
    assert len(strict_report.error_examples) == 1
    assert strict_connection.commits == 0
    assert strict_connection.rollbacks == 1

    lenient_connection = _FakeConnection()
    report = load_archive(
        cast(DatabaseConnection, lenient_connection),
        dataset="bgl",
        target="staging",
        strict=False,
        manifest_path=manifest_path,
        create_search_index=False,
    )
    assert report.inserted_records == 1
    assert report.rejected_records == 1
    assert lenient_connection.commits == 1
