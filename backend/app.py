"""Flask application for the Temporal trading platform.

Structured as an application factory with two blueprints (server-rendered
pages and the JSON API) so the test suite can build an isolated app instance
pointed at a throwaway database.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from functools import wraps

import bcrypt
import psycopg2
import yfinance as yf
from apscheduler.schedulers.background import BackgroundScheduler
from flask import (
    Blueprint, Flask, jsonify, redirect,
    render_template, request, session, url_for,
)
from flask_cors import CORS
from flask_wtf.csrf import CSRFError, CSRFProtect

from . import config, database

logger = logging.getLogger(__name__)

csrf = CSRFProtect()
scheduler = BackgroundScheduler()

pages = Blueprint("pages", __name__)
api = Blueprint("api", __name__, url_prefix="/api")

# Generic message returned to clients. Full exception detail is logged
# server-side; leaking psycopg2 text would expose schema internals.
GENERIC_ERROR = "Something went wrong. Please try again."


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def login_required(view):
    """Reject unauthenticated callers.

    API routes get a 401 JSON body; page routes are redirected to login.
    Applying this centrally removes the per-route `if 'user_id' not in
    session` checks that previously had to be remembered by hand.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("pages.index"))
        return view(*args, **kwargs)
    return wrapped


def current_user_id():
    return session["user_id"]


def db_error(exc, message=GENERIC_ERROR, status=400):
    """Log the real failure, return a message that reveals nothing."""
    logger.exception("Database operation failed: %s", exc)
    return jsonify({"error": message}), status


# Business-rule violations we raise ourselves are safe and useful to show.
# Anything else is treated as an internal error and hidden.
_SAFE_DB_ERRORS = (
    "Insufficient balance",
    "Insufficient assets",
    "Insufficient holdings",
    "Quantity must be greater than zero",
    "Invalid order type",
    "Invalid order kind",
    "No price data available",
    "Target price required",
    "Deposit amount must be greater than zero",
    "Withdrawal amount must be greater than zero",
)


def user_facing_db_error(exc, status=400):
    """Surface deliberate RAISE EXCEPTION text from our stored procedures."""
    detail = str(exc).split("\n")[0].strip()
    for prefix in _SAFE_DB_ERRORS:
        if prefix.lower() in detail.lower():
            logger.info("Business rule rejected request: %s", detail)
            return jsonify({"error": prefix}), status
    return db_error(exc, status=status)


def parse_amount(raw):
    """Validate a monetary amount from JSON. Returns (value, error)."""
    if raw is None:
        return None, "Amount is required"
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, "Invalid amount"
    # Rejects NaN and the infinities, which would otherwise reach NUMERIC.
    if value != value or value in (float("inf"), float("-inf")):
        return None, "Invalid amount"
    if value <= 0:
        return None, "Amount must be greater than zero"
    return round(value, 2), None


# --------------------------------------------------------------------------
# Page routes
# --------------------------------------------------------------------------

@pages.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("pages.dashboard"))
    return render_template("login.html")


@pages.route("/register")
def register_page():
    return render_template("register.html")


def _render_dashboard(page):
    return render_template("dashboard.html", username=session["username"], page=page)


@pages.route("/dashboard")
@login_required
def dashboard():
    return _render_dashboard("home")


@pages.route("/markets")
@login_required
def markets():
    return _render_dashboard("markets")


@pages.route("/portfolio")
@login_required
def portfolio_page():
    return _render_dashboard("portfolio")


@pages.route("/trade")
@login_required
def trade_page():
    return _render_dashboard("trade")


@pages.route("/analytics")
@login_required
def analytics_page():
    return _render_dashboard("analytics")


@pages.route("/transactions")
@login_required
def transactions_page():
    return _render_dashboard("transactions")


@pages.route("/healthz")
def healthz():
    """Liveness/readiness probe for containers and uptime checks."""
    try:
        database.query_value("SELECT 1")
    except Exception as exc:
        logger.warning("Health check failed: %s", exc)
        return jsonify({"status": "degraded", "database": "unreachable"}), 503
    return jsonify({"status": "ok", "database": "ok"}), 200


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------

