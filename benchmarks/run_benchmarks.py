"""TimescaleDB benchmark suite.

Measures the things this project actually claims to do well:

  1. Ingest throughput      - COPY into a hypertable
  2. Storage compression    - native columnar compression on old chunks
  3. Continuous aggregates  - pre-computed OHLC vs. aggregating raw ticks
  4. Latest-price cache     - a cache table vs. DISTINCT ON over the hypertable
  5. Query latency          - p50 / p95 / p99 for the hot dashboard queries

Everything runs against a dedicated `temporal_bench` database built from
schema.sql, so development data is never touched.

    python -m benchmarks.run_benchmarks                  # 5M rows
    python -m benchmarks.run_benchmarks --rows 20000000
    python -m benchmarks.run_benchmarks --keep           # don't drop the db

Results are written to benchmarks/RESULTS.md.
"""

import argparse
import io
import pathlib
import platform
import random
import statistics
import time
from datetime import UTC, datetime, timedelta

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import (
    ISOLATION_LEVEL_AUTOCOMMIT,
    ISOLATION_LEVEL_READ_COMMITTED,
)

from backend import config

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_PATH = PROJECT_ROOT / "schema.sql"
RESULTS_PATH = PROJECT_ROOT / "benchmarks" / "RESULTS.md"

BENCH_DB = "temporal_bench"
COPY_CHUNK_ROWS = 250_000

# Spread the data over two years so most chunks fall outside the 7-day
# compression window and can actually be compressed.
HISTORY_DAYS = 730


# --------------------------------------------------------------------------
# Infrastructure
# --------------------------------------------------------------------------


def admin_connection(dbname="postgres"):
    conn = psycopg2.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASS,
        dbname=dbname,
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


def create_bench_database():
    print(f"Creating {BENCH_DB} from schema.sql ...")
    conn = admin_connection()
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(BENCH_DB))
        )
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(BENCH_DB)))
    conn.close()

    conn = admin_connection(BENCH_DB)
    with conn.cursor() as cur:
        cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.close()


def drop_bench_database():
    conn = admin_connection()
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(BENCH_DB))
        )
    conn.close()


def timed(cur, query, params=None, runs=10, warmup=2):
    """Execute a query repeatedly and return latency percentiles in ms."""
    for _ in range(warmup):
        cur.execute(query, params or ())
        cur.fetchall()

    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        cur.execute(query, params or ())
        cur.fetchall()
        samples.append((time.perf_counter() - start) * 1000)

    samples.sort()
    return {
        "mean": statistics.fmean(samples),
        "p50": samples[len(samples) // 2],
        "p95": samples[min(int(len(samples) * 0.95), len(samples) - 1)],
        "p99": samples[min(int(len(samples) * 0.99), len(samples) - 1)],
        "min": samples[0],
    }


def human_bytes(n):
    if n is None:
        return "n/a"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024:
            return f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} PB"


# --------------------------------------------------------------------------
# 1. Ingest
# --------------------------------------------------------------------------


