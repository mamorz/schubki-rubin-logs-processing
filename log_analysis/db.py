"""Connection settings come exclusively from the process environment."""
import os
import psycopg
from psycopg.rows import dict_row


def connect():
    password = os.environ.get("SERENEDB_PASSWORD")
    if not password:
        raise RuntimeError("Set SERENEDB_PASSWORD in the process environment first.")
    return psycopg.connect(
        host=os.getenv("SERENEDB_HOST", "127.0.0.1"),
        port=int(os.getenv("SERENEDB_PORT", "7891")),
        dbname=os.getenv("SERENEDB_DATABASE", "postgres"),
        user=os.getenv("SERENEDB_USER", "postgres"),
        password=password,
        connect_timeout=5,
        autocommit=True,
        row_factory=dict_row,
    )


def query(statement, parameters=()):
    with connect() as conn:
        return conn.execute(statement, parameters).fetchall()


def health_check():
    return query("SELECT 1 AS ok, version() AS version")[0]