@api.route("/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""

    if not username or not email or not password:
        return jsonify({"error": "Username, email and password are required"}), 400
    if len(username) > 64:
        return jsonify({"error": "Username must be 64 characters or fewer"}), 400
    if "@" not in email or len(email) > 254:
        return jsonify({"error": "Enter a valid email address"}), 400
    if len(password) < config.MIN_PASSWORD_LENGTH:
        return jsonify({
            "error": f"Password must be at least {config.MIN_PASSWORD_LENGTH} characters"
        }), 400
    # bcrypt silently truncates past 72 bytes, which would make the tail of a
    # long passphrase meaningless. Reject rather than quietly weaken it.
    if len(password.encode("utf-8")) > config.MAX_PASSWORD_BYTES:
        return jsonify({
            "error": f"Password must be {config.MAX_PASSWORD_BYTES} bytes or fewer"
        }), 400

    hashed_pw = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    try:
        database.execute_query(
            "INSERT INTO users (username, email, password_hash) VALUES (%s, %s, %s)",
            (username, email, hashed_pw),
        )
    except psycopg2.errors.UniqueViolation:
        # Deliberately does not say which field collided.
        return jsonify({"error": "That username or email is already registered"}), 409
    except Exception as exc:
        return db_error(exc, "Registration failed. Please try again.", 500)

    return jsonify({"message": "User registered successfully"}), 201


@api.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not username or not password:
        return jsonify({"error": "Invalid username or password"}), 401

    try:
        user = database.query_one(
            "SELECT user_id, username, password_hash FROM users WHERE username = %s",
            (username,),
        )
    except Exception as exc:
        return db_error(exc, GENERIC_ERROR, 500)

    # Always run a bcrypt comparison so response time does not reveal
    # whether the username exists.
    stored_hash = user[2] if user else bcrypt.hashpw(b"placeholder", bcrypt.gensalt()).decode()
    matches = bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))

    if not user or not matches:
        return jsonify({"error": "Invalid username or password"}), 401

    # Clearing first prevents pre-login session state carrying over.
    session.clear()
    session["user_id"] = user[0]
    session["username"] = user[1]
    session.permanent = True
    return jsonify({"user_id": user[0], "username": user[1]}), 200


@api.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "Logged out"}), 200


# --------------------------------------------------------------------------
# Wallet
# --------------------------------------------------------------------------

@api.route("/wallet", methods=["GET"])
@login_required
def get_wallet():
    balance = database.query_value(
        "SELECT balance FROM wallets WHERE user_id = %s", (current_user_id(),)
    )
    return jsonify({"balance": float(balance or 0)})


