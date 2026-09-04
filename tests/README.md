# Tests

Two layers:

- **`test_security.py`** - auth, CSRF, input validation. No database needed.
- **`test_trading.py`**, **`test_portfolio.py`** - the money paths, run against
  a real TimescaleDB instance because the logic under test lives in Postgres
  triggers and stored procedures. There is no meaningful way to test
  `fn_update_portfolio_after_trade` without Postgres.

## Running

```bash
docker compose up -d db          # TimescaleDB on localhost:5433
pytest                           # everything
pytest -m "not db"               # security tests only, no database
```

Database tests create and drop a dedicated `temporal_test` database, so they
never touch development data.
