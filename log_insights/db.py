"""SereneDB connection boundary."""

from __future__ import annotations

from typing import Any, Callable

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from .config import Settings

DatabaseConnection = Connection[dict[str, Any]]
ConnectionFactory = Callable[[], DatabaseConnection]


def connect(settings: Settings) -> DatabaseConnection:
    """Open a SereneDB connection using the organizer-supplied DSN."""

    return psycopg.connect(settings.require_serenedb_dsn(), row_factory=dict_row)


def connection_factory(settings: Settings) -> ConnectionFactory:
    """Create a lazy factory so importing the server never opens the database."""

    return lambda: connect(settings)