def _wallet_operation(sql_function, amount):
    """Move money and read the resulting balance in one transaction.

    Running these as two separate statements previously left a window where a
    concurrent trade could change the balance between the write and the read,
    returning a figure that never actually existed.
    """
    user_id = current_user_id()
    with database.transaction() as cur:
        cur.execute(f"SELECT {sql_function}(%s, %s)", (user_id, amount))
        cur.execute("SELECT balance FROM wallets WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
    return float(row[0]) if row else 0.0


@api.route("/wallet/deposit", methods=["POST"])
@login_required
def deposit_wallet():
    data = request.get_json(silent=True) or {}
    amount, error = parse_amount(data.get("amount"))
    if error:
        return jsonify({"error": error}), 400
    if amount > config.MAX_DEPOSIT:
        return jsonify({
            "error": f"Maximum deposit amount is ${config.MAX_DEPOSIT:,}"
        }), 400

    try:
        balance = _wallet_operation("deposit_money", amount)
    except Exception as exc:
        return user_facing_db_error(exc)
    return jsonify({"balance": balance, "message": "Deposit successful"}), 200


@api.route("/wallet/withdraw", methods=["POST"])
@login_required
def withdraw_wallet():
    data = request.get_json(silent=True) or {}
    amount, error = parse_amount(data.get("amount"))
    if error:
        return jsonify({"error": error}), 400

    try:
        balance = _wallet_operation("withdraw_money", amount)
    except Exception as exc:
        return user_facing_db_error(exc)
    return jsonify({"balance": balance, "message": "Withdrawal successful"}), 200


@api.route("/wallet/history", methods=["GET"])
@login_required
def get_wallet_history():
    history = database.query_all(
        """
        SELECT action, old_value, new_value, timestamp, context
        FROM audit_logs
        WHERE user_id = %s
        ORDER BY timestamp DESC
        LIMIT 50
        """,
        (current_user_id(),),
    )
    return jsonify([
        {
            "action": row[0],
            "old_value": float(row[1]) if row[1] is not None else 0,
            "new_value": float(row[2]) if row[2] is not None else 0,
            "change": (float(row[2]) - float(row[1]))
                      if row[1] is not None and row[2] is not None else 0,
            "timestamp": row[3].isoformat(),
            "context": row[4],
        } for row in history
    ])


# --------------------------------------------------------------------------
# Portfolio and trading
# --------------------------------------------------------------------------

@api.route("/portfolio", methods=["GET"])
@login_required
def get_portfolio():
    # Columns are listed explicitly: SELECT * left the response bound to the
    # view's column order.
    rows = database.query_all(
        """
        SELECT asset_id, symbol, quantity, avg_price,
               current_price, current_value, unrealized_pl
        FROM portfolio_summary
        WHERE user_id = %s
        """,
        (current_user_id(),),
    )
    return jsonify([
        {
            "asset_id": r[0],
            "symbol": r[1],
            "quantity": float(r[2]),
            "avg_price": float(r[3]),
            "current_price": float(r[4]),
            "current_value": float(r[5]),
            "unrealized_pl": float(r[6]),
        } for r in rows
    ])


@api.route("/portfolio/stats", methods=["GET"])
@login_required
def get_portfolio_stats():
    user_id = current_user_id()
    stats = database.query_one(
        """
        SELECT
            COALESCE(SUM(avg_price * quantity), 0) AS total_invested,
            COALESCE(SUM(current_value), 0)        AS current_value,
            COALESCE(SUM(unrealized_pl), 0)        AS total_pl
        FROM portfolio_summary
        WHERE user_id = %s
        """,
        (user_id,),
    )
    balance = float(database.query_value(
        "SELECT balance FROM wallets WHERE user_id = %s", (user_id,), default=0
    ) or 0)

    return jsonify({
        "invested": float(stats[0]),
        "current_value": float(stats[1]),
        "total_pl": float(stats[2]),
        "wallet_balance": balance,
        "total_wealth": float(stats[1]) + balance,
    })


@api.route("/order", methods=["POST"])
@login_required
def place_order():
    data = request.get_json(silent=True) or {}
    asset_id = data.get("asset_id")
    order_type = (data.get("order_type") or "").lower()
    quantity = data.get("quantity")
    order_kind = (data.get("order_kind") or "market").lower()
    target_price = data.get("target_price")
    expires_at = data.get("expires_at")

    if not asset_id or not order_type or not quantity:
        return jsonify({"status": "error", "error": "Missing trade parameters"}), 400

    try:
        asset_id = int(asset_id)
        quantity = float(quantity)
    except (TypeError, ValueError):
        return jsonify({"status": "error", "error": "Invalid asset or quantity"}), 400

    # `not x > 0` rather than `x <= 0` so that NaN is also rejected.
    if not quantity > 0:
        return jsonify({"status": "error", "error": "Quantity must be greater than zero"}), 400
    if order_type not in ("buy", "sell"):
        return jsonify({"status": "error", "error": "Invalid order type"}), 400
    if order_kind not in ("market", "limit", "stop_loss"):
        return jsonify({"status": "error", "error": "Invalid order kind"}), 400

    if order_kind in ("limit", "stop_loss"):
        if target_price is None:
            return jsonify({
                "status": "error",
                "error": "target_price is required for limit/stop_loss orders",
            }), 400
        try:
            target_price = float(target_price)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "error": "Invalid target price"}), 400
        if not target_price > 0:
            return jsonify({"status": "error", "error": "Target price must be positive"}), 400
    else:
        target_price = None

    if expires_at:
        try:
            expires_at = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except ValueError:
            return jsonify({"status": "error", "error": "Invalid expires_at format"}), 400
    else:
        expires_at = None

    try:
        order_id = database.query_value(
            "SELECT place_order(%s, %s, %s, %s, %s, %s, %s)",
            (current_user_id(), asset_id, order_type, quantity,
             order_kind, target_price, expires_at),
        )
    except Exception as exc:
        response, status = user_facing_db_error(exc)
        return jsonify({"status": "error", "error": response.get_json()["error"]}), status

    return jsonify({
        "status": "success",
        "message": f"Order #{order_id} placed successfully",
        "order_id": order_id,
    }), 200


