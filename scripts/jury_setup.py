"""Fail-closed jury initialization. Default mode only inspects; never loads .env."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import time

import psycopg
from psycopg.rows import dict_row

from scripts.check_sql_backend import check_sql_backend

# Intentionally independent of all environment/CLI connection settings.
JURY_DSN = "host=serenedb port=7890 dbname=postgres user=postgres connect_timeout=5"
SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
SQL_FILES = ("01_schema", "02_load_bgl", "03_load_openstack", "04_load_ownership", "05_indexes")
TABLE_COUNTS = {"bgl_logs": 4_747_963, "openstack_logs": 207_820,
                "openstack_anomalous_instances": 4, "ownership": 22}
SOURCE_COUNTS = {"openstack_normal1": 52_312, "openstack_normal2": 137_074,
                 "openstack_abnormal": 18_434}
EXPECTED_RELATIONS = {(name, "r") for name in TABLE_COUNTS} | {
    ("bgl_logs_idx", "i"), ("openstack_logs_idx", "i")}
# SereneDB 26.09 ships its documentation in this separate schema.
BUNDLED_RELATIONS = {("docs", "r"), ("meta", "r"), ("docs_fts", "i"), ("docs_pkey", "i")}
SYSTEM_SCHEMAS = {"pg_catalog", "information_schema"}


class UnsafeState(RuntimeError):
    """No automatic repairs or continuation are safe."""


def inspect_state(connection) -> str:
    """Prove empty or complete using SELECT only; every other state fails closed."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT nspname FROM pg_catalog.pg_namespace")
        schemas = {row["nspname"] for row in cursor.fetchall()}
        if "public" not in schemas or schemas - SYSTEM_SCHEMAS - {"public", "sdb_docs"}:
            raise UnsafeState(f"Unexpected database schemas: {sorted(schemas)}")
        cursor.execute(
            "SELECT n.nspname, c.relname, c.relkind FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
        )
        relations = set()
        for row in cursor.fetchall():
            schema = row["nspname"]
            relation = (row["relname"], row["relkind"])
            if schema in SYSTEM_SCHEMAS:
                continue
            if schema == "sdb_docs" and relation in BUNDLED_RELATIONS:
                continue
            if schema != "public":
                raise UnsafeState(f"Unexpected relation: {schema}.{relation[0]}")
            relations.add(relation)
        cursor.execute(
            "SELECT n.nspname, d.dictname FROM pg_catalog.pg_ts_dict d "
            "JOIN pg_catalog.pg_namespace n ON n.oid = d.dictnamespace"
        )
        dictionaries = set()
        for row in cursor.fetchall():
            identity = (row["nspname"], row["dictname"])
            if identity[0] in SYSTEM_SCHEMAS or identity == ("sdb_docs", "tokenizer"):
                continue
            dictionaries.add(identity)
        cursor.execute(
            "SELECT n.nspname, p.proname FROM pg_catalog.pg_proc p "
            "JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')"
        )
        functions = {(row["nspname"], row["proname"]) for row in cursor.fetchall()}
        bundled_functions = {("sdb_docs", name) for name in ("read", "reference", "search", "sections")}
        if functions - bundled_functions:
            raise UnsafeState("Unexpected user-defined functions")
        if not relations and not dictionaries:
            return "empty"
        if relations != EXPECTED_RELATIONS or dictionaries != {("public", "log_dict")}:
            raise UnsafeState("Partial or unknown schema, dictionary or indexes; refusing initialization")
        for table, expected in TABLE_COUNTS.items():
            cursor.execute(f"SELECT COUNT(*) AS n FROM public.{table}")
            actual = cursor.fetchone()["n"]
            if actual != expected:
                raise UnsafeState(f"{table}: expected {expected} rows, found {actual}; refusing reload")
        cursor.execute("SELECT source_file, COUNT(*) AS n FROM public.openstack_logs GROUP BY source_file")
        if {row["source_file"]: row["n"] for row in cursor.fetchall()} != SOURCE_COUNTS:
            raise UnsafeState("OpenStack source counts differ from the complete corpus")
    # Reuse the SELECT-only checks for columns, unique IDs/ownership keys,
    # actual full-text search execution, and complete index visibility.
    check_sql_backend(connection)
    return "ready"


def load_sql() -> list[tuple[str, str]]:
    """Read all scripts before any write and disallow destructive SQL regressions."""
    scripts = []
    for name in SQL_FILES:
        sql = (SQL_DIR / f"{name}.sql").read_text()
        code = re.sub(r"--[^\n]*", "", sql)
        if re.search(r"\b(DROP|TRUNCATE|DELETE|UPDATE|ALTER)\b", code, re.I):
            raise UnsafeState(f"Destructive SQL is not allowed in jury initialization: {name}")
        scripts.append((name, sql))
    # The first CREATE is an exclusive claim. A competing setup must fail here,
    # before it can append any rows, even if both observed an empty catalog.
    schema = re.sub(r"--[^\n]*", "", scripts[0][1]).lstrip()
    if not schema.startswith("CREATE TEXT SEARCH DICTIONARY log_dict ("):
        raise UnsafeState("Schema must start with an unconditional CREATE of log_dict")
    return scripts


def setup(connection, *, initialize_empty: bool = False) -> str:
    state = inspect_state(connection)
    print(f"Jury database state: {state}", flush=True)
    if state == "ready":
        print("Complete corpus and indexes verified; skipping all initialization SQL.", flush=True)
        return state
    if not initialize_empty:
        return state
    scripts = load_sql()
    # Recheck immediately before CREATE; SQL errors always propagate, never resume.
    if inspect_state(connection) != "empty":
        raise UnsafeState("Database changed during preflight; refusing initialization")
    for name, sql in scripts:
        print(f"Loading {name}.sql (first startup may take several minutes)...", flush=True)
        with connection.cursor() as cursor:
            cursor.execute(sql, prepare=False)
            while cursor.nextset():
                pass
    if inspect_state(connection) != "ready":
        raise UnsafeState("Initialization did not produce a complete corpus")
    return "ready"


def wait_for_database(timeout: float = 120):
    deadline = time.monotonic() + timeout
    while True:
        try:
            connection = psycopg.connect(JURY_DSN, autocommit=True, row_factory=dict_row)
            try:
                connection.execute("SELECT 1")
            except Exception:
                connection.close()
                raise
            return connection
        except psycopg.OperationalError:
            if time.monotonic() >= deadline:
                raise UnsafeState("Compose SereneDB did not become ready within 120 seconds") from None
            time.sleep(2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initialize-empty", action="store_true")
    args = parser.parse_args()
    try:
        with wait_for_database() as connection:
            setup(connection, initialize_empty=args.initialize_empty)
    except Exception as exc:
        raise SystemExit(
            f"Jury setup stopped: {exc}\n"
            "No automatic repair, rollback, or reload will be attempted. "
            "Preserve the volume and ask the project maintainers to inspect it."
        ) from exc


if __name__ == "__main__":
    main()
