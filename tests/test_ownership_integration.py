from __future__ import annotations

import os

from dotenv import load_dotenv
import pytest

from log_insights.config import Settings
from log_insights.db import connect
from log_insights.ownership import (
    OWNERSHIP_TABLE,
    load_ownership_mapping,
    read_ownership_mapping,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SERENEDB_INTEGRATION_TESTS") != "1",
    reason="Set RUN_SERENEDB_INTEGRATION_TESTS=1 to test the loaded local SereneDB",
)


def test_ownership_loader_is_idempotent_in_serenedb() -> None:
    load_dotenv()
    settings = Settings.from_env()
    expected = sorted(record.as_tuple() for record in read_ownership_mapping())

    with connect(settings) as connection:
        first = load_ownership_mapping(connection)
        second = load_ownership_mapping(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT dataset, component, team, mapping_version, provenance "
                f"FROM {OWNERSHIP_TABLE} ORDER BY dataset, component"
            )
            actual = [tuple(row.values()) for row in cursor.fetchall()]

    assert first == second
    assert second.row_count == 15
    assert actual == expected