@api.route("/transactions", methods=["GET"])
@login_required
def get_transactions():
    try:
        limit = min(int(request.args.get("limit", 100)), 500)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        return jsonify({"error": "Invalid pagination parameters"}), 400

    rows = database.query_all(
        """
        SELECT t.trade_id, a.symbol, t.trade_type, t.quantity, t.price,
               (t.quantity * t.price) AS total, t.executed_at
        FROM trades t
        JOIN assets a ON t.asset_id = a.asset_id
        WHERE t.user_id = %s
        ORDER BY t.executed_at DESC
        LIMIT %s OFFSET %s
        """,
        (current_user_id(), limit, offset),
    )
    return jsonify([
        {
            "id": r[0],
            "symbol": r[1],
            "type": r[2],
            "quantity": float(r[3]),
            "price": float(r[4]),
            "total": float(r[5]),
            "time": r[6].isoformat(),
        } for r in rows
    ])


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------

@api.route("/prices", methods=["GET"])
def get_latest_prices():
    """Read-only. Order matching runs on the scheduler, not on page loads.

    This endpoint previously ran process_limit_orders() once per asset on
    every call, turning a single dashboard poll into 25 write transactions.
    """
    prices = database.query_all(
        """
        SELECT a.symbol, a.name, lp.price, lp.time, a.asset_id
        FROM latest_prices lp
        JOIN assets a ON lp.asset_id = a.asset_id
        ORDER BY a.symbol
        """
    )
    return jsonify([
        {
            "symbol": r[0],
            "name": r[1],
            "price": float(r[2]),
            "time": r[3].isoformat() if r[3] else None,
            "asset_id": r[4],
        } for r in prices
    ])


@api.route("/analytics/ohlc/<int:asset_id>", methods=["GET"])
def get_ohlc_history(asset_id):
    """Historical OHLC candles, served from the continuous aggregate."""
    days_map = {"1M": 30, "3M": 90, "6M": 180, "1Y": 365}
    days = days_map.get(request.args.get("period", "1M"), 30)

    # The window is parameterised as a multiplier of INTERVAL '1 day'. The
    # previous form, INTERVAL '%s day', put the placeholder inside a quoted
    # SQL literal, where psycopg2 cannot bind it.
    ohlc = database.query_all(
        """
        SELECT bucket, open, high, low, close
        FROM market_data_daily
        WHERE asset_id = %s AND bucket >= NOW() - (%s * INTERVAL '1 day')
        ORDER BY bucket ASC
        """,
        (asset_id, days),
    )

    if not ohlc:
        # The continuous aggregate may not have refreshed yet.
        ohlc = database.query_all(
            """
            SELECT time_bucket('1 day', time) AS bucket,
                   FIRST(price, time) AS open, MAX(price) AS high,
                   MIN(price) AS low, LAST(price, time) AS close
            FROM market_data
            WHERE asset_id = %s AND time >= NOW() - (%s * INTERVAL '1 day')
            GROUP BY bucket
            ORDER BY bucket ASC
            """,
            (asset_id, days),
        )

    return jsonify([
        {
            "time": r[0].isoformat(),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
        } for r in ohlc
    ])


