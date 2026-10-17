"""Read-only preflight for main's SQL infrastructure; never initializes a DB."""

from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv

from log_insights.config import Settings
from log_insights.db import connect
from log_insights.queries import SQL_DATASET_SPECS


def check_sql_backend(connection) -> dict:
    """SELECT-only checks for columns, timestamps, identity, mapping and indexes.

    Counts are over the current corpus, not an assumed complete or sample load.
    Use an explicitly supplied read-only database account for this command.
    """
    report = {"backend": "sql", "datasets": {}}
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT dataset, component, COUNT(*) AS n FROM ownership "
            "GROUP BY dataset, component HAVING COUNT(*) <> 1 LIMIT 1"
        )
        if cursor.fetchone() is not None:
            raise ValueError("ownership contains duplicate dataset/component keys")
        cursor.execute("SELECT dataset, component, team, area FROM ownership LIMIT 0")
        cursor.execute(
            "SELECT COUNT(*) AS n FROM ownership WHERE dataset IS NULL OR "
            "component IS NULL OR team IS NULL OR team = '' OR team = 'UNMAPPED'"
        )
        if cursor.fetchone()["n"]:
            raise ValueError("ownership contains invalid keys or reserved/empty teams")
        for dataset, spec in SQL_DATASET_SPECS.items():
            cursor.execute(
                f"SELECT {spec.record_columns} FROM {spec.table} LIMIT 0"
            )
            cursor.execute(
                f"SELECT COUNT(*) AS rows, COUNT(DISTINCT {spec.id_expression}) AS ids, "
                f"COUNT(*) FILTER (WHERE ts IS NULL OR component IS NULL OR level IS NULL "
                f"OR line_no <= 0 OR line_no >= 1000000000) AS invalid, "
                f"MIN(ts) AS earliest, MAX(ts) AS latest FROM {spec.table}"
            )
            counts = dict(cursor.fetchone())
            if counts["rows"] != counts["ids"] or counts["invalid"]:
                raise ValueError(f"{dataset} has invalid or duplicate source identities/fields")
            # Checking both projections and actual @@ evaluation detects missing
            # index columns and an unready search dictionary without writing SQL.
            cursor.execute(
                f"SELECT {spec.record_columns} FROM {spec.index_relation} "
                "WHERE message @@ %s LIMIT 1",
                ("core" if dataset == "bgl" else "instance",),
            )
            cursor.fetchone()
            cursor.execute(f"SELECT COUNT(*) AS rows FROM {spec.index_relation}")
            if cursor.fetchone()["rows"] != counts["rows"]:
                raise ValueError(f"{dataset} index visibility differs from table count")
            report["datasets"][dataset] = counts
    return report


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    with connect(Settings.from_env()) as connection:
        print(json.dumps(check_sql_backend(connection), indent=2, default=str))


if __name__ == "__main__":
    main()
