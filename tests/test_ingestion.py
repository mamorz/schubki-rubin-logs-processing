from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import pytest

from log_insights.ingestion import (
    BGL_HEADERS,
    OPENSTACK_HEADERS,
    read_bgl_csv,
    read_openstack_csv,
)


def _write_csv(path: Path, headers: tuple[str, ...], row: dict[str, str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=headers)
        writer.writeheader()
        writer.writerow(row)


def test_read_bgl_csv_preserves_fields_and_parses_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "bgl.csv"
    _write_csv(
        path,
        BGL_HEADERS,
        {
            "LineId": "1",
            "Label": "-",
            "Timestamp": "1117838570",
            "Date": "2005.06.03",
            "Node": "R02-M1-N0-C:J12-U11",
            "Time": "2005-06-03-15.42.50.675872",
            "NodeRepeat": "R02-M1-N0-C:J12-U11",
            "Type": "RAS",
            "Component": "KERNEL",
            "Level": "INFO",
            "Content": "instruction cache parity error corrected",
            "EventId": "E77",
            "EventTemplate": "instruction cache parity error corrected",
        },
    )

    row = read_bgl_csv(path, expected_rows=1)[0]

    assert row[0] == 1
    assert row[8:12] == (
        "KERNEL",
        "INFO",
        "instruction cache parity error corrected",
        "E77",
    )
    assert row[-1] == datetime(2005, 6, 3, 15, 42, 50, 675872)


def test_read_openstack_csv_preserves_fields_and_parses_timestamp(
    tmp_path: Path,
) -> None:
    path = tmp_path / "openstack.csv"
    _write_csv(
        path,
        OPENSTACK_HEADERS,
        {
            "LineId": "1",
            "Logrecord": "nova-api.log.1",
            "Date": "2017-05-16",
            "Time": "00:00:00.008",
            "Pid": "25746",
            "Level": "INFO",
            "Component": "nova.osapi_compute.wsgi.server",
            "ADDR": "request context",
            "Content": "GET /servers/detail status: 200",
            "EventId": "E25",
            "EventTemplate": "GET <*> status: <*>",
        },
    )

    row = read_openstack_csv(path, expected_rows=1)[0]

    assert row[0] == 1
    assert row[4:10] == (
        25746,
        "INFO",
        "nova.osapi_compute.wsgi.server",
        "request context",
        "GET /servers/detail status: 200",
        "E25",
    )
    assert row[-1] == datetime(2017, 5, 16, 0, 0, 0, 8000)


def test_reader_rejects_changed_schema(tmp_path: Path) -> None:
    path = tmp_path / "invalid.csv"
    path.write_text("LineId,Content\n1,message\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Unexpected headers"):
        read_bgl_csv(path, expected_rows=1)


def test_reader_rejects_duplicate_stable_ids(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.csv"
    row = {
        "LineId": "1",
        "Logrecord": "nova-api.log.1",
        "Date": "2017-05-16",
        "Time": "00:00:00.008",
        "Pid": "25746",
        "Level": "INFO",
        "Component": "nova.api",
        "ADDR": "request context",
        "Content": "message",
        "EventId": "E1",
        "EventTemplate": "message",
    }
    with path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=OPENSTACK_HEADERS)
        writer.writeheader()
        writer.writerow(row)
        writer.writerow(row)

    with pytest.raises(ValueError, match="Duplicate LineId"):
        read_openstack_csv(path, expected_rows=2)
