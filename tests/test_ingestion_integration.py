from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
import pytest

from log_insights.config import Settings
from log_insights.db import connect
from log_insights.ingestion import (
    BGL_SEARCH_INDEX,
    BGL_TABLE,
    OPENSTACK_SEARCH_INDEX,
    OPENSTACK_TABLE,
    read_bgl_csv,
    read_openstack_csv,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SERENEDB_INTEGRATION_TESTS") != "1",
    reason="Set RUN_SERENEDB_INTEGRATION_TESTS=1 to test the loaded local SereneDB",
)


def test_loaded_samples_and_full_text_indexes() -> None:
    project_root = Path(__file__).resolve().parents[1]
    expected_bgl = read_bgl_csv(
        project_root / "data" / "raw" / "BGL_2k.log_structured.csv"
    )
    expected_openstack = read_openstack_csv(
        project_root / "data" / "raw" / "OpenStack_2k.log_structured.csv"
    )

    load_dotenv()
    with connect(Settings.from_env()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) AS count FROM {BGL_TABLE}")
            assert cursor.fetchone()["count"] == 2_000
            cursor.execute(f"SELECT COUNT(*) AS count FROM {OPENSTACK_TABLE}")
            assert cursor.fetchone()["count"] == 2_000

            cursor.execute(
                f"SELECT line_id, component, severity, message, event_id, event_time "
                f"FROM {BGL_TABLE} ORDER BY line_id"
            )
            actual_bgl = [tuple(row.values()) for row in cursor.fetchall()]
            assert actual_bgl == [
                (row[0], row[8], row[9], row[10], row[11], row[13])
                for row in expected_bgl
            ]

            cursor.execute(
                f"SELECT line_id, component, severity, message, event_id, event_time "
                f"FROM {OPENSTACK_TABLE} ORDER BY line_id"
            )
            actual_openstack = [tuple(row.values()) for row in cursor.fetchall()]
            assert actual_openstack == [
                (row[0], row[6], row[5], row[8], row[9], row[11])
                for row in expected_openstack
            ]

            cursor.execute(
                f"SELECT COUNT(*) AS count FROM {BGL_SEARCH_INDEX} "
                "WHERE message @@ 'core' "
                "AND event_time >= TIMESTAMP '2005-06-03 00:00:00'"
            )
            assert cursor.fetchone()["count"] > 0
            cursor.execute(
                f"SELECT COUNT(*) AS count FROM {OPENSTACK_SEARCH_INDEX} "
                "WHERE message @@ 'instance' "
                "AND event_time >= TIMESTAMP '2017-05-16 00:00:00'"
            )
            assert cursor.fetchone()["count"] > 0

            for table in (BGL_TABLE, OPENSTACK_TABLE):
                cursor.execute(
                    f"SELECT component, severity, COUNT(*) AS count FROM {table} "
                    "GROUP BY component, severity"
                )
                assert sum(row["count"] for row in cursor.fetchall()) == 2_000
