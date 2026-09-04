"""Cost basis, realized P&L, and the bi-temporal portfolio model.

This logic lives in the fn_update_portfolio_after_trade trigger, so these
tests drive it through real trades against a real TimescaleDB instance.
"""

import pytest

pytestmark = pytest.mark.db


def current_holding(db, user_id, asset_id):
    """The one portfolio row that is currently valid for this user/asset."""
    return db.query_one(
        """
        SELECT quantity, avg_price FROM portfolio
        WHERE user_id = %s AND asset_id = %s
          AND valid_to IS NULL AND transaction_to IS NULL
        """,
        (user_id, asset_id),
    )


def buy(db, user_id, asset_id, qty):
    return db.query_value(
        "SELECT place_order(%s, %s, 'buy', %s, 'market', NULL, NULL)",
        (user_id, asset_id, qty),
    )


def sell(db, user_id, asset_id, qty):
    return db.query_value(
        "SELECT place_order(%s, %s, 'sell', %s, 'market', NULL, NULL)",
        (user_id, asset_id, qty),
    )


class TestCostBasis:
    def test_first_buy_sets_quantity_and_average(self, clean_db, make_user, asset_id):
        user = make_user()
        buy(clean_db, user, asset_id, 10)

        qty, avg = current_holding(clean_db, user, asset_id)
        assert float(qty) == 10
        assert float(avg) == 100.0

    def test_second_buy_produces_weighted_average(
        self, clean_db, make_user, asset_id, set_price
    ):
        """10 @ 100 then 10 @ 120 must average to 110, not to 120."""
        user = make_user()
        buy(clean_db, user, asset_id, 10)

        set_price(asset_id, 120)
        buy(clean_db, user, asset_id, 10)

        qty, avg = current_holding(clean_db, user, asset_id)
        assert float(qty) == 20
        assert float(avg) == pytest.approx(110.0)

    def test_selling_does_not_change_the_average(
        self, clean_db, make_user, asset_id, set_price
    ):
        """A sale realizes profit; it must not re-price the remaining units."""
        user = make_user()
        buy(clean_db, user, asset_id, 10)
        set_price(asset_id, 120)
        buy(clean_db, user, asset_id, 10)          # avg now 110

        set_price(asset_id, 130)
        sell(clean_db, user, asset_id, 5)

        qty, avg = current_holding(clean_db, user, asset_id)
        assert float(qty) == 15
        assert float(avg) == pytest.approx(110.0)

    def test_selling_everything_closes_the_position(
        self, clean_db, make_user, asset_id
    ):
        user = make_user()
        buy(clean_db, user, asset_id, 10)
        sell(clean_db, user, asset_id, 10)

        assert current_holding(clean_db, user, asset_id) is None


class TestRealizedPnl:
    def test_profit_is_booked_against_average_cost(
        self, clean_db, make_user, asset_id, set_price
    ):
        """Buy 10@100 + 10@120 (avg 110), sell 5@130 => (130-110)*5 = 100."""
        user = make_user()
        buy(clean_db, user, asset_id, 10)
        set_price(asset_id, 120)
        buy(clean_db, user, asset_id, 10)

        set_price(asset_id, 130)
        sell(clean_db, user, asset_id, 5)

        realized = clean_db.query_value(
            "SELECT SUM(realized_profit) FROM realized_pnl WHERE user_id = %s",
            (user,),
        )
        assert float(realized) == pytest.approx(100.0)

    def test_loss_is_recorded_as_negative(
        self, clean_db, make_user, asset_id, set_price
    ):
        user = make_user()
        buy(clean_db, user, asset_id, 10)

        set_price(asset_id, 80)
        sell(clean_db, user, asset_id, 10)

        realized = clean_db.query_value(
            "SELECT SUM(realized_profit) FROM realized_pnl WHERE user_id = %s",
            (user,),
        )
        assert float(realized) == pytest.approx(-200.0)

    def test_no_pnl_row_is_written_for_a_buy(self, clean_db, make_user, asset_id):
        user = make_user()
        buy(clean_db, user, asset_id, 10)

        count = clean_db.query_value(
            "SELECT count(*) FROM realized_pnl WHERE user_id = %s", (user,)
        )
        assert count == 0


class TestBiTemporalModel:
    """The portfolio table keeps history; only one row is ever current."""

    def test_exactly_one_current_row_per_holding(
        self, clean_db, make_user, asset_id, set_price
    ):
        user = make_user()
        buy(clean_db, user, asset_id, 10)
        set_price(asset_id, 120)
        buy(clean_db, user, asset_id, 5)
        set_price(asset_id, 130)
        buy(clean_db, user, asset_id, 5)

        current = clean_db.query_value(
            """
            SELECT count(*) FROM portfolio
            WHERE user_id = %s AND asset_id = %s
              AND valid_to IS NULL AND transaction_to IS NULL
            """,
            (user, asset_id),
        )
        assert current == 1

    def test_superseded_versions_are_retained(
        self, clean_db, make_user, asset_id, set_price
    ):
        """Three buys leave two closed versions plus one open one."""
        user = make_user()
        buy(clean_db, user, asset_id, 10)
        set_price(asset_id, 120)
        buy(clean_db, user, asset_id, 5)
        set_price(asset_id, 130)
        buy(clean_db, user, asset_id, 5)

        total = clean_db.query_value(
            "SELECT count(*) FROM portfolio WHERE user_id = %s AND asset_id = %s",
            (user, asset_id),
        )
        closed = clean_db.query_value(
            """
            SELECT count(*) FROM portfolio
            WHERE user_id = %s AND asset_id = %s AND valid_to IS NOT NULL
            """,
            (user, asset_id),
        )
        assert total == 3
        assert closed == 2

    def test_history_reconstructs_the_earliest_position(
        self, clean_db, make_user, asset_id, set_price
    ):
        """The first version still records the original 10 units at 100."""
        user = make_user()
        buy(clean_db, user, asset_id, 10)
        set_price(asset_id, 120)
        buy(clean_db, user, asset_id, 10)

        first = clean_db.query_one(
            """
            SELECT quantity, avg_price FROM portfolio
            WHERE user_id = %s AND asset_id = %s
            ORDER BY valid_from ASC, portfolio_version_id ASC
            LIMIT 1
            """,
            (user, asset_id),
        )
        assert float(first[0]) == 10
        assert float(first[1]) == 100.0


class TestPortfolioSummaryView:
    def test_unrealized_pl_tracks_the_current_price(
        self, clean_db, make_user, asset_id, set_price
    ):
        user = make_user()
        buy(clean_db, user, asset_id, 10)      # cost basis 100

        set_price(asset_id, 150)               # +50 per unit on 10 units

        row = clean_db.query_one(
            """
            SELECT quantity, avg_price, current_price, current_value, unrealized_pl
            FROM portfolio_summary WHERE user_id = %s
            """,
            (user,),
        )
        qty, avg, price, value, unrealized = (float(v) for v in row)
        assert qty == 10
        assert avg == 100.0
        assert price == 150.0
        assert value == pytest.approx(1500.0)
        assert unrealized == pytest.approx(500.0)

    def test_closed_positions_leave_the_summary(self, clean_db, make_user, asset_id):
        user = make_user()
        buy(clean_db, user, asset_id, 10)
        sell(clean_db, user, asset_id, 10)

        rows = clean_db.query_all(
            "SELECT * FROM portfolio_summary WHERE user_id = %s", (user,)
        )
        assert rows == []
