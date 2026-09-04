"""Shared pytest fixtures.

Database-backed tests run against a dedicated `temporal_test` database that
is created from schema.sql once per session and dropped afterwards, so they
never touch development data.
"""

import os
import pathlib

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_PATH = PROJECT_ROOT / "schema.sql"
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
TEST_DB_NAME = os.getenv("TEST_DB_NAME", "temporal_test")


def _admin_dsn():
    """Connection settings for the maintenance database."""
    from backend import config
    return {
        "host": config.DB_HOST,
        "port": config.DB_PORT,
        "user": config.DB_USER,
        "password": config.DB_PASS,
        "dbname": "postgres",
    }


def _database_available():
    try:
        conn = psycopg2.connect(connect_timeout=3, **_admin_dsn())
        conn.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def database_url():
    """Create the test database from schema.sql; drop it when the run ends."""
    if not _database_available():
        pytest.skip("No database reachable. Start one with: docker compose up -d db")

    admin = _admin_dsn()
    conn = psycopg2.connect(**admin)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with conn.cursor() as cur:
        cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(TEST_DB_NAME)))
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB_NAME)))
    conn.close()

    target = dict(admin, dbname=TEST_DB_NAME)
    conn = psycopg2.connect(**target)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with conn.cursor() as cur:
        cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        # Tests must run against the same shape as a migrated production
        # database, not against the bare baseline.
        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            cur.execute(migration.read_text(encoding="utf-8"))
    conn.close()

    yield target

    conn = psycopg2.connect(**admin)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with conn.cursor() as cur:
        cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(TEST_DB_NAME)))
    conn.close()


@pytest.fixture(scope="session")
def db_pool(database_url):
    """Repoint the application's connection pool at the test database."""
    from backend import config, database

    original = (config.DB_NAME, config.DB_HOST, config.DB_PORT)
    config.DB_NAME = database_url["dbname"]
    config.DB_HOST = database_url["host"]
    config.DB_PORT = database_url["port"]
    database.close_pool()

    yield database

    database.close_pool()
    config.DB_NAME, config.DB_HOST, config.DB_PORT = original


@pytest.fixture
def clean_db(db_pool):
    """Truncate the transactional tables between tests.

    Assets and the price cache are left alone: they are reference data seeded
    by schema.sql, and every trading test needs a priced asset.
    """
    with db_pool.transaction() as cur:
        cur.execute(
            """
            TRUNCATE realized_pnl, trades, orders, portfolio,
                     audit_logs, wallets, users
            RESTART IDENTITY CASCADE
            """
        )
    return db_pool


@pytest.fixture
def asset_id(clean_db):
    """An asset guaranteed to have a current price."""
    row = clean_db.query_one("SELECT asset_id FROM assets ORDER BY asset_id LIMIT 1")
    assert row, "schema.sql should seed the assets table"
    aid = row[0]
    # place_order() reads latest_prices, which the market_data trigger feeds.
    clean_db.execute_query(
        "INSERT INTO market_data (asset_id, price, time, source) "
        "VALUES (%s, %s, NOW(), 'test')",
        (aid, 100.00),
    )
    return aid


@pytest.fixture
def set_price(clean_db):
    """Push a new market price for an asset, updating the latest-price cache."""
    def _set(aid, price):
        clean_db.execute_query(
            "INSERT INTO market_data (asset_id, price, time, source) "
            "VALUES (%s, %s, NOW(), 'test')",
            (aid, price),
        )
    return _set


@pytest.fixture
def make_user(clean_db):
    """Create a user (and, via trigger, their wallet). Returns user_id."""
    counter = {"n": 0}

    def _make(balance=None):
        counter["n"] += 1
        name = f"trader{counter['n']}"
        user_id = clean_db.query_value(
            "INSERT INTO users (username, email, password_hash) "
            "VALUES (%s, %s, %s) RETURNING user_id",
            (name, f"{name}@example.com", "x"),
        )
        if balance is not None:
            clean_db.execute_query(
                "UPDATE wallets SET balance = %s WHERE user_id = %s",
                (balance, user_id),
            )
        return user_id

    return _make


@pytest.fixture
def app():
    """Flask app with CSRF disabled so tests can exercise the views directly."""
    from backend.app import create_app
    return create_app(TESTING=True, WTF_CSRF_ENABLED=False)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def csrf_app():
    """Flask app with CSRF left ON, for the tests asserting it is enforced."""
    from backend.app import create_app
    return create_app(TESTING=True)
