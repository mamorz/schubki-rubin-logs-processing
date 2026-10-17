"""Print non-secret validation evidence for the bounded full-data staging load."""

from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from log_insights.config import Settings
from log_insights.db import connect
from log_insights.full_ingestion import (
    BGL_STAGING_SEARCH_INDEX,
    BGL_STAGING_TABLE,
    OPENSTACK_STAGING_SEARCH_INDEX,
    OPENSTACK_STAGING_TABLE,
)
from log_insights.ingestion import BGL_TABLE, OPENSTACK_TABLE
from log_insights.ownership import OWNERSHIP_TABLE

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    settings = Settings.from_env()
    datasets = {
        "bgl": (BGL_STAGING_TABLE, BGL_STAGING_SEARCH_INDEX, "cache"),
        "openstack": (
            OPENSTACK_STAGING_TABLE,
            OPENSTACK_STAGING_SEARCH_INDEX,
            "instance",
        ),
    }
    output: dict[str, Any] = {"sample_table_counts": {}, "staging": {}}
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            for name, table in (("bgl", BGL_TABLE), ("openstack", OPENSTACK_TABLE)):
                cursor.execute(f"SELECT COUNT(*) AS count FROM {table}")
                output["sample_table_counts"][name] = int(cursor.fetchone()["count"])

            for name, (table, index, term) in datasets.items():
                cursor.execute(
                    f"""SELECT COUNT(*) AS row_count,
                        COUNT(DISTINCT line_id) AS distinct_record_ids,
                        MIN(event_time) AS earliest_event_time,
                        MAX(event_time) AS latest_event_time,
                        SUM(CASE WHEN event_id IS NULL THEN 1 ELSE 0 END)
                            AS missing_event_ids,
                        SUM(CASE WHEN event_template IS NULL THEN 1 ELSE 0 END)
                            AS missing_event_templates
                    FROM {table}"""
                )
                validation = dict(cursor.fetchone())
                for field in (
                    "row_count",
                    "distinct_record_ids",
                    "missing_event_ids",
                    "missing_event_templates",
                ):
                    validation[field] = int(validation[field])
                if name == "openstack":
                    cursor.execute(
                        f"""SELECT
                            SUM(CASE WHEN is_labeled_anomaly_instance THEN 1 ELSE 0 END)
                                AS labeled_anomaly_records,
                            SUM(CASE WHEN is_labeled_anomaly_instance = false THEN 1 ELSE 0 END)
                                AS labeled_normal_instance_records,
                            SUM(CASE WHEN is_labeled_anomaly_instance IS NULL THEN 1 ELSE 0 END)
                                AS records_without_instance_marker
                        FROM {table}"""
                    )
                    validation["anomaly_instance_evidence"] = {
                        key: int(value) for key, value in cursor.fetchone().items()
                    }
                cursor.execute(
                    f"""SELECT component, COUNT(*) AS record_count
                    FROM {table}
                    GROUP BY component
                    ORDER BY component"""
                )
                components = cursor.fetchall()
                cursor.execute(
                    f"""SELECT severity, COUNT(*) AS record_count
                    FROM {table}
                    GROUP BY severity
                    ORDER BY severity"""
                )
                severities = cursor.fetchall()
                cursor.execute(
                    f"SELECT COUNT(*) AS match_count FROM {index} WHERE message @@ %s",
                    (term,),
                )
                search_matches = int(cursor.fetchone()["match_count"])
                cursor.execute(
                    f"""SELECT
                        COUNT(DISTINCT l.component) AS total_components,
                        COUNT(DISTINCT o.component) AS mapped_components,
                        SUM(CASE WHEN o.component IS NULL THEN 1 ELSE 0 END)
                            AS unmapped_records
                    FROM {table} AS l
                    LEFT JOIN {OWNERSHIP_TABLE} AS o
                      ON o.dataset = %s AND o.component = l.component""",
                    (name,),
                )
                ownership = dict(cursor.fetchone())
                ownership = {key: int(value) for key, value in ownership.items()}
                output["staging"][name] = {
                    "table": table,
                    "index": index,
                    "validation": validation,
                    "components": components,
                    "severities": severities,
                    "full_text_probe": {"term": term, "matches": search_matches},
                    "ownership_coverage": ownership,
                }
    print(json.dumps(output, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
