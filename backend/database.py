"""PostgreSQL/TimescaleDB access layer.

The connection pool is built lazily on first use so that importing this
module never requires a live database. That matters for two reasons: the
test suite imports the app before pointing it at a throwaway container, and
under Docker the web process may boot before Postgres accepts connections.
"""

import logging
import os
import threading
from contextlib import contextmanager

from psycopg2 import pool as pg_pool

from . import config

logger = logging.getLogger(__name__)

_pool = None
_pool_lock = threading.Lock()


def _build_pool():
    return pg_pool.ThreadedConnectionPool(
        config.DB_POOL_MIN,
        config.DB_POOL_MAX,
        host=config.DB_HOST,
        database=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASS,
        port=config.DB_PORT,
    )


def get_pool():
    """Return the process-wide connection pool, creating it if needed."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _build_pool()
                logger.info(
                    "Connection pool ready (%s:%s/%s, %s-%s connections)",
                    config.DB_HOST,
                    config.DB_PORT,
                    config.DB_NAME,
                    config.DB_POOL_MIN,
                    config.DB_POOL_MAX,
                )
    return _pool


def close_pool():
    """Dispose of the pool. Used by tests and graceful shutdown."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None


def get_db_connection():
    return get_pool().getconn()


def release_db_connection(conn):
    get_pool().putconn(conn)


@contextmanager
def transaction():
    """Run a block of statements inside a single database transaction.

    Commits on clean exit, rolls back on any exception, and always returns
    the connection to the pool with autocommit reset so the next borrower
    inherits a predictable session.

        with database.transaction() as cur:
            cur.execute("SELECT deposit_money(%s, %s)", (user_id, amount))
            cur.execute("SELECT balance FROM wallets WHERE user_id = %s", (user_id,))
            balance = cur.fetchone()[0]
    """
    conn = get_db_connection()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def execute_query(query, params=None, fetch=False):
    """Run a single statement in its own transaction.

    Kept as the workhorse for one-shot reads and writes. Anything that needs
    two statements to be atomic must use transaction() instead.
    """
    with transaction() as cur:
        cur.execute(query, params)
        if fetch:
            return cur.fetchall()
    return None


def query_all(query, params=None):
    """Fetch every row for a read-only query."""
    return execute_query(query, params, fetch=True)


def query_one(query, params=None):
    """Fetch the first row, or None when the query returns nothing."""
    rows = execute_query(query, params, fetch=True)
    return rows[0] if rows else None


def query_value(query, params=None, default=None):
    """Fetch the first column of the first row, or `default`."""
    row = query_one(query, params)
    return row[0] if row is not None else default


def init_db(schema_path=None):
    """Apply schema.sql. Destructive: the script drops existing tables."""
    if schema_path is None:
        schema_path = os.path.join(os.path.dirname(__file__), "..", "schema.sql")
    with open(schema_path, encoding="utf-8") as handle:
        schema_sql = handle.read()

    with transaction() as cur:
        cur.execute(schema_sql)
    logger.info("Schema applied from %s", schema_path)
