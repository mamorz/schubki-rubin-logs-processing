"""Opt-in SELECT-only validation of an explicitly supplied SQL-managed database."""

import os

import pytest

from log_insights.config import Settings
from log_insights.db import connect
from log_insights.queries import QueryService, SQL_DATASET_SPECS
from scripts.check_sql_backend import check_sql_backend

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SQL_BACKEND_INTEGRATION_TESTS") != "1",
    reason="Requires explicit SQL-backend DSN and preloaded schema/indexes",
)


def test_main_schema_readonly_queries():
    # Deliberately do not search parent directories for a .env file.
    settings = Settings.from_env()
    settings.require_serenedb_dsn()
    with connect(settings) as connection:
        report = check_sql_backend(connection)
    service = QueryService(lambda: connect(settings), SQL_DATASET_SPECS)
    for dataset, term in (("bgl", "core"), ("openstack", "instance")):
        count = report["datasets"][dataset]["rows"]
        for grouping in ("component", "severity", "event_id", "team", "time"):
            result = service.summarize_logs(
                dataset=dataset, group_by=grouping,
                interval="hour" if grouping == "time" else None,
            )
            assert result.total_matching_record_count == count
            assert result.development_sample is False
        search = service.search_logs(dataset=dataset, text=term)
        assert search.total_corpus_match_count <= count
        if search.rows:
            nearby = service.get_nearby_events(dataset=dataset, record_id=search.rows[0]["record_id"])
            assert nearby.found
