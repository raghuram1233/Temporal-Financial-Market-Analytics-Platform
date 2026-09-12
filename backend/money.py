"""Exact monetary and quantity handling.

The database stores money as ``NUMERIC`` and psycopg2 hands those columns
back as :class:`decimal.Decimal`. Every value was previously passed through
``float()`` on its way into a JSON response, which reintroduced binary
floating-point error at the one boundary the schema had been careful to
avoid: ``0.1 + 0.2`` is not ``0.3`` in IEEE 754, and a balance assembled
from enough such additions drifts.

Two rules follow, and this module exists to enforce them:

1. Money never becomes a ``float`` anywhere in the process. Input is parsed
   straight from its textual form into ``Decimal``; output is serialised
   straight from ``Decimal``.
2. Monetary values cross the wire as JSON **strings**. A JSON number is
   parsed by ``JSON.parse`` into a float64 regardless of how many digits the
   server wrote, so a number field silently hands the precision back to the
   client. Quoting the value keeps the exact digits intact and makes the
   client's own rounding an explicit, visible decision. This is the same
   choice Stripe and Coinbase make in their public APIs.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from flask.json.provider import DefaultJSONProvider

# Column scales from schema.sql. Amounts are quantized to these before they
# reach Postgres so that rounding happens once, here, under a rule we chose,
# rather than implicitly inside the database driver.
CASH_PLACES = Decimal("0.01")  # wallets.balance          NUMERIC(15,2)
QUANTITY_PLACES = Decimal("0.00001")  # orders.quantity       NUMERIC(15,5)
PRICE_PLACES = Decimal("0.00001")  # orders.price          NUMERIC(15,5)

# Guards against absurd input reaching a NUMERIC(15,x) column, where it would
# raise a numeric field overflow that surfaces as an opaque 500.
MAX_MAGNITUDE = Decimal("9999999999")


class DecimalJSONProvider(DefaultJSONProvider):
    """Serialise ``Decimal`` as a JSON string instead of failing on it.

    ``DefaultJSONProvider`` raises ``TypeError`` for ``Decimal``, which is
    what forced the ``float()`` casts in the first place. Handling it here
    means a route can return the value the database gave it and the exact
    digits survive to the client.
    """

    @staticmethod
    def default(o):
        if isinstance(o, Decimal):
            return str(o)
        return DefaultJSONProvider.default(o)


def to_decimal(raw, places=CASH_PLACES, noun="Amount"):
    """Parse untrusted JSON input into an exact ``Decimal``.

    Returns ``(value, error)``; exactly one of the two is ever set. ``noun``
    names the field in the error text so callers get "Quantity must be
    greater than zero" rather than a generic message.

    ``raw`` is converted via ``str`` rather than accepted as a float, because
    ``Decimal(0.1)`` preserves the float's error while ``Decimal("0.1")`` is
    exact. A client that sends a JSON number has already lost precision, but
    this at least stops us from adding more.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, f"{noun} is required"
    if isinstance(raw, bool):
        # bool is an int subclass; accepting it would turn True into 1.
        return None, f"Invalid {noun.lower()}"

    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None, f"Invalid {noun.lower()}"

    # Decimal happily constructs NaN and Infinity from their textual forms,
    # so the check float() needed is still required here.
    if not value.is_finite():
        return None, f"Invalid {noun.lower()}"
    if value <= 0:
        return None, f"{noun} must be greater than zero"
    if value > MAX_MAGNITUDE:
        return None, f"{noun} is too large"

    return value.quantize(places, rounding=ROUND_HALF_UP), None


def to_quantity(raw):
    """Parse an order quantity. Same rules as `to_decimal`, different scale."""
    return to_decimal(raw, places=QUANTITY_PLACES, noun="Quantity")


def to_price(raw):
    """Parse a limit or stop-loss target price."""
    return to_decimal(raw, places=PRICE_PLACES, noun="Target price")


def money(value, places=CASH_PLACES):
    """Normalise a value read from the database for output.

    ``None`` becomes an exact zero so that responses always carry a numeric
    string rather than a mix of ``null`` and digits.
    """
    if value is None:
        return Decimal("0").quantize(places)
    if not isinstance(value, Decimal):
        # yfinance and pandas hand back float64. Going through str() keeps the
        # shortest representation that round-trips, instead of the full binary
        # expansion Decimal(float) would produce.
        value = Decimal(str(value))
    return value
