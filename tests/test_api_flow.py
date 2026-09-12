"""End-to-end API tests: register, log in, fund, trade, inspect, log out.

These drive the HTTP surface rather than the stored procedures, so they cover
the request validation, session handling, and serialisation that the
database-level tests do not reach.
"""

from decimal import Decimal

import pytest

pytestmark = pytest.mark.db

CREDENTIALS = {
    "username": "alice",
    "email": "alice@example.com",
    "password": "correct-horse-9",
}


@pytest.fixture
def logged_in(client, clean_db):
    """A registered, authenticated client."""
    resp = client.post("/api/register", json=CREDENTIALS)
    assert resp.status_code == 201, resp.get_data(as_text=True)

    resp = client.post(
        "/api/login",
        json={
            "username": CREDENTIALS["username"],
            "password": CREDENTIALS["password"],
        },
    )
    assert resp.status_code == 200
    return client


class TestRegistrationAndLogin:
    def test_register_then_login(self, client, clean_db):
        assert client.post("/api/register", json=CREDENTIALS).status_code == 201

        resp = client.post(
            "/api/login",
            json={
                "username": CREDENTIALS["username"],
                "password": CREDENTIALS["password"],
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["username"] == "alice"

    def test_duplicate_registration_is_rejected_without_naming_the_field(self, client, clean_db):
        client.post("/api/register", json=CREDENTIALS)
        resp = client.post("/api/register", json=CREDENTIALS)

        assert resp.status_code == 409
        error = resp.get_json()["error"]
        assert "already registered" in error
        # Must not leak the constraint name or any psycopg2 detail.
        assert "users_username_key" not in error
        assert "DETAIL" not in error

    def test_wrong_password_is_rejected(self, client, clean_db):
        client.post("/api/register", json=CREDENTIALS)
        resp = client.post(
            "/api/login",
            json={
                "username": "alice",
                "password": "wrong",
            },
        )
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "Invalid username or password"

    def test_unknown_user_gets_the_same_message_as_a_wrong_password(self, client, clean_db):
        """Identical wording keeps the endpoint from confirming who exists."""
        resp = client.post(
            "/api/login",
            json={
                "username": "nobody",
                "password": "whatever",
            },
        )
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "Invalid username or password"

    def test_logout_ends_the_session(self, logged_in):
        assert logged_in.get("/api/wallet").status_code == 200
        assert logged_in.post("/api/logout").status_code == 200
        assert logged_in.get("/api/wallet").status_code == 401


class TestWalletEndpoints:
    def test_new_account_starts_funded(self, logged_in, money_field):
        balance = money_field(logged_in.get("/api/wallet").get_json()["balance"])
        assert balance == Decimal("100000.00")

    def test_deposit_increases_balance(self, logged_in, money_field):
        resp = logged_in.post("/api/wallet/deposit", json={"amount": 5000})
        assert resp.status_code == 200
        assert money_field(resp.get_json()["balance"]) == Decimal("105000.00")

    def test_withdraw_decreases_balance(self, logged_in, money_field):
        resp = logged_in.post("/api/wallet/withdraw", json={"amount": 1000})
        assert resp.status_code == 200
        assert money_field(resp.get_json()["balance"]) == Decimal("99000.00")

    def test_fractional_amounts_do_not_drift(self, logged_in, money_field):
        """Three deposits that float64 cannot represent must still total exactly.

        0.1 + 0.2 + 0.3 is 0.6000000000000001 in binary floating point.
        """
        for amount in ("0.10", "0.20", "0.30"):
            assert logged_in.post("/api/wallet/deposit", json={"amount": amount}).status_code == 200

        balance = money_field(logged_in.get("/api/wallet").get_json()["balance"])
        assert balance == Decimal("100000.60")

    def test_withdrawing_more_than_held_is_refused(self, logged_in, money_field):
        resp = logged_in.post("/api/wallet/withdraw", json={"amount": 999999})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "Insufficient balance"
        # The balance must be untouched.
        balance = money_field(logged_in.get("/api/wallet").get_json()["balance"])
        assert balance == Decimal("100000.00")

    def test_deposit_cap_is_enforced(self, logged_in):
        resp = logged_in.post("/api/wallet/deposit", json={"amount": 500001})
        assert resp.status_code == 400
        assert "Maximum deposit" in resp.get_json()["error"]

    @pytest.mark.parametrize("amount", [0, -100, "abc", None])
    def test_invalid_amounts_are_refused(self, logged_in, amount):
        resp = logged_in.post("/api/wallet/deposit", json={"amount": amount})
        assert resp.status_code == 400

    def test_history_records_the_movements(self, logged_in):
        logged_in.post("/api/wallet/deposit", json={"amount": 5000})
        logged_in.post("/api/wallet/withdraw", json={"amount": 1000})

        history = logged_in.get("/api/wallet/history").get_json()
        assert len(history) == 2
        assert all("timestamp" in row for row in history)


class TestTradingEndpoints:
    def test_market_buy_then_portfolio_reflects_it(self, logged_in, asset_id, money_field):
        resp = logged_in.post(
            "/api/order",
            json={
                "asset_id": asset_id,
                "order_type": "buy",
                "quantity": 10,
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "success"

        holdings = logged_in.get("/api/portfolio").get_json()
        assert len(holdings) == 1
        assert money_field(holdings[0]["quantity"]) == Decimal("10")
        assert money_field(holdings[0]["avg_price"]) == Decimal("100")

    def test_stats_combine_cash_and_holdings(self, logged_in, asset_id, money_field):
        logged_in.post(
            "/api/order",
            json={
                "asset_id": asset_id,
                "order_type": "buy",
                "quantity": 10,
            },
        )
        stats = logged_in.get("/api/portfolio/stats").get_json()

        assert money_field(stats["invested"]) == Decimal("1000")
        assert money_field(stats["wallet_balance"]) == Decimal("99000.00")
        # Exact, not approximate: cash plus holdings must reconstruct the
        # starting balance to the cent.
        assert money_field(stats["total_wealth"]) == Decimal("100000.00")

    def test_transactions_list_the_trade(self, logged_in, asset_id, money_field):
        logged_in.post(
            "/api/order",
            json={
                "asset_id": asset_id,
                "order_type": "buy",
                "quantity": 10,
            },
        )
        trades = logged_in.get("/api/transactions").get_json()

        assert len(trades) == 1
        assert trades[0]["type"] == "buy"
        assert money_field(trades[0]["total"]) == Decimal("1000")

    def test_transactions_are_paginated(self, logged_in, asset_id):
        for _ in range(3):
            logged_in.post(
                "/api/order",
                json={
                    "asset_id": asset_id,
                    "order_type": "buy",
                    "quantity": 1,
                },
            )

        assert len(logged_in.get("/api/transactions?limit=2").get_json()) == 2
        assert len(logged_in.get("/api/transactions?limit=2&offset=2").get_json()) == 1

    def test_pnl_summary_after_a_round_trip(self, logged_in, asset_id, set_price, money_field):
        logged_in.post(
            "/api/order",
            json={
                "asset_id": asset_id,
                "order_type": "buy",
                "quantity": 10,
            },
        )
        set_price(asset_id, 130)
        logged_in.post(
            "/api/order",
            json={
                "asset_id": asset_id,
                "order_type": "sell",
                "quantity": 10,
            },
        )

        summary = logged_in.get("/api/analytics/pnl_summary").get_json()
        assert money_field(summary["realized_pnl"]) == Decimal("300")
        assert money_field(summary["unrealized_pnl"]) == Decimal("0")
        assert money_field(summary["total_pnl"]) == Decimal("300")

    def test_buying_beyond_balance_returns_a_clean_message(self, logged_in, asset_id):
        resp = logged_in.post(
            "/api/order",
            json={
                "asset_id": asset_id,
                "order_type": "buy",
                "quantity": 100000,
            },
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "Insufficient balance"

    def test_selling_without_holdings_returns_a_clean_message(self, logged_in, asset_id):
        resp = logged_in.post(
            "/api/order",
            json={
                "asset_id": asset_id,
                "order_type": "sell",
                "quantity": 5,
            },
        )
        assert resp.status_code == 400
        assert "Insufficient" in resp.get_json()["error"]

    @pytest.mark.parametrize(
        "payload,expected",
        [
            ({"order_type": "buy", "quantity": 1}, "Missing"),
            ({"asset_id": 1, "quantity": 1}, "Missing"),
            ({"asset_id": 1, "order_type": "sideways", "quantity": 1}, "Invalid order type"),
            (
                {"asset_id": 1, "order_type": "buy", "quantity": 1, "order_kind": "iceberg"},
                "Invalid order kind",
            ),
            (
                {"asset_id": 1, "order_type": "buy", "quantity": 1, "order_kind": "limit"},
                "target_price is required",
            ),
            (
                {"asset_id": 1, "order_type": "buy", "quantity": 1, "expires_at": "not-a-date"},
                "Invalid expires_at",
            ),
        ],
    )
    def test_malformed_orders_are_refused(self, logged_in, payload, expected):
        resp = logged_in.post("/api/order", json=payload)
        assert resp.status_code == 400
        assert expected.lower() in resp.get_json()["error"].lower()

    def test_limit_order_is_accepted_and_left_open(self, logged_in, asset_id):
        resp = logged_in.post(
            "/api/order",
            json={
                "asset_id": asset_id,
                "order_type": "buy",
                "quantity": 5,
                "order_kind": "limit",
                "target_price": 90,
            },
        )
        assert resp.status_code == 200
        # No trade yet: the market is above the target.
        assert logged_in.get("/api/transactions").get_json() == []


class TestHealthEndpoint:
    def test_healthz_reports_database_reachable(self, client, clean_db):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.get_json() == {"status": "ok", "database": "ok"}
