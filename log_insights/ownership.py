"""Versioned fictional component ownership and its idempotent loader."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .db import DatabaseConnection

OWNERSHIP_TABLE = "fictional_component_ownership_v1"
OWNERSHIP_VERSION = "v1"
OWNERSHIP_PROVENANCE = "Fictional hackathon mapping; not authoritative"
OWNERSHIP_MAPPING_REPOSITORY_PATH = (
    "ownership/fictional_component_ownership_v1.csv"
)
DEFAULT_OWNERSHIP_PATH = (
    Path(__file__).resolve().parents[1] / OWNERSHIP_MAPPING_REPOSITORY_PATH
)
UNMAPPED_TEAM = "UNMAPPED"

OWNERSHIP_HEADERS = (
    "dataset",
    "component",
    "team",
    "mapping_version",
    "provenance",
)

CONFIRMED_COMPONENTS = frozenset(
    {
        ("bgl", "APP"),
        ("bgl", "DISCOVERY"),
        ("bgl", "HARDWARE"),
        ("bgl", "KERNEL"),
        ("bgl", "MMCS"),
        ("openstack", "nova.api.openstack.compute.server_external_events"),
        ("openstack", "nova.api.openstack.wsgi"),
        ("openstack", "nova.compute.claims"),
        ("openstack", "nova.compute.manager"),
        ("openstack", "nova.compute.resource_tracker"),
        ("openstack", "nova.metadata.wsgi.server"),
        ("openstack", "nova.osapi_compute.wsgi.server"),
        ("openstack", "nova.scheduler.host_manager"),
        ("openstack", "nova.virt.libvirt.driver"),
        ("openstack", "nova.virt.libvirt.imagecache"),
    }
)

CREATE_OWNERSHIP_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {OWNERSHIP_TABLE} (
    dataset VARCHAR NOT NULL,
    component VARCHAR NOT NULL,
    team VARCHAR NOT NULL,
    mapping_version VARCHAR NOT NULL,
    provenance VARCHAR NOT NULL,
    PRIMARY KEY (dataset, component)
)
"""

INSERT_OWNERSHIP_SQL = f"""
INSERT INTO {OWNERSHIP_TABLE} (
    dataset, component, team, mapping_version, provenance
) VALUES (%s, %s, %s, %s, %s)
"""


@dataclass(frozen=True)
class OwnershipRecord:
    dataset: str
    component: str
    team: str
    mapping_version: str
    provenance: str

    def as_tuple(self) -> tuple[str, str, str, str, str]:
        return (
            self.dataset,
            self.component,
            self.team,
            self.mapping_version,
            self.provenance,
        )


@dataclass(frozen=True)
class OwnershipLoadResult:
    table: str
    mapping_version: str
    source_file: str
    row_count: int


def read_ownership_mapping(
    path: Path = DEFAULT_OWNERSHIP_PATH,
) -> list[OwnershipRecord]:
    """Read and strictly validate the complete v1 fictional mapping."""

    if not path.is_file():
        raise FileNotFoundError(f"Ownership mapping not found: {path}")

    with path.open("r", encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        actual_headers = tuple(reader.fieldnames or ())
        if actual_headers != OWNERSHIP_HEADERS:
            raise ValueError(
                f"Unexpected ownership headers in {path}: "
                f"expected {OWNERSHIP_HEADERS!r}, got {actual_headers!r}"
            )
        raw_rows = list(reader)

    records: list[OwnershipRecord] = []
    for row_number, row in enumerate(raw_rows, start=2):
        values = {field: (row.get(field) or "").strip() for field in OWNERSHIP_HEADERS}
        missing = [field for field, value in values.items() if not value]
        if missing:
            raise ValueError(
                f"Missing ownership values in {path} at CSV row {row_number}: "
                f"{', '.join(missing)}"
            )
        if values["mapping_version"] != OWNERSHIP_VERSION:
            raise ValueError(
                f"Unexpected mapping_version in {path} at CSV row {row_number}"
            )
        if values["provenance"] != OWNERSHIP_PROVENANCE:
            raise ValueError(
                f"Unexpected provenance in {path} at CSV row {row_number}"
            )
        if values["team"] == UNMAPPED_TEAM:
            raise ValueError(f"{UNMAPPED_TEAM} is reserved for join misses")
        records.append(OwnershipRecord(**values))

    keys = [(record.dataset, record.component) for record in records]
    if len(set(keys)) != len(keys):
        raise ValueError(f"Duplicate dataset/component ownership keys in {path}")
    actual_components = frozenset(keys)
    if actual_components != CONFIRMED_COMPONENTS:
        missing = sorted(CONFIRMED_COMPONENTS - actual_components)
        unexpected = sorted(actual_components - CONFIRMED_COMPONENTS)
        raise ValueError(
            "Ownership mapping must contain exactly the 15 confirmed components; "
            f"missing={missing!r}, unexpected={unexpected!r}"
        )
    return records


def load_ownership_mapping(
    connection: DatabaseConnection,
    path: Path = DEFAULT_OWNERSHIP_PATH,
) -> OwnershipLoadResult:
    """Atomically replace only the project-owned v1 mapping table.

    Validation completes before SQL starts. Deleting and reinserting within one
    transaction makes repeated runs deterministic while leaving the log tables and
    their indexes untouched.
    """

    records = read_ownership_mapping(path)
    try:
        with connection.cursor() as cursor:
            cursor.execute(CREATE_OWNERSHIP_TABLE_SQL)
            cursor.execute(f"DELETE FROM {OWNERSHIP_TABLE}")
            cursor.executemany(
                INSERT_OWNERSHIP_SQL,
                [record.as_tuple() for record in records],
            )
            cursor.execute(f"SELECT COUNT(*) AS row_count FROM {OWNERSHIP_TABLE}")
            count_row = cursor.fetchone()
        if count_row is None:
            raise RuntimeError("SereneDB did not return an ownership row count")
        row_count = int(count_row["row_count"])
        if row_count != len(records):
            raise RuntimeError(
                f"Ownership row-count validation failed: expected={len(records)}, "
                f"actual={row_count}"
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    return OwnershipLoadResult(
        table=OWNERSHIP_TABLE,
        mapping_version=OWNERSHIP_VERSION,
        source_file=OWNERSHIP_MAPPING_REPOSITORY_PATH,
        row_count=row_count,
    )