def load_market_data(conn, total_rows):
    """Bulk-load synthetic ticks with COPY. Returns (rows, seconds)."""
    with conn.cursor() as cur:
        cur.execute("SELECT asset_id FROM assets ORDER BY asset_id")
        assets = [r[0] for r in cur.fetchall()]

    rows_per_asset = total_rows // len(assets)
    span = timedelta(days=HISTORY_DAYS)
    step = span / rows_per_asset
    start_time = datetime.now(UTC) - span

    # The per-row price-cache trigger would fire millions of times during a
    # bulk load, dominating the measurement and saying nothing about the
    # hypertable itself. Drop it for the load, then repopulate set-based.
    #
    # TimescaleDB rejects ALTER TABLE ... DISABLE TRIGGER on a hypertable with
    # compression enabled, so the trigger is dropped and recreated rather than
    # toggled. The trigger function itself is left in place.
    with conn.cursor() as cur:
        cur.execute("DROP TRIGGER IF EXISTS trg_update_latest_price_cache ON market_data")
    conn.commit()

    print(
        f"Loading {total_rows:,} rows across {len(assets)} assets "
        f"({rows_per_asset:,} each over {HISTORY_DAYS} days) ..."
    )

    written = 0
    elapsed = 0.0
    for asset_id in assets:
        price = random.uniform(20, 500)
        index = 0
        while index < rows_per_asset:
            batch = min(COPY_CHUNK_ROWS, rows_per_asset - index)
            buf = io.StringIO()
            for _ in range(batch):
                price *= 1 + random.gauss(0, 0.0015)
                price = max(price, 0.01)
                stamp = start_time + step * index
                buf.write(f"{asset_id},{price:.6f},{stamp.isoformat()},benchmark\n")
                index += 1
            buf.seek(0)

            start = time.perf_counter()
            with conn.cursor() as cur:
                cur.copy_expert(
                    "COPY market_data (asset_id, price, time, source) FROM STDIN WITH (FORMAT csv)",
                    buf,
                )
            conn.commit()
            elapsed += time.perf_counter() - start
            written += batch

        print(f"  loaded {written:>12,} rows", end="\r")

    print(f"  loaded {written:,} rows in {elapsed:,.1f}s ({written / elapsed:,.0f} rows/sec)      ")

    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TRIGGER trg_update_latest_price_cache
            AFTER INSERT ON market_data
            FOR EACH ROW EXECUTE FUNCTION fn_update_latest_price_cache()
            """
        )
        cur.execute(
            """
            INSERT INTO latest_prices_cache (asset_id, price, time)
            SELECT DISTINCT ON (asset_id) asset_id, price, time
            FROM market_data ORDER BY asset_id, time DESC
            ON CONFLICT (asset_id) DO UPDATE
              SET price = EXCLUDED.price, time = EXCLUDED.time
            """
        )
    conn.commit()

    return written, elapsed


# --------------------------------------------------------------------------
# 2. Storage and compression
# --------------------------------------------------------------------------


def hypertable_bytes(cur, name="market_data"):
    cur.execute("SELECT total_bytes FROM hypertable_detailed_size(%s)", (name,))
    values = [r[0] for r in cur.fetchall() if r[0] is not None]
    return sum(values) if values else None


def compress_old_chunks(conn):
    """Compress every chunk outside the 7-day policy window."""
    start = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT compress_chunk(c, if_not_compressed => true) "
            "FROM show_chunks('market_data', older_than => INTERVAL '7 days') c"
        )
    conn.commit()
    elapsed = time.perf_counter() - start

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM timescaledb_information.chunks "
            "WHERE hypertable_name = 'market_data' AND is_compressed"
        )
        compressed = cur.fetchone()[0]

    return compressed, elapsed


# --------------------------------------------------------------------------
# 3-5. Query benchmarks
# --------------------------------------------------------------------------

Q_LATEST_CACHE = "SELECT asset_id, price, time FROM latest_prices_cache"

Q_LATEST_SCAN = """
    SELECT DISTINCT ON (asset_id) asset_id, price, time
    FROM market_data ORDER BY asset_id, time DESC
"""

Q_OHLC_CAGG = """
    SELECT bucket, open, high, low, close
    FROM market_data_daily
    WHERE asset_id = %s AND bucket >= NOW() - (%s * INTERVAL '1 day')
    ORDER BY bucket
"""

Q_OHLC_RAW = """
    SELECT time_bucket('1 day', time) AS bucket,
           FIRST(price, time) AS open, MAX(price) AS high,
           MIN(price) AS low, LAST(price, time) AS close
    FROM market_data
    WHERE asset_id = %s AND time >= NOW() - (%s * INTERVAL '1 day')
    GROUP BY bucket ORDER BY bucket
"""

Q_RANGE_SCAN = """
    SELECT count(*), avg(price), min(price), max(price)
    FROM market_data
    WHERE asset_id = %s AND time >= NOW() - (%s * INTERVAL '1 day')
"""

Q_INDICATORS = """
    WITH daily AS (
        SELECT time_bucket('1 day', time) AS d, asset_id, AVG(price) AS p
        FROM market_data WHERE asset_id = %s
        GROUP BY d, asset_id
    )
    SELECT d, p,
           AVG(p) OVER (PARTITION BY asset_id ORDER BY d
                        ROWS BETWEEN 6 PRECEDING AND CURRENT ROW),
           STDDEV(p) OVER (PARTITION BY asset_id ORDER BY d
                           ROWS BETWEEN 20 PRECEDING AND CURRENT ROW)
    FROM daily ORDER BY d DESC LIMIT 7
