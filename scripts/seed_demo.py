"""Seed the database with synthetic market history.

A fresh install has 25 assets but no prices, so every chart is empty and
place_order() fails with 'No price data available'. This fills in a plausible
history so the platform is usable immediately, without waiting on Binance or
Yahoo Finance.

    python -m scripts.seed_demo              # 90 days, default
    python -m scripts.seed_demo --days 365
    python -m scripts.seed_demo --reset      # clear existing prices first

The data is generated, not real. `source` is set to 'seed_demo' so it can be
told apart from ingested prices at any time.
"""

import argparse
import logging
import random
from datetime import datetime, timedelta, timezone

from psycopg2.extras import execute_values

from backend import database

logger = logging.getLogger("seed_demo")

# Rough starting points so the generated series look sane per asset class.
# These are round numbers, not quotes.
BASE_PRICES = {
    "BTC": 62000, "ETH": 3400, "SOL": 145, "ADA": 0.45, "XRP": 0.52,
    "DOGE": 0.12, "DOT": 6.20, "LINK": 14.50, "MATIC": 0.58, "AVAX": 27.00,
    "LTC": 72.00, "SHIB": 0.000018,
    "AAPL": 210, "TSLA": 178, "NVDA": 118, "MSFT": 420, "AMZN": 185,
    "GOOGL": 175, "META": 495, "NFLX": 640, "DIS": 92, "ADBE": 520,
    "INTC": 31, "AMD": 158, "CRM": 245,
}
DEFAULT_BASE = 100.0

# Crypto moves more than equities, so give it a wider daily step.
DAILY_VOLATILITY = {"crypto": 0.045, "stock": 0.018}

# Ticks generated for the final day, so the 1D chart and order book populate.
INTRADAY_TICKS = 96


def generate_series(base_price, days, volatility, ticks):
    """Random walk backwards from today, then a final intraday tail.

    Walking backwards keeps the most recent price anchored near `base_price`,
    which matters because that is the price orders will execute at.
    """
    now = datetime.now(timezone.utc)
    points = []

    price = base_price
    for day in range(days):
        stamp = now - timedelta(days=day + 1)
        # Divide rather than multiply, so the walk reads forwards in time.
        price = price / (1 + random.gauss(0, volatility))
        points.append((stamp, max(price, base_price * 0.05)))

    points.sort(key=lambda p: p[0])

    # Intraday tail across the last 24 hours, converging back on base_price.
    step = timedelta(hours=24) / ticks
    price = points[-1][1] if points else base_price
    for i in range(ticks):
        stamp = now - timedelta(hours=24) + step * (i + 1)
        drift = (base_price - price) / max(ticks - i, 1)
        price = price + drift + random.gauss(0, base_price * volatility * 0.15)
        points.append((stamp, max(price, base_price * 0.05)))

    return points


def seed(days, reset):
    assets = database.query_all(
        "SELECT asset_id, symbol, type FROM assets ORDER BY asset_id")
    if not assets:
        raise SystemExit("No assets found. Apply schema.sql first.")

    if reset:
        logger.info("Clearing existing market data")
        with database.transaction() as cur:
            cur.execute("TRUNCATE market_data")
            cur.execute("DELETE FROM latest_prices_cache")

    total = 0
    for asset_id, symbol, asset_type in assets:
        base = BASE_PRICES.get(symbol, DEFAULT_BASE)
        volatility = DAILY_VOLATILITY.get(asset_type, 0.02)
        series = generate_series(base, days, volatility, INTRADAY_TICKS)

        rows = [(asset_id, round(price, 8), stamp, "seed_demo")
                for stamp, price in series]

        # One round trip per asset. Inserting these one at a time would mean
        # tens of thousands of separate statements.
        with database.transaction() as cur:
            execute_values(
                cur,
                "INSERT INTO market_data (asset_id, price, time, source) VALUES %s",
                rows,
                page_size=1000,
            )
        total += len(rows)
        logger.info("%-6s %5d points, latest %.6f", symbol, len(rows), series[-1][1])

    logger.info("Inserted %d price points across %d assets", total, len(assets))
    return total


def refresh_aggregate():
    """Materialise the continuous aggregate so OHLC charts render at once."""
    try:
        # refresh_continuous_aggregate cannot run inside a transaction block.
        conn = database.get_db_connection()
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "CALL refresh_continuous_aggregate('market_data_daily', NULL, NULL)"
                )
        finally:
            conn.autocommit = False
            database.release_db_connection(conn)
        logger.info("Continuous aggregate refreshed")
    except Exception as exc:
        logger.warning("Could not refresh continuous aggregate: %s", exc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=90,
                        help="days of daily history to generate (default: 90)")
    parser.add_argument("--reset", action="store_true",
                        help="delete existing market data first")
    parser.add_argument("--seed", type=int, default=None,
                        help="RNG seed, for reproducible output")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.seed is not None:
        random.seed(args.seed)

    seed(args.days, args.reset)
    refresh_aggregate()

    priced = database.query_value("SELECT count(*) FROM latest_prices_cache")
    logger.info("Assets with a current price: %s", priced)


if __name__ == "__main__":
    main()
