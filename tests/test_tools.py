import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from log_analysis import db, server


class ToolsTest(unittest.TestCase):
    def test_missing_password_fails_without_connecting(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(db.psycopg, "connect") as connect:
            with self.assertRaises(RuntimeError):
                db.connect()
            connect.assert_not_called()

    def test_dataset_cannot_be_sql(self):
        with self.assertRaises(ValueError):
            server.table_for("bgl; DROP TABLE bgl_logs")

    def test_search_binds_input_and_escapes_wildcards(self):
        start = datetime(2005, 1, 1, tzinfo=timezone.utc)
        end = datetime(2006, 1, 1, tzinfo=timezone.utc)
        with patch.object(server, "query", return_value=[]) as query:
            server.search_logs("bgl", "a%'_!", start, end, 12)
            statement, parameters = query.call_args.args
            self.assertNotIn("a%", statement.as_string())
            self.assertEqual(parameters, (start, end, "%a!%'!_!!%", 12))

    def test_limits_and_dates_rejected_before_database(self):
        now = datetime.now(timezone.utc)
        with patch.object(server, "query") as query:
            for limit in (0, 201):
                with self.assertRaises(ValueError):
                    server.search_logs("bgl", "test", now, now, limit)
            with self.assertRaises(ValueError):
                server.summarize_logs("openstack", now, now)
            query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