@api.route("/analytics/price_history/<int:asset_id>", methods=["GET"])
def get_price_history(asset_id):
    """Recent ticks for the 1D chart: last 24h, else the most recent 50."""
    history = database.query_all(
        """
        SELECT time, price FROM market_data
        WHERE asset_id = %s AND time >= NOW() - INTERVAL '24 hours'
        ORDER BY time ASC
        """,
        (asset_id,),
    )

    if not history:
        history = database.query_all(
            """
            SELECT time, price FROM (
                SELECT time, price FROM market_data
                WHERE asset_id = %s
                ORDER BY time DESC LIMIT 50
            ) sub ORDER BY time ASC
            """,
            (asset_id,),
        )

    return jsonify([{"time": r[0].isoformat(), "price": float(r[1])} for r in history])


@api.route("/analytics/recent_trades/<int:asset_id>", methods=["GET"])
def get_recent_market_trades(asset_id):
    rows = database.query_all(
        """
        SELECT executed_at, price, quantity, trade_type
        FROM trades
        WHERE asset_id = %s
        ORDER BY executed_at DESC
        LIMIT 30
        """,
        (asset_id,),
    )
    return jsonify([
        {
            "time": r[0].isoformat(),
            "price": float(r[1]),
            "quantity": float(r[2]),
            "side": r[3],
        } for r in rows
    ])


# Only the sort direction differs between the two sides of the book, and it
# is chosen from these constants rather than from anything a caller sends.
_DEPTH_SQL = """
    SELECT ROUND(price::numeric, 2) AS price_level, SUM(quantity) AS total_qty
    FROM trades
    WHERE asset_id = %s
      AND trade_type = %s
      AND executed_at >= NOW() - INTERVAL '24 hours'
    GROUP BY 1
    ORDER BY price_level {direction}
    LIMIT 10
"""


@api.route("/analytics/orderbook/<int:asset_id>", methods=["GET"])
def get_market_orderbook(asset_id):
    """Price-level depth derived from the last 24h of executed trades."""
    bids = database.query_all(_DEPTH_SQL.format(direction="DESC"), (asset_id, "buy"))
    asks = database.query_all(_DEPTH_SQL.format(direction="ASC"), (asset_id, "sell"))

    return jsonify({
        "bids": [{"price": float(r[0]), "quantity": float(r[1])} for r in bids],
        "asks": [{"price": float(r[0]), "quantity": float(r[1])} for r in asks],
    })


@api.route("/analytics/yf_candles/<int:asset_id>", methods=["GET"])
def get_yfinance_candles(asset_id):
    supported_intervals = ["1m", "2m", "5m", "15m", "30m", "60m", "90m", "1d"]
    interval = (request.args.get("interval") or "1d").lower()
    period = (request.args.get("range") or "1mo").lower()

    if interval not in supported_intervals:
        return jsonify({"error": "Unsupported interval"}), 400

    asset = database.query_one(
        "SELECT symbol, type FROM assets WHERE asset_id = %s", (asset_id,)
    )
    if not asset:
        return jsonify({"error": "Asset not found"}), 404

    symbol, asset_type = asset
    ticker_symbol = f"{symbol}-USD" if asset_type == "crypto" else symbol

    intraday_intervals = {"1m", "2m", "5m", "15m", "30m", "60m", "90m"}
    warning = None
    effective_period = period

    if interval == "1m":
        effective_period = "7d"
        if period != "7d":
            warning = "1m data is only available for the last 7 days; range adjusted to 7d."
    elif interval in intraday_intervals:
        allowed = {"1d", "5d", "7d", "1mo", "2mo"}
        if period not in allowed:
            effective_period = "2mo"
            warning = "Intraday intervals are available for about 60 days; range adjusted to 2mo."

    try:
        hist = yf.Ticker(ticker_symbol).history(
            period=effective_period, interval=interval, auto_adjust=False
        )
    except Exception as exc:
        logger.warning("yfinance fetch failed for %s: %s", ticker_symbol, exc)
        return jsonify({"error": "Upstream market data provider is unavailable"}), 502

    meta = {
        "interval": interval,
        "requested_range": period,
        "effective_range": effective_period,
        "warning": warning,
        "supported_intervals": supported_intervals,
    }
    if hist.empty:
        return jsonify({"meta": meta, "candles": []})

    candles = []
    for idx, row in hist.iterrows():
        dt = idx.to_pydatetime()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        adj_close = row.get("Adj Close", row.get("Close", 0))
        candles.append({
            "time": dt.isoformat(),
            "open": float(row.get("Open", 0) or 0),
            "high": float(row.get("High", 0) or 0),
            "low": float(row.get("Low", 0) or 0),
            "close": float(row.get("Close", 0) or 0),
            "adj_close": float(adj_close or 0),
            "volume": float(row.get("Volume", 0) or 0),
        })

    return jsonify({"meta": meta, "candles": candles})


