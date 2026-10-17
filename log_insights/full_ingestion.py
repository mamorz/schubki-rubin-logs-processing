"""Streaming ingestion for the original BGL and OpenStack archives.

The full-data tables are deliberately separate from the existing ``*_sample_v1``
tables. Raw archive members are streamed directly; they are never expanded to
large generated files on disk or accumulated in Python lists.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import tarfile
from time import monotonic
from typing import Any, BinaryIO, Iterator, Mapping, Sequence
import zipfile

import psycopg

from .db import DatabaseConnection
from .ingestion import CREATE_TEXT_SEARCH_DICTIONARY_SQL, TEXT_SEARCH_DICTIONARY

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_MANIFEST = PROJECT_ROOT / "manifests/full_dataset_sources_v1.json"

BGL_FULL_TABLE = "loghub_bgl_full_v1"
OPENSTACK_FULL_TABLE = "loghub_openstack_full_v1"
BGL_STAGING_TABLE = "loghub_bgl_full_v1_staging"
OPENSTACK_STAGING_TABLE = "loghub_openstack_full_v1_staging"
BGL_FULL_SEARCH_INDEX = "loghub_bgl_full_v1_fts"
OPENSTACK_FULL_SEARCH_INDEX = "loghub_openstack_full_v1_fts"
BGL_STAGING_SEARCH_INDEX = "loghub_bgl_full_v1_staging_fts"
OPENSTACK_STAGING_SEARCH_INDEX = "loghub_openstack_full_v1_staging_fts"

MAX_ERROR_EXAMPLES = 10
OPENSTACK_RAW_MEMBERS = (
    "openstack_abnormal.log",
    "openstack_normal1.log",
    "openstack_normal2.log",
)

_ALLOWED_TARGETS = {
    ("bgl", "full"): (BGL_FULL_TABLE, BGL_FULL_SEARCH_INDEX),
    ("bgl", "staging"): (BGL_STAGING_TABLE, BGL_STAGING_SEARCH_INDEX),
    ("openstack", "full"): (OPENSTACK_FULL_TABLE, OPENSTACK_FULL_SEARCH_INDEX),
    ("openstack", "staging"): (
        OPENSTACK_STAGING_TABLE,
        OPENSTACK_STAGING_SEARCH_INDEX,
    ),
}
_REQUEST_ID = re.compile(r"\b(req-[0-9a-fA-F-]+)\b")
_INSTANCE_ID = re.compile(r"\[instance:\s*([0-9a-fA-F-]+)\]")
_BGL_TAIL = re.compile(
    r"^(?:(?P<node_repeat>.*?)\s+)?"
    r"(?P<log_type>RAS|NULL)\s+"
    r"(?P<component>\S+)\s+"
    r"(?P<severity>INFO|WARNING|ERROR|FATAL|SEVERE|FAILURE)"
    r"(?:\s(?P<message>.*))?$"
)

BGL_COLUMNS = (
    "line_id",
    "source_file",
    "source_line_number",
    "source_archive_sha256",
    "raw_line",
    "anomaly_label",
    "is_anomaly",
    "timestamp_epoch",
    "date_text",
    "node",
    "time_text",
    "node_repeat",
    "log_type",
    "component",
    "severity",
    "message",
    "request_id",
    "event_id",
    "event_template",
    "event_time",
)

OPENSTACK_COLUMNS = (
    "line_id",
    "source_file",
    "source_line_number",
    "source_archive_sha256",
    "source_class",
    "raw_line",
    "log_record",
    "date_text",
    "time_text",
    "pid",
    "severity",
    "component",
    "address_context",
    "request_id",
    "instance_id",
    "is_labeled_anomaly_instance",
    "message",
    "event_id",
    "event_template",
    "event_time",
)


@dataclass(frozen=True)
class SelectionPolicy:
    """Deterministic bounded selection from a streaming source."""

    stride: int = 1
    max_records_per_file: int | None = None
    include_first_per_component: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.stride, bool) or self.stride < 1:
            raise ValueError("stride must be a positive integer")
        if self.max_records_per_file is not None and (
            isinstance(self.max_records_per_file, bool)
            or self.max_records_per_file < 1
        ):
            raise ValueError("max_records_per_file must be a positive integer")
        if not isinstance(self.include_first_per_component, bool):
            raise ValueError("include_first_per_component must be a boolean")

    def includes(self, line_number: int, selected_count: int) -> bool:
        if (
            self.max_records_per_file is not None
            and selected_count >= self.max_records_per_file
        ):
            return False
        return (line_number - 1) % self.stride == 0


@dataclass(frozen=True)
class ParsingError:
    source_file: str
    source_line_number: int
    reason: str
    raw_line_excerpt: str


@dataclass(frozen=True)
class SourceFileEvidence:
    url: str
    archive_path: str
    archive_size_bytes: int
    archive_sha256: str
    member_name: str
    member_size_bytes: int
    member_sha256: str
    role: str


@dataclass(frozen=True)
class IngestionReport:
    dataset: str
    target_table: str
    attempted_records: int
    parsed_records: int
    rejected_records: int
    parsing_error_count: int
    inserted_records: int
    source_files: Sequence[SourceFileEvidence]
    error_examples: Sequence[ParsingError]
    selection: Mapping[str, Any]
    event_fields: Mapping[str, str]
    phase_seconds: Mapping[str, float]
    elapsed_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DatasetInspection:
    dataset: str
    physical_record_count: int
    parsed_record_count: int
    rejected_record_count: int
    empty_message_count: int
    missing_request_context_count: int
    request_id_count: int
    earliest_event_time: datetime | None
    latest_event_time: datetime | None
    components: Mapping[str, int]
    severities: Mapping[str, int]
    error_examples: Sequence[ParsingError]

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for field in ("earliest_event_time", "latest_event_time"):
            value = result[field]
            result[field] = value.isoformat() if value is not None else None
        return result


class StrictIngestionError(RuntimeError):
    """Raised after a strict streaming load sees one or more rejected records."""

    def __init__(self, report: IngestionReport) -> None:
        self.report = report
        super().__init__(
            f"Strict {report.dataset} ingestion rejected "
            f"{report.rejected_records} record(s); transaction rolled back"
        )


class _MutableReport:
    def __init__(self) -> None:
        self.attempted = 0
        self.parsed = 0
        self.rejected = 0
        self.errors: list[ParsingError] = []

    def reject(self, error: ParsingError) -> None:
        self.rejected += 1
        if len(self.errors) < MAX_ERROR_EXAMPLES:
            self.errors.append(error)


def load_source_manifest(path: Path = DEFAULT_SOURCE_MANIFEST) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as source:
        manifest = json.load(source)
    if manifest.get("manifest_version") != "v1":
        raise ValueError("Unsupported full-dataset source manifest version")
    return manifest


def verify_archive(path: Path, expected_size: int, expected_sha256: str) -> None:
    """Stream an archive to verify its recorded size and SHA-256."""

    actual_size = path.stat().st_size
    if actual_size != expected_size:
        raise ValueError(
            f"Archive size mismatch for {path}: expected={expected_size}, "
            f"actual={actual_size}"
        )
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"Archive SHA-256 mismatch for {path}: expected={expected_sha256}, "
            f"actual={actual_sha256}"
        )


def parse_bgl_line(
    raw_line: str,
    *,
    source_file: str,
    source_line_number: int,
    archive_sha256: str,
) -> tuple[Any, ...]:
    """Parse one physical BGL line; no template fields are synthesized."""

    line = raw_line.rstrip("\r\n")
    parts = line.split(maxsplit=5)
    if len(parts) < 6:
        raise ValueError("expected the 5-field BGL prefix and a RAS payload")
    (
        anomaly_label,
        timestamp_epoch_text,
        date_text,
        node,
        time_text,
    ) = parts[:5]
    tail_match = _BGL_TAIL.fullmatch(parts[5])
    if tail_match is None:
        raise ValueError("could not locate the BGL RAS/component/severity boundary")
    node_repeat = tail_match.group("node_repeat")
    log_type = tail_match.group("log_type")
    component = tail_match.group("component")
    severity = tail_match.group("severity")
    message = tail_match.group("message") or ""
    try:
        timestamp_epoch = int(timestamp_epoch_text)
        event_time = datetime.fromisoformat(
            f"{time_text[:10]}T{time_text[11:].replace('.', ':', 2)}"
        )
    except ValueError as error:
        raise ValueError(f"invalid BGL timestamp: {error}") from error
    if time_text[:10].replace("-", ".") != date_text:
        raise ValueError("BGL Date and Time fields disagree")
    return (
        source_line_number,
        source_file,
        source_line_number,
        archive_sha256,
        line,
        anomaly_label,
        anomaly_label != "-",
        timestamp_epoch,
        date_text,
        node,
        time_text,
        node_repeat,
        log_type,
        component,
        severity,
        message,
        None,
        None,
        None,
        event_time,
    )


def parse_openstack_line(
    raw_line: str,
    *,
    source_file: str,
    source_line_number: int,
    archive_sha256: str,
    file_ordinal: int,
    anomaly_instance_ids: frozenset[str] = frozenset(),
) -> tuple[Any, ...]:
    """Parse one physical OpenStack line with optional request context."""

    line = raw_line.rstrip("\r\n")
    parts = line.split(maxsplit=6)
    if len(parts) < 6:
        raise ValueError("expected at least 6 fixed OpenStack fields")
    if len(parts) == 6:
        parts.append("")
    log_record, date_text, time_text, pid_text, severity, component, remainder = parts
    try:
        pid = int(pid_text)
        event_time = datetime.fromisoformat(f"{date_text}T{time_text}")
    except ValueError as error:
        raise ValueError(f"invalid OpenStack timestamp or PID: {error}") from error

    address_context: str | None = None
    message = remainder
    if remainder.startswith("["):
        context_end = remainder.find("]")
        if context_end < 0:
            raise ValueError("unterminated OpenStack request context")
        address_context = remainder[1:context_end]
        message = remainder[context_end + 1 :]
        if message.startswith(" "):
            message = message[1:]

    request_match = _REQUEST_ID.search(address_context or "")
    instance_match = _INSTANCE_ID.search(message)
    instance_id = instance_match.group(1) if instance_match else None
    source_class = "abnormal" if source_file == "openstack_abnormal.log" else "normal"
    line_id = file_ordinal * 1_000_000_000 + source_line_number
    return (
        line_id,
        source_file,
        source_line_number,
        archive_sha256,
        source_class,
        line,
        log_record,
        date_text,
        time_text,
        pid,
        severity,
        component,
        address_context,
        request_match.group(1) if request_match else None,
        instance_id,
        instance_id in anomaly_instance_ids if instance_id is not None else None,
        message,
        None,
        None,
        event_time,
    )


def inspect_archives(
    manifest_path: Path = DEFAULT_SOURCE_MANIFEST,
) -> dict[str, DatasetInspection]:
    """Stream every raw record and return bounded, non-secret format evidence."""

    manifest = load_source_manifest(manifest_path)
    results: dict[str, DatasetInspection] = {}
    for dataset in ("bgl", "openstack"):
        stats = _InspectionAccumulator(dataset)
        for parsed, error in _stream_parsed_records(
            dataset,
            manifest,
            SelectionPolicy(),
        ):
            stats.observe(parsed, error)
        results[dataset] = stats.finish()
    return results


def load_archive(
    connection: DatabaseConnection,
    *,
    dataset: str,
    target: str = "staging",
    policy: SelectionPolicy = SelectionPolicy(),
    strict: bool = True,
    manifest_path: Path = DEFAULT_SOURCE_MANIFEST,
    create_search_index: bool = True,
) -> IngestionReport:
    """Stream selected records through COPY into an allowlisted versioned table."""

    key = (dataset, target)
    if key not in _ALLOWED_TARGETS:
        raise ValueError(f"Unsupported dataset/target combination: {key!r}")
    table, index = _ALLOWED_TARGETS[key]
    manifest = load_source_manifest(manifest_path)
    _verify_dataset_archive(dataset, manifest)
    sources = _source_evidence(dataset, manifest)
    mutable = _MutableReport()
    started = monotonic()
    copy_started = monotonic()
    inserted = 0
    columns = BGL_COLUMNS if dataset == "bgl" else OPENSTACK_COLUMNS

    try:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP INDEX IF EXISTS {index}")
            if target == "staging":
                cursor.execute(f"DROP TABLE IF EXISTS {table}")
            cursor.execute(_create_table_sql(dataset, table))
            if target == "full":
                cursor.execute(f"DELETE FROM {table}")
            copy_sql = f"COPY {table} ({', '.join(columns)}) FROM STDIN"
            with cursor.copy(copy_sql) as copy:
                for parsed, error in _stream_parsed_records(dataset, manifest, policy):
                    mutable.attempted += 1
                    if error is not None:
                        mutable.reject(error)
                        continue
                    assert parsed is not None
                    mutable.parsed += 1
                    copy.write_row(parsed)

            report = _build_report(
                dataset,
                table,
                mutable,
                inserted_records=0,
                sources=sources,
                policy=policy,
                phase_seconds={"copy_and_validation": monotonic() - copy_started},
                elapsed_seconds=monotonic() - started,
            )
            if strict and mutable.rejected:
                raise StrictIngestionError(report)
            cursor.execute(f"SELECT COUNT(*) AS row_count FROM {table}")
            count_row = cursor.fetchone()
            if count_row is None:
                raise RuntimeError(f"No row count returned for {table}")
            inserted = int(count_row["row_count"])
            if inserted != mutable.parsed:
                raise RuntimeError(
                    f"COPY count mismatch for {table}: parsed={mutable.parsed}, "
                    f"inserted={inserted}"
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    copy_seconds = monotonic() - copy_started
    index_seconds = 0.0
    if create_search_index:
        index_started = monotonic()
        _create_and_refresh_search_index(connection, dataset, table, index)
        index_seconds = monotonic() - index_started

    return _build_report(
        dataset,
        table,
        mutable,
        inserted_records=inserted,
        sources=sources,
        policy=policy,
        phase_seconds={
            "copy_and_validation": copy_seconds,
            "index_create_and_refresh": index_seconds,
        },
        elapsed_seconds=monotonic() - started,
    )


def _stream_parsed_records(
    dataset: str,
    manifest: Mapping[str, Any],
    policy: SelectionPolicy,
) -> Iterator[tuple[tuple[Any, ...] | None, ParsingError | None]]:
    source = manifest["sources"][dataset]
    archive_path = PROJECT_ROOT / source["archive_path"]
    archive_sha256 = source["archive_sha256"]
    anomaly_instance_ids = frozenset(source.get("anomaly_instance_ids", ()))
    seen_components: set[str] = set()
    if dataset == "bgl":
        with zipfile.ZipFile(archive_path) as archive:
            with archive.open("BGL.log") as member:
                yield from _parse_member(
                    member,
                    "BGL.log",
                    archive_sha256,
                    policy,
                    lambda line, name, number, sha: parse_bgl_line(
                        line,
                        source_file=name,
                        source_line_number=number,
                        archive_sha256=sha,
                    ),
                    component_index=13,
                    seen_components=seen_components,
                )
        return

    with tarfile.open(archive_path, "r:gz") as archive:
        for ordinal, member_name in enumerate(OPENSTACK_RAW_MEMBERS, start=1):
            member = archive.extractfile(member_name)
            if member is None:
                raise ValueError(f"Missing archive member: {member_name}")
            with member:
                yield from _parse_member(
                    member,
                    member_name,
                    archive_sha256,
                    policy,
                    lambda line, name, number, sha, ordinal=ordinal: (
                        parse_openstack_line(
                            line,
                            source_file=name,
                            source_line_number=number,
                            archive_sha256=sha,
                            file_ordinal=ordinal,
                            anomaly_instance_ids=anomaly_instance_ids,
                        )
                    ),
                    component_index=11,
                    seen_components=seen_components,
                )


def _parse_member(
    member: BinaryIO,
    member_name: str,
    archive_sha256: str,
    policy: SelectionPolicy,
    parser: Any,
    *,
    component_index: int,
    seen_components: set[str],
) -> Iterator[tuple[tuple[Any, ...] | None, ParsingError | None]]:
    selected = 0
    for line_number, raw_bytes in enumerate(member, start=1):
        base_included = policy.includes(line_number, selected)
        if not base_included and not policy.include_first_per_component:
            continue
        try:
            raw_line = raw_bytes.decode("utf-8")
            parsed = parser(raw_line, member_name, line_number, archive_sha256)
            component = str(parsed[component_index])
            first_component = component not in seen_components
            seen_components.add(component)
            if not base_included and not first_component:
                continue
            selected += 1
            yield parsed, None
        except (UnicodeDecodeError, ValueError) as error:
            if not base_included:
                continue
            selected += 1
            excerpt = raw_bytes[:500].decode("utf-8", errors="replace").rstrip("\r\n")
            yield None, ParsingError(
                source_file=member_name,
                source_line_number=line_number,
                reason=str(error),
                raw_line_excerpt=excerpt,
            )


class _InspectionAccumulator:
    def __init__(self, dataset: str) -> None:
        self.dataset = dataset
        self.physical = 0
        self.parsed = 0
        self.rejected = 0
        self.empty = 0
        self.missing_context = 0
        self.requests = 0
        self.earliest: datetime | None = None
        self.latest: datetime | None = None
        self.components: Counter[str] = Counter()
        self.severities: Counter[str] = Counter()
        self.errors: list[ParsingError] = []

    def observe(
        self,
        parsed: tuple[Any, ...] | None,
        error: ParsingError | None,
    ) -> None:
        self.physical += 1
        if error is not None:
            self.rejected += 1
            if len(self.errors) < MAX_ERROR_EXAMPLES:
                self.errors.append(error)
            return
        assert parsed is not None
        self.parsed += 1
        if self.dataset == "bgl":
            component, severity, message, event_time = (
                parsed[13],
                parsed[14],
                parsed[15],
                parsed[19],
            )
            request_id = None
        else:
            component, severity, message, event_time = (
                parsed[11],
                parsed[10],
                parsed[16],
                parsed[19],
            )
            if parsed[12] is None:
                self.missing_context += 1
            request_id = parsed[13]
        self.components[str(component)] += 1
        self.severities[str(severity)] += 1
        if message == "":
            self.empty += 1
        if request_id is not None:
            self.requests += 1
        self.earliest = event_time if self.earliest is None else min(self.earliest, event_time)
        self.latest = event_time if self.latest is None else max(self.latest, event_time)

    def finish(self) -> DatasetInspection:
        return DatasetInspection(
            dataset=self.dataset,
            physical_record_count=self.physical,
            parsed_record_count=self.parsed,
            rejected_record_count=self.rejected,
            empty_message_count=self.empty,
            missing_request_context_count=self.missing_context,
            request_id_count=self.requests,
            earliest_event_time=self.earliest,
            latest_event_time=self.latest,
            components=dict(sorted(self.components.items())),
            severities=dict(sorted(self.severities.items())),
            error_examples=tuple(self.errors),
        )


def _create_table_sql(dataset: str, table: str) -> str:
    common = """line_id BIGINT PRIMARY KEY,
    source_file VARCHAR NOT NULL,
    source_line_number BIGINT NOT NULL,
    source_archive_sha256 VARCHAR NOT NULL,"""
    if dataset == "bgl":
        fields = f"""{common}
    raw_line VARCHAR NOT NULL,
    anomaly_label VARCHAR NOT NULL,
    is_anomaly BOOLEAN NOT NULL,
    timestamp_epoch BIGINT NOT NULL,
    date_text VARCHAR NOT NULL,
    node VARCHAR NOT NULL,
    time_text VARCHAR NOT NULL,
    node_repeat VARCHAR,
    log_type VARCHAR NOT NULL,
    component VARCHAR NOT NULL,
    severity VARCHAR NOT NULL,
    message VARCHAR NOT NULL,
    request_id VARCHAR,
    event_id VARCHAR,
    event_template VARCHAR,
    event_time TIMESTAMP NOT NULL"""
    else:
        fields = f"""{common}
    source_class VARCHAR NOT NULL,
    raw_line VARCHAR NOT NULL,
    log_record VARCHAR NOT NULL,
    date_text VARCHAR NOT NULL,
    time_text VARCHAR NOT NULL,
    pid BIGINT NOT NULL,
    severity VARCHAR NOT NULL,
    component VARCHAR NOT NULL,
    address_context VARCHAR,
    request_id VARCHAR,
    instance_id VARCHAR,
    is_labeled_anomaly_instance BOOLEAN,
    message VARCHAR NOT NULL,
    event_id VARCHAR,
    event_template VARCHAR,
    event_time TIMESTAMP NOT NULL"""
    return f"CREATE TABLE IF NOT EXISTS {table} (\n    {fields}\n)"


def _create_and_refresh_search_index(
    connection: DatabaseConnection,
    dataset: str,
    table: str,
    index: str,
) -> None:
    try:
        with connection.cursor() as cursor:
            cursor.execute(CREATE_TEXT_SEARCH_DICTIONARY_SQL)
        connection.commit()
    except psycopg.Error as error:
        connection.rollback()
        if error.sqlstate != "42710":
            raise

    extras = (
        "anomaly_label, event_id"
        if dataset == "bgl"
        else "request_id, instance_id, is_labeled_anomaly_instance, source_class, event_id"
    )
    with connection.cursor() as cursor:
        cursor.execute(
            f"""CREATE INDEX {index} ON {table}
            USING inverted (
                line_id,
                message {TEXT_SEARCH_DICTIONARY},
                event_time,
                component,
                severity,
                {extras}
            )"""
        )
    connection.commit()
    with connection.cursor() as cursor:
        cursor.execute(f"VACUUM (REFRESH_TABLE) {table}")
    connection.commit()


def _verify_dataset_archive(dataset: str, manifest: Mapping[str, Any]) -> None:
    source = manifest["sources"][dataset]
    path = PROJECT_ROOT / source["archive_path"]
    verify_archive(path, source["archive_size_bytes"], source["archive_sha256"])


def _source_evidence(
    dataset: str, manifest: Mapping[str, Any]
) -> tuple[SourceFileEvidence, ...]:
    source = manifest["sources"][dataset]
    return tuple(
        SourceFileEvidence(
            url=source["url"],
            archive_path=source["archive_path"],
            archive_size_bytes=source["archive_size_bytes"],
            archive_sha256=source["archive_sha256"],
            member_name=member["name"],
            member_size_bytes=member["size_bytes"],
            member_sha256=member["sha256"],
            role=member["role"],
        )
        for member in source["members"]
    )


def _build_report(
    dataset: str,
    table: str,
    mutable: _MutableReport,
    *,
    inserted_records: int,
    sources: Sequence[SourceFileEvidence],
    policy: SelectionPolicy,
    phase_seconds: Mapping[str, float],
    elapsed_seconds: float,
) -> IngestionReport:
    return IngestionReport(
        dataset=dataset,
        target_table=table,
        attempted_records=mutable.attempted,
        parsed_records=mutable.parsed,
        rejected_records=mutable.rejected,
        parsing_error_count=mutable.rejected,
        inserted_records=inserted_records,
        source_files=tuple(sources),
        error_examples=tuple(mutable.errors),
        selection={
            "stride": policy.stride,
            "max_records_per_file": policy.max_records_per_file,
            "include_first_per_component": policy.include_first_per_component,
        },
        event_fields={
            "event_id": "NULL: not present in official raw archive",
            "event_template": "NULL: not present in official raw archive",
        },
        phase_seconds={
            name: round(seconds, 3) for name, seconds in phase_seconds.items()
        },
        elapsed_seconds=round(elapsed_seconds, 3),
    )
