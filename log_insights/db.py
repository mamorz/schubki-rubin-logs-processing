"""SereneDB connection boundary."""

from __future__ import annotations

import logging
import math
from typing import Any, Callable

import psycopg
from psycopg import Connection
from psycopg import sql
from psycopg.rows import dict_row

from .config import Settings

DatabaseConnection = Connection[dict[str, Any]]
ConnectionFactory = Callable[[], DatabaseConnection]
logger = logging.getLogger(__name__)
_CANCEL_GRACE_SECONDS = 1.0


class _QueryConnection(Connection):
    """Bound driver waits and cancel SQL missed by SereneDB's native deadline.

    In 26.09.1.3 max_execution_time interrupts analytical operators but not all
    blocking functions (notably pg_sleep). A timed-out wait therefore sends a
    PostgreSQL CancelRequest and drains its response before discarding the
    session. No timer thread can race with a subsequent statement.
    """

    execution_timeout_seconds: float | None = None

    def wait(self, gen, *args, timeout=None, **kwargs):
        limit = self.execution_timeout_seconds
        if timeout is not None:
            limit = timeout if limit is None else min(limit, timeout)
        try:
            return super().wait(gen, *args, timeout=limit, **kwargs)
        except psycopg.errors._WaitTimeout as error:
            # _WaitTimeout is the documented exception from Connection.wait in
            # our pinned psycopg 3.3.6. Cancelling the await alone cannot stop SQL.
            try:
                self.cancel_safe(timeout=_CANCEL_GRACE_SECONDS)
                try:
                    super().wait(gen, *args, timeout=_CANCEL_GRACE_SECONDS, **kwargs)
                except psycopg.errors.QueryCanceled:
                    pass  # Server acknowledged cancellation.
            except psycopg.Error:
                logger.warning("Database did not acknowledge deadline cancellation", exc_info=True)
            finally:
                # Even a success racing with the deadline discards this session.
                self.close()
            raise psycopg.errors.QueryCanceled("MCP SQL execution deadline exceeded") from error


def connect(settings: Settings, *, timeout_seconds: float | None = None) -> DatabaseConnection:
    """Open a SereneDB connection using the organizer-supplied DSN."""

    if timeout_seconds is None:
        return psycopg.connect(settings.require_serenedb_dsn(), row_factory=dict_row)
    connection = _QueryConnection.connect(settings.require_serenedb_dsn(), row_factory=dict_row)
    connection.execution_timeout_seconds = timeout_seconds
    return connection


def connection_factory(settings: Settings) -> ConnectionFactory:
    """Open isolated MCP query sessions with a database-enforced deadline.

    SereneDB 26.09.1.3 accepts statement_timeout without enforcing it. Its
    session-scoped max_execution_time is enforced and reports SQLSTATE 57014.
    Keep bulk loaders using connect() outside this MCP-only execution policy.
    """

    timeout_ms = math.ceil(settings.sql_timeout_seconds * 1000)

    def open_query_connection() -> DatabaseConnection:
        connection = connect(settings, timeout_seconds=settings.sql_timeout_seconds)
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("SET SESSION max_execution_time = {}").format(sql.Literal(timeout_ms)))
                cursor.execute("SHOW max_execution_time")
                applied = cursor.fetchone()
                if applied is None or applied.get("max_execution_time") not in (timeout_ms, str(timeout_ms)):
                    raise psycopg.NotSupportedError("Database did not apply the MCP execution deadline")
            # Finish setup before the tool opens its own read transaction.
            connection.commit()
        except BaseException:
            connection.close()
            raise
        return connection

    return open_query_connection