"""


def run_query_benchmarks(conn, asset_id):
    results = {}
    with conn.cursor() as cur:
        results["latest_cache"] = timed(cur, Q_LATEST_CACHE, runs=20)
        results["latest_scan"] = timed(cur, Q_LATEST_SCAN, runs=5, warmup=1)

        for days in (30, 365):
            results[f"ohlc_cagg_{days}"] = timed(cur, Q_OHLC_CAGG, (asset_id, days), runs=20)
            results[f"ohlc_raw_{days}"] = timed(cur, Q_OHLC_RAW, (asset_id, days), runs=5, warmup=1)

        results["range_30d"] = timed(cur, Q_RANGE_SCAN, (asset_id, 30), runs=10)
        results["range_365d"] = timed(cur, Q_RANGE_SCAN, (asset_id, 365), runs=5, warmup=1)
        results["indicators"] = timed(cur, Q_INDICATORS, (asset_id,), runs=5, warmup=1)
    return results


def explain(conn, query, params):
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (ANALYZE, BUFFERS, SUMMARY OFF) " + query, params)
        return [r[0] for r in cur.fetchall()]


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def speedup(baseline_ms, candidate_ms):
    if not candidate_ms:
        return "n/a"
    return f"{baseline_ms / candidate_ms:,.1f}x"


def write_report(env, ingest, storage, before_q, after_q, plans):
    rows, load_seconds = ingest
    size_before, size_after, chunk_total, chunks_compressed, compress_seconds = storage

    ratio = size_before / size_after if size_after else 0

    lines = [
        "# Benchmark Results",
        "",
        "Generated by `python -m benchmarks.run_benchmarks`. Every figure below was",
        "measured on the machine and dataset described here - re-run the suite to",
        "reproduce them on your own hardware.",
        "",
        "## Headline",
        "",
        "| Measurement | Result |",
        "|---|---|",
        f"| Ingest throughput | **{rows / load_seconds:,.0f} rows/sec** (COPY into hypertable) |",
        f"| Storage compression | **{ratio:,.1f}x** ({human_bytes(size_before)} -> {human_bytes(size_after)}) |",
        f"| Latest-price lookup | **{speedup(after_q['latest_scan']['p50'], after_q['latest_cache']['p50'])}** faster via cache table |",
        f"| OHLC 30d | **{speedup(after_q['ohlc_raw_30']['p50'], after_q['ohlc_cagg_30']['p50'])}** faster via continuous aggregate |",
        f"| OHLC 365d | **{speedup(after_q['ohlc_raw_365']['p50'], after_q['ohlc_cagg_365']['p50'])}** faster via continuous aggregate |",
        "",
        "## Environment",
        "",
        "| | |",
        "|---|---|",
        f"| Database | {env['pg_version']} |",
        f"| TimescaleDB | {env['ts_version']} |",
        f"| Host | {env['platform']} |",
        f"| Max parallel workers | {env['cpus']} |",
        f"| Dataset | {rows:,} rows, {env['assets']} assets, {HISTORY_DAYS} days |",
        f"| Measured | {env['timestamp']} |",
        "",
        "## 1. Ingest throughput",
        "",
        "Bulk load via `COPY` into the `market_data` hypertable. The per-row",
        "price-cache trigger is dropped for the duration of the load and the",
        "cache is repopulated with a single set-based statement afterwards -",
        "otherwise the trigger fires once per row and dominates the measurement.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Rows loaded | {rows:,} |",
        f"| Wall time | {load_seconds:,.1f} s |",
        f"| **Throughput** | **{rows / load_seconds:,.0f} rows/sec** |",
        "",
        "## 2. Compression",
        "",
        "TimescaleDB native columnar compression on chunks older than 7 days",
        "(`compress_segmentby = 'asset_id'`).",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Chunks total | {chunk_total:,} |",
        f"| Chunks compressed | {chunks_compressed:,} |",
        f"| Size before | {human_bytes(size_before)} |",
        f"| Size after | {human_bytes(size_after)} |",
        f"| **Compression ratio** | **{ratio:,.1f}x** |",
        f"| Space saved | {human_bytes(size_before - size_after)} "
        f"({100 * (1 - size_after / size_before):,.1f}%) |",
        f"| Time to compress | {compress_seconds:,.1f} s |",
        "",
        "## 3. Query latency",
        "",
        "Each query is warmed up, then timed over repeated runs. Times in",
        "milliseconds; lower is better.",
        "",
        "### Latest price for all assets",
        "",
        "`latest_prices_cache` is maintained by a trigger so the hot path never",
        "scans the hypertable.",
        "",
        "| Method | p50 | p95 | p99 | Speedup |",
        "|---|---:|---:|---:|---:|",
    ]

    lc, ls = after_q["latest_cache"], after_q["latest_scan"]
    lines += [
        f"| `latest_prices_cache` lookup | {lc['p50']:,.2f} | {lc['p95']:,.2f} "
        f"| {lc['p99']:,.2f} | **{speedup(ls['p50'], lc['p50'])}** |",
        f"| `DISTINCT ON` over `market_data` | {ls['p50']:,.2f} | {ls['p95']:,.2f} "
        f"| {ls['p99']:,.2f} | baseline |",
        "",
        "### OHLC candles (single asset)",
        "",
        "Continuous aggregate vs. bucketing raw ticks at query time.",
        "",
        "| Window | Method | p50 | p95 | Speedup |",
        "|---|---|---:|---:|---:|",
    ]

    for days in (30, 365):
        cagg = after_q[f"ohlc_cagg_{days}"]
        raw = after_q[f"ohlc_raw_{days}"]
        lines += [
            f"| {days}d | `market_data_daily` (continuous aggregate) "
            f"| {cagg['p50']:,.2f} | {cagg['p95']:,.2f} "
            f"| **{speedup(raw['p50'], cagg['p50'])}** |",
            f"| {days}d | raw `time_bucket` aggregation "
            f"| {raw['p50']:,.2f} | {raw['p95']:,.2f} | baseline |",
        ]

    lines += [
        "",
        "### Effect of compression on scan queries",
        "",
        "The same range scans, measured before and after compressing old chunks.",
        "",
        "| Query | Uncompressed p50 | Compressed p50 | Change |",
        "|---|---:|---:|---:|",
    ]

    for key, label in [
        ("range_30d", "30-day aggregate scan"),
        ("range_365d", "365-day aggregate scan"),
        ("indicators", "SMA-7 + volatility window"),
    ]:
        b, a = before_q[key]["p50"], after_q[key]["p50"]
        direction = "faster" if a < b else "slower"
        factor = (b / a) if a else 0
        if factor < 1 and factor:
            factor = 1 / factor
        lines.append(f"| {label} | {b:,.2f} | {a:,.2f} | {factor:,.2f}x {direction} |")

    lines += [
        "",
        "Compression is not a free speed-up, and these numbers show the real",
        "tradeoff: storage falls sharply while full-range aggregate scans get",
        "*slower*, because every batch must be decompressed before it can be",
        "aggregated. TimescaleDB compression pays off for queries that filter on",
        "the `segmentby` column and for storage cost - not for wide scans over",
        "historical data. The continuous aggregate above is the right answer for",
        "that access pattern, which is why the charts read from it rather than",
        "from raw ticks.",
        "",
        "## 4. Query plans",
        "",
        "Confirmation that the intended access paths are actually used.",
        "",
    ]
    for title, plan in plans.items():
        lines += [f"### {title}", "", "```", *plan[:12], "```", ""]

    lines += [
        "## Reproducing",
        "",
        "```bash",
        "docker compose up -d db",
        f"python -m benchmarks.run_benchmarks --rows {rows}",
        "```",
        "",
        "The suite builds a throwaway `temporal_bench` database from `schema.sql`,",
        "loads synthetic ticks, and drops it again unless `--keep` is passed.",
        "Absolute milliseconds vary with hardware, so the cross-method ratios are",
        "the meaningful result.",
        "",
    ]

    RESULTS_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {RESULTS_PATH.relative_to(PROJECT_ROOT)}")
    return ratio


# --------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rows",
        type=int,
        default=5_000_000,
        help="market_data rows to generate (default: 5,000,000)",
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the benchmark database afterwards"
    )
    parser.add_argument("--seed", type=int, default=7, help="RNG seed")
    args = parser.parse_args()

    random.seed(args.seed)
    overall = time.perf_counter()

    create_bench_database()
    conn = psycopg2.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASS,
        dbname=BENCH_DB,
    )

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            pg_version = cur.fetchone()[0].split(" on ")[0]
            cur.execute("SELECT extversion FROM pg_extension WHERE extname='timescaledb'")
            ts_version = cur.fetchone()[0]
            cur.execute("SHOW max_parallel_workers")
            cpus = cur.fetchone()[0]
            cur.execute("SELECT count(*), min(asset_id) FROM assets")
            asset_count, first_asset = cur.fetchone()

        rows, load_seconds = load_market_data(conn, args.rows)

        print("Refreshing continuous aggregate ...")
        start = time.perf_counter()
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        with conn.cursor() as cur:
            cur.execute("CALL refresh_continuous_aggregate('market_data_daily', NULL, NULL)")
        conn.set_isolation_level(ISOLATION_LEVEL_READ_COMMITTED)
        print(f"  refreshed in {time.perf_counter() - start:,.1f}s")

        with conn.cursor() as cur:
            cur.execute("ANALYZE market_data")
        conn.commit()

        with conn.cursor() as cur:
            size_before = hypertable_bytes(cur)
            cur.execute(
                "SELECT count(*) FROM timescaledb_information.chunks "
                "WHERE hypertable_name = 'market_data'"
            )
            chunk_total = cur.fetchone()[0]

        print("Measuring queries (uncompressed) ...")
        before_q = run_query_benchmarks(conn, first_asset)

        print("Compressing chunks older than 7 days ...")
        chunks_compressed, compress_seconds = compress_old_chunks(conn)
        with conn.cursor() as cur:
            cur.execute("ANALYZE market_data")
        conn.commit()
        with conn.cursor() as cur:
            size_after = hypertable_bytes(cur)
        print(f"  compressed {chunks_compressed}/{chunk_total} chunks in {compress_seconds:,.1f}s")

        print("Measuring queries (compressed) ...")
        after_q = run_query_benchmarks(conn, first_asset)

        plans = {
            "OHLC via continuous aggregate": explain(conn, Q_OHLC_CAGG, (first_asset, 30)),
            "OHLC via raw time_bucket": explain(conn, Q_OHLC_RAW, (first_asset, 30)),
        }

        env = {
            "pg_version": pg_version,
            "ts_version": ts_version,
            "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
            "cpus": cpus,
            "assets": asset_count,
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        }

        ratio = write_report(
            env,
            (rows, load_seconds),
            (size_before, size_after, chunk_total, chunks_compressed, compress_seconds),
            before_q,
            after_q,
            plans,
        )

        print("\n" + "=" * 66)
        print("HEADLINE NUMBERS")
        print("=" * 66)
        print(f"  Ingest        {rows / load_seconds:>12,.0f} rows/sec")
        print(
            f"  Compression   {ratio:>12,.1f}x   "
            f"{human_bytes(size_before)} -> {human_bytes(size_after)}"
        )
        print(
            f"  Latest price  {speedup(after_q['latest_scan']['p50'], after_q['latest_cache']['p50']):>12}   "
            f"faster via cache table"
        )
        print(
            f"  OHLC 30d      {speedup(after_q['ohlc_raw_30']['p50'], after_q['ohlc_cagg_30']['p50']):>12}   "
            f"faster via continuous aggregate"
        )
        print(
            f"  OHLC 365d     {speedup(after_q['ohlc_raw_365']['p50'], after_q['ohlc_cagg_365']['p50']):>12}   "
            f"faster via continuous aggregate"
        )
        print("=" * 66)
        print(f"Total run time: {time.perf_counter() - overall:,.1f}s")

    finally:
        conn.close()
        if not args.keep:
            drop_bench_database()
            print(f"Dropped {BENCH_DB}")
        else:
            print(f"Kept {BENCH_DB} (--keep)")


if __name__ == "__main__":
    main()