# --------------------------------------------------------------------------
# Analytics
# --------------------------------------------------------------------------

@api.route("/analytics/pnl_summary", methods=["GET"])
@login_required
def get_pnl_summary():
    user_id = current_user_id()
    realized = float(database.query_value(
        "SELECT COALESCE(SUM(realized_profit), 0) FROM realized_pnl WHERE user_id = %s",
        (user_id,), default=0,
    ))
    unrealized = float(database.query_value(
        "SELECT COALESCE(SUM(unrealized_pl), 0) FROM portfolio_summary WHERE user_id = %s",
        (user_id,), default=0,
    ))
    return jsonify({
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "total_pnl": realized + unrealized,
    })


@api.route("/analytics/leaderboard", methods=["GET"])
def get_leaderboard():
    """Top traders by total P&L (realized plus unrealized).

    Each component is aggregated in its own subquery before joining. Summing
    both across a single join would multiply the rows together, and the
    previous version silently omitted realized P&L altogether.
    """
    rows = database.query_all(
        """
        SELECT u.username,
               COALESCE(r.realized, 0) + COALESCE(p.unrealized, 0) AS total_pl,
               COALESCE(r.realized, 0)   AS realized_pl,
               COALESCE(p.unrealized, 0) AS unrealized_pl
        FROM users u
        LEFT JOIN (
            SELECT user_id, SUM(realized_profit) AS realized
            FROM realized_pnl GROUP BY user_id
        ) r ON r.user_id = u.user_id
        LEFT JOIN (
            SELECT user_id, SUM(unrealized_pl) AS unrealized
            FROM portfolio_summary GROUP BY user_id
        ) p ON p.user_id = u.user_id
        ORDER BY total_pl DESC
        LIMIT 10
        """
    )
    return jsonify([
        {
            "username": r[0],
            "total_pl": float(r[1]),
            "realized_pl": float(r[2]),
            "unrealized_pl": float(r[3]),
        } for r in rows
    ])


@api.route("/analytics/asset_stats", methods=["GET"])
def get_asset_stats():
    rows = database.query_all(
        """
        SELECT a.symbol,
               COUNT(t.trade_id) AS trade_count,
               COALESCE(SUM(t.quantity * t.price), 0) AS volume
        FROM trades t
        JOIN assets a ON t.asset_id = a.asset_id
        GROUP BY a.symbol
        ORDER BY trade_count DESC
        LIMIT 10
        """
    )
    return jsonify([
        {"symbol": r[0], "count": r[1], "volume": float(r[2])} for r in rows
    ])


