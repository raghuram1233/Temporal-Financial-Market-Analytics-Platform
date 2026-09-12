"""Order execution, wallet movements, limit/stop-loss matching, and expiry.

These exercise the place_order / execute_trade / process_limit_orders stored
procedures directly, plus regression coverage for endpoints whose queries
previously could not execute.
"""

import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg2
import pytest

pytestmark = pytest.mark.db

STARTING_BALANCE = 100000.00


def balance_of(db, user_id):
    return float(db.query_value("SELECT balance FROM wallets WHERE user_id = %s", (user_id,)))


def place(db, user_id, asset_id, side, qty, kind="market", target=None, expires=None):
    return db.query_value(
        "SELECT place_order(%s, %s, %s, %s, %s, %s, %s)",
        (user_id, asset_id, side, qty, kind, target, expires),
    )


class TestWalletTrigger:
    def test_new_user_gets_a_wallet_automatically(self, clean_db, make_user):
        """trg_create_wallet fires on user insert."""
        user = make_user()
        assert balance_of(clean_db, user) == STARTING_BALANCE

    def test_buy_debits_the_wallet(self, clean_db, make_user, asset_id):
        user = make_user()
        place(clean_db, user, asset_id, "buy", 10)  # 10 @ 100
        assert balance_of(clean_db, user) == STARTING_BALANCE - 1000

    def test_sell_credits_the_wallet(self, clean_db, make_user, asset_id, set_price):
        user = make_user()
        place(clean_db, user, asset_id, "buy", 10)  # -1000
        set_price(asset_id, 150)
        place(clean_db, user, asset_id, "sell", 10)  # +1500
        assert balance_of(clean_db, user) == STARTING_BALANCE + 500

    def test_balance_changes_are_audited(self, clean_db, make_user, asset_id):
        user = make_user()
        place(clean_db, user, asset_id, "buy", 10)

        rows = clean_db.query_all(
            "SELECT action, old_value, new_value FROM audit_logs "
            "WHERE user_id = %s AND action = 'WALLET_BALANCE_UPDATE'",
            (user,),
        )
        assert len(rows) == 1
        assert float(rows[0][1]) == STARTING_BALANCE
        assert float(rows[0][2]) == STARTING_BALANCE - 1000


class TestOrderValidation:
    def test_buy_beyond_balance_is_rejected(self, clean_db, make_user, asset_id):
        user = make_user(balance=500)
        with pytest.raises(psycopg2.Error, match="Insufficient balance"):
            place(clean_db, user, asset_id, "buy", 10)  # needs 1000

    def test_rejected_buy_leaves_the_balance_untouched(self, clean_db, make_user, asset_id):
        user = make_user(balance=500)
        with pytest.raises(psycopg2.Error):
            place(clean_db, user, asset_id, "buy", 10)
        assert balance_of(clean_db, user) == 500

    def test_rejected_order_is_rolled_back_entirely(self, clean_db, make_user, asset_id):
        """The failed order row must not survive: it never executed."""
        user = make_user(balance=500)
        with pytest.raises(psycopg2.Error):
            place(clean_db, user, asset_id, "buy", 10)
        assert clean_db.query_value("SELECT count(*) FROM orders WHERE user_id = %s", (user,)) == 0

    def test_selling_without_holdings_is_rejected(self, clean_db, make_user, asset_id):
        user = make_user()
        with pytest.raises(psycopg2.Error, match="Insufficient"):
            place(clean_db, user, asset_id, "sell", 5)

    def test_selling_more_than_held_is_rejected(self, clean_db, make_user, asset_id):
        user = make_user()
        place(clean_db, user, asset_id, "buy", 5)
        with pytest.raises(psycopg2.Error, match="Insufficient"):
            place(clean_db, user, asset_id, "sell", 10)

    def test_zero_quantity_is_rejected(self, clean_db, make_user, asset_id):
        user = make_user()
        with pytest.raises(psycopg2.Error, match="greater than zero"):
            place(clean_db, user, asset_id, "buy", 0)

    def test_unknown_order_kind_is_rejected(self, clean_db, make_user, asset_id):
        user = make_user()
        with pytest.raises(psycopg2.Error, match="Invalid order kind"):
            place(clean_db, user, asset_id, "buy", 1, kind="iceberg")

    def test_limit_order_without_target_is_rejected(self, clean_db, make_user, asset_id):
        user = make_user()
        with pytest.raises(psycopg2.Error, match="Target price required"):
            place(clean_db, user, asset_id, "buy", 1, kind="limit", target=None)


class TestConcurrency:
    def test_two_simultaneous_buys_cannot_overdraw(self, clean_db, make_user, asset_id):
        """Both threads see enough money before either commits.

        execute_trade takes SELECT ... FOR UPDATE on the wallet row, so the
        second transaction blocks and then re-reads the debited balance.
        Exactly one buy must succeed.
        """
        user = make_user(balance=1500)  # enough for one 1000 buy, not two
        results = []
        barrier = threading.Barrier(2)

        def attempt():
            barrier.wait()
            try:
                place(clean_db, user, asset_id, "buy", 10)
                results.append("ok")
            except Exception:
                results.append("rejected")

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert sorted(results) == ["ok", "rejected"]
        assert balance_of(clean_db, user) == 500
        assert clean_db.query_value("SELECT count(*) FROM trades WHERE user_id = %s", (user,)) == 1


class TestLimitOrders:
    def test_limit_buy_stays_open_above_target(self, clean_db, make_user, asset_id):
        user = make_user()
        place(clean_db, user, asset_id, "buy", 5, kind="limit", target=90)

        filled = clean_db.query_value("SELECT process_limit_orders(%s)", (asset_id,))
        assert filled == 0
        assert (
            clean_db.query_value("SELECT status FROM orders WHERE user_id = %s", (user,)) == "open"
        )

    def test_limit_buy_fills_when_price_drops(self, clean_db, make_user, asset_id, set_price):
        user = make_user()
        place(clean_db, user, asset_id, "buy", 5, kind="limit", target=90)

        set_price(asset_id, 85)
        filled = clean_db.query_value("SELECT process_limit_orders(%s)", (asset_id,))

        assert filled == 1
        assert (
            clean_db.query_value("SELECT status FROM orders WHERE user_id = %s", (user,))
            == "filled"
        )
        # Fills at the market price, not at the target.
        assert (
            float(clean_db.query_value("SELECT price FROM trades WHERE user_id = %s", (user,)))
            == 85.0
        )

    def test_limit_sell_fills_when_price_rises(self, clean_db, make_user, asset_id, set_price):
        user = make_user()
        place(clean_db, user, asset_id, "buy", 10)
        place(clean_db, user, asset_id, "sell", 5, kind="limit", target=110)

        assert clean_db.query_value("SELECT process_limit_orders(%s)", (asset_id,)) == 0

        set_price(asset_id, 115)
        assert clean_db.query_value("SELECT process_limit_orders(%s)", (asset_id,)) == 1

    def test_stop_loss_sell_fires_when_price_falls(self, clean_db, make_user, asset_id, set_price):
        """A stop-loss triggers in the opposite direction to a limit order."""
        user = make_user()
        place(clean_db, user, asset_id, "buy", 10)
        place(clean_db, user, asset_id, "sell", 5, kind="stop_loss", target=90)

        assert clean_db.query_value("SELECT process_limit_orders(%s)", (asset_id,)) == 0

        set_price(asset_id, 85)
        assert clean_db.query_value("SELECT process_limit_orders(%s)", (asset_id,)) == 1


class TestOrderExpiry:
    def test_past_expiry_cancels_the_order(self, clean_db, make_user, asset_id):
        user = make_user()
        past = datetime.now(UTC) - timedelta(minutes=1)
        place(clean_db, user, asset_id, "buy", 5, kind="limit", target=90, expires=past)

        cancelled = clean_db.query_value("SELECT expire_stale_orders()")
        assert cancelled == 1
        assert (
            clean_db.query_value("SELECT status FROM orders WHERE user_id = %s", (user,))
            == "cancelled"
        )

    def test_future_expiry_is_left_alone(self, clean_db, make_user, asset_id):
        user = make_user()
        future = datetime.now(UTC) + timedelta(days=1)
        place(clean_db, user, asset_id, "buy", 5, kind="limit", target=90, expires=future)

        assert clean_db.query_value("SELECT expire_stale_orders()") == 0
        assert (
            clean_db.query_value("SELECT status FROM orders WHERE user_id = %s", (user,)) == "open"
        )

    def test_expired_orders_do_not_fill(self, clean_db, make_user, asset_id, set_price):
        user = make_user()
        past = datetime.now(UTC) - timedelta(minutes=1)
        place(clean_db, user, asset_id, "buy", 5, kind="limit", target=90, expires=past)

        set_price(asset_id, 85)  # would otherwise trigger the order
        assert clean_db.query_value("SELECT process_limit_orders(%s)", (asset_id,)) == 0


class TestAnalyticsEndpoints:
    """Regression coverage for queries that previously could not execute."""

    def test_ohlc_endpoint_returns_data(self, clean_db, client, asset_id):
        """The INTERVAL bound was interpolated into a quoted SQL literal.

        `INTERVAL '%s day'` is not a bindable placeholder, so this endpoint
        raised instead of returning candles.
        """
        for period in ("1M", "3M", "6M", "1Y"):
            resp = client.get(f"/api/analytics/ohlc/{asset_id}?period={period}")
            assert resp.status_code == 200, f"{period}: {resp.get_data(as_text=True)}"
            assert isinstance(resp.get_json(), list)

    def test_price_history_endpoint(self, clean_db, client, asset_id):
        resp = client.get(f"/api/analytics/price_history/{asset_id}")
        assert resp.status_code == 200
        assert len(resp.get_json()) >= 1

    def test_prices_endpoint_performs_no_writes(self, clean_db, client, asset_id):
        """It used to run process_limit_orders() once per asset per call."""
        before = clean_db.query_value("SELECT count(*) FROM trades")
        assert client.get("/api/prices").status_code == 200
        assert clean_db.query_value("SELECT count(*) FROM trades") == before

    def test_leaderboard_includes_realized_pnl(
        self, clean_db, client, make_user, asset_id, set_price, money_field
    ):
        """The old query summed only unrealized P&L and dropped realized."""
        user = make_user()
        place(clean_db, user, asset_id, "buy", 10)
        set_price(asset_id, 130)
        place(clean_db, user, asset_id, "sell", 10)  # realizes +300, no holding left

        resp = client.get("/api/analytics/leaderboard")
        assert resp.status_code == 200
        row = next(r for r in resp.get_json() if money_field(r["realized_pl"]) != Decimal("0"))
        assert money_field(row["realized_pl"]) == Decimal("300")
        assert money_field(row["total_pl"]) == Decimal("300")

    def test_orderbook_endpoint(self, clean_db, client, asset_id, make_user, money_field):
        user = make_user()
        place(clean_db, user, asset_id, "buy", 10)

        resp = client.get(f"/api/analytics/orderbook/{asset_id}")
        assert resp.status_code == 200
        body = resp.get_json()
        assert "bids" in body and "asks" in body
        assert money_field(body["bids"][0]["price"]) == Decimal("100.00")