@api.route("/analytics/indicators/<int:asset_id>", methods=["GET"])
def get_indicators(asset_id):
    """7-day SMA and 20-day rolling volatility over daily average price."""
    rows = database.query_all(
        """
        WITH daily_data AS (
            SELECT time_bucket('1 day', time) AS day_bucket,
                   asset_id,
                   AVG(price) AS daily_avg_price
            FROM market_data
            WHERE asset_id = %s
            GROUP BY day_bucket, asset_id
        ),
        indicators AS (
            SELECT day_bucket,
                   daily_avg_price,
                   AVG(daily_avg_price) OVER (
                       PARTITION BY asset_id ORDER BY day_bucket
                       ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
                   ) AS sma_7,
                   STDDEV(daily_avg_price) OVER (
                       PARTITION BY asset_id ORDER BY day_bucket
                       ROWS BETWEEN 20 PRECEDING AND CURRENT ROW
                   ) AS volatility
            FROM daily_data
        )
        SELECT day_bucket, daily_avg_price, sma_7, volatility
        FROM indicators
        ORDER BY day_bucket DESC
        LIMIT 7
        """,
        (asset_id,),
    )
    rows = list(rows)[::-1]  # chronological order for charting
    return jsonify([
        {
            "time": r[0].isoformat(),
            "price": float(r[1]),
            "sma_7": float(r[2]) if r[2] is not None else None,
            "volatility": float(r[3]) if r[3] is not None else None,
        } for r in rows
    ])


# --------------------------------------------------------------------------
# Background jobs
# --------------------------------------------------------------------------

def process_pending_limit_orders():
    try:
        assets = database.query_all("SELECT asset_id FROM assets")
        for (asset_id,) in assets:
            database.query_value("SELECT process_limit_orders(%s)", (asset_id,))
    except Exception as exc:
        logger.error("Limit order processing failed: %s", exc)


def expire_old_orders():
    try:
        count = database.query_value("SELECT expire_stale_orders()", default=0)
        if count:
            logger.info("Expired %s stale orders", count)
    except Exception as exc:
        logger.error("Order expiry failed: %s", exc)


def start_scheduler():
    if scheduler.running:
        return
    scheduler.add_job(process_pending_limit_orders, "interval", seconds=30,
                      id="process_limits", replace_existing=True)
    scheduler.add_job(expire_old_orders, "interval", minutes=5,
                      id="expire_orders", replace_existing=True)
    scheduler.start()
    logger.info("Background scheduler started")


# --------------------------------------------------------------------------
# Application factory
# --------------------------------------------------------------------------

def create_app(**overrides):
    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
    )
    app.config.update(
        SECRET_KEY=config.SECRET_KEY,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=config.SESSION_COOKIE_SECURE,
        SESSION_COOKIE_SAMESITE=config.SESSION_COOKIE_SAMESITE,
        PERMANENT_SESSION_LIFETIME=timedelta(
            minutes=config.PERMANENT_SESSION_LIFETIME_MINUTES
        ),
        WTF_CSRF_TIME_LIMIT=None,
    )
    app.config.update(overrides)

    csrf.init_app(app)

    # Same-origin only unless origins are explicitly configured. A wildcard
    # here alongside session cookies would let any site issue authenticated
    # requests on a logged-in user's behalf.
    if config.CORS_ORIGINS:
        CORS(app, origins=config.CORS_ORIGINS, supports_credentials=True)

    app.register_blueprint(pages)
    app.register_blueprint(api)

    @app.errorhandler(CSRFError)
    def handle_csrf_error(exc):
        logger.warning("CSRF validation failed: %s", exc.description)
        return jsonify({"error": "Session expired. Please refresh and try again."}), 400

    @app.errorhandler(404)
    def handle_not_found(_):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Not found"}), 404
        return redirect(url_for("pages.index"))

    return app


app = create_app()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG if config.DEBUG else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    # Guard against the reloader starting two scheduler instances.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not config.DEBUG:
        if config.AUTO_INIT_DB:
            database.init_db()
            logger.info("Schema applied")
        if config.ENABLE_SCHEDULER:
            start_scheduler()
    app.run(
        debug=config.DEBUG,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
    )
