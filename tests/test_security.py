"""Security tests: authentication, CSRF, and input validation.

None of these need a database - every check short-circuits before the view
body runs, which is exactly the property being asserted.
"""

from decimal import Decimal

import pytest

from backend import config

PROTECTED_API_GETS = [
    "/api/wallet",
    "/api/wallet/history",
    "/api/portfolio",
    "/api/portfolio/stats",
    "/api/transactions",
    "/api/analytics/pnl_summary",
]

PROTECTED_API_POSTS = [
    ("/api/wallet/deposit", {"amount": 100}),
    ("/api/wallet/withdraw", {"amount": 100}),
    ("/api/order", {"asset_id": 1, "order_type": "buy", "quantity": 1}),
]

PROTECTED_PAGES = [
    "/dashboard",
    "/markets",
    "/portfolio",
    "/trade",
    "/analytics",
    "/transactions",
]


class TestAuthenticationRequired:
    @pytest.mark.parametrize("path", PROTECTED_API_GETS)
    def test_get_without_session_is_401(self, client, path):
        resp = client.get(path)
        assert resp.status_code == 401
        assert resp.get_json() == {"error": "Unauthorized"}

    @pytest.mark.parametrize("path,body", PROTECTED_API_POSTS)
    def test_post_without_session_is_401(self, client, path, body):
        resp = client.post(path, json=body)
        assert resp.status_code == 401

    @pytest.mark.parametrize("path", PROTECTED_PAGES)
    def test_page_without_session_redirects_to_login(self, client, path):
        resp = client.get(path)
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/")

    def test_session_cookie_is_hardened(self, app):
        assert app.config["SESSION_COOKIE_HTTPONLY"] is True
        assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"

    def test_unknown_api_path_returns_json_not_html(self, client):
        resp = client.get("/api/nope")
        assert resp.status_code == 404
        assert resp.is_json


class TestCsrfProtection:
    """State-changing requests must carry a CSRF token.

    Without this, any page on the internet could POST to /api/wallet/withdraw
    on behalf of a logged-in user, because the session cookie rides along.
    """

    @pytest.mark.parametrize(
        "path,body",
        [
            ("/api/login", {"username": "a", "password": "b"}),
            ("/api/register", {"username": "a", "email": "a@b.c", "password": "abcd1234"}),
            ("/api/logout", {}),
            ("/api/wallet/deposit", {"amount": 100}),
            ("/api/wallet/withdraw", {"amount": 100}),
            ("/api/order", {"asset_id": 1, "order_type": "buy", "quantity": 1}),
        ],
    )
    def test_post_without_token_is_rejected(self, csrf_app, path, body):
        resp = csrf_app.test_client().post(path, json=body)
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_login_page_exposes_a_token(self, csrf_app):
        html = csrf_app.test_client().get("/").get_data(as_text=True)
        assert 'name="csrf-token"' in html
        assert "js/csrf.js" in html

    def test_read_only_requests_need_no_token(self, csrf_app):
        # A 401 here proves CSRF did not block the request; auth did.
        resp = csrf_app.test_client().get("/api/wallet")
        assert resp.status_code == 401


class TestCorsIsNotWideOpen:
    def test_no_wildcard_origin_by_default(self, client):
        """A wildcard alongside session cookies would defeat CSRF entirely.

        Uses a route that returns 401 before touching the database, so the
        assertion is about headers rather than about connectivity.
        """
        resp = client.get("/api/wallet", headers={"Origin": "https://evil.example"})
        assert resp.status_code == 401
        assert resp.headers.get("Access-Control-Allow-Origin") != "*"

    def test_default_config_lists_no_origins(self):
        assert config.CORS_ORIGINS == []


class TestConfigRefusesWeakSecrets:
    """The app must not boot with a guessable session key."""

    def test_placeholder_keys_are_in_the_rejection_list(self):
        assert "change-me-in-production" in config._REJECTED_SECRETS

    def test_loaded_secret_key_is_strong(self):
        assert len(config.SECRET_KEY) >= 32
        assert config.SECRET_KEY.lower() not in config._REJECTED_SECRETS


class TestRegistrationValidation:
    """Input rules that run before any database call."""

    @pytest.mark.parametrize(
        "payload,expected",
        [
            ({}, "required"),
            ({"username": "a", "email": "a@b.c"}, "required"),
            ({"username": "a", "email": "not-an-email", "password": "abcd1234"}, "valid email"),
            ({"username": "a", "email": "a@b.c", "password": "short"}, "at least"),
            ({"username": "x" * 65, "email": "a@b.c", "password": "abcd1234"}, "64 characters"),
        ],
    )
    def test_bad_input_is_rejected(self, client, payload, expected):
        resp = client.post("/api/register", json=payload)
        assert resp.status_code == 400
        assert expected.lower() in resp.get_json()["error"].lower()

    def test_password_over_bcrypt_limit_is_rejected(self, client):
        """bcrypt silently truncates past 72 bytes, weakening long passphrases."""
        resp = client.post(
            "/api/register",
            json={
                "username": "bob",
                "email": "bob@example.com",
                "password": "a" * (config.MAX_PASSWORD_BYTES + 1),
            },
        )
        assert resp.status_code == 400
        assert "72" in resp.get_json()["error"]


class TestAmountValidation:
    """money.to_decimal guards every monetary endpoint."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, "required"),
            ("", "required"),
            ("abc", "Invalid"),
            (0, "greater than zero"),
            (-50, "greater than zero"),
            (float("nan"), "Invalid"),
            (float("inf"), "Invalid"),
            ("NaN", "Invalid"),  # Decimal() accepts these spellings even
            ("Infinity", "Invalid"),  # though float() is what the old code feared
            (True, "Invalid"),  # bool is an int subclass; True is not 1
            ("1e40", "too large"),  # would overflow NUMERIC(15,2)
        ],
    )
    def test_rejects_bad_amounts(self, raw, expected):
        from backend import money

        value, error = money.to_decimal(raw)
        assert value is None
        assert expected.lower() in error.lower()

    def test_accepts_and_rounds_valid_amount(self):
        from backend import money

        value, error = money.to_decimal("100.567")
        assert error is None
        assert value == Decimal("100.57")
        assert isinstance(value, Decimal)

    def test_parses_from_text_not_through_float(self):
        """The whole point of the module: no float ever touches the value.

        Decimal(0.1) is 0.1000000000000000055511151231257827, because that is
        what the float actually holds. Parsing from the string form is exact,
        and this asserts we take the second path.
        """
        from backend import money

        value, error = money.to_decimal("0.07")
        assert error is None
        assert value == Decimal("0.07")
        assert str(value) == "0.07"

    def test_repeated_addition_does_not_drift(self):
        """0.1 + 0.2 != 0.3 in float64. It must hold exactly here."""
        from backend import money

        total = sum(
            (money.to_decimal(x)[0] for x in ("0.10", "0.20")),
            Decimal("0"),
        )
        assert total == Decimal("0.30")

        cents = sum((money.to_decimal("0.01")[0] for _ in range(100)), Decimal("0"))
        assert cents == Decimal("1.00")


class TestMoneySerialisation:
    """Monetary values cross the wire as strings, never as JSON numbers."""

    def test_decimal_is_serialised_as_a_string(self, client):
        from decimal import Decimal as D

        from flask import jsonify

        app = client.application
        with app.test_request_context():
            body = jsonify({"balance": D("100000.00")}).get_data(as_text=True)
        # Quoted, and with the trailing zeros the database scale implies.
        assert '"balance": "100000.00"' in body or '"balance":"100000.00"' in body

    def test_full_precision_survives_serialisation(self, client):
        """A value with more digits than float64 can hold must round-trip."""
        from decimal import Decimal as D

        from flask import jsonify

        exact = D("12345678901234.56789")
        app = client.application
        with app.test_request_context():
            body = jsonify({"v": exact}).get_data(as_text=True)
        assert "12345678901234.56789" in body
        # The same value through float() would have lost the tail.
        assert str(float(exact)) not in body


@pytest.mark.db
class TestRateLimiting:
    """Brute force and account-creation abuse are refused before the DB is hit.

    These need the database because a rate-limited login still has to be a
    login: the limiter sits in front of a view that queries the users table.
    """

    def test_repeated_failed_logins_are_eventually_refused(self, limited_client):
        wrong = {"username": "nobody", "password": "wrong-password"}

        # The configured allowance is 10/minute. The first ten attempts are
        # answered normally - a wrong password is a 401, not a 429.
        for attempt in range(10):
            resp = limited_client.post("/api/login", json=wrong)
            assert resp.status_code == 401, f"attempt {attempt + 1} was {resp.status_code}"

        resp = limited_client.post("/api/login", json=wrong)
        assert resp.status_code == 429
        assert "Too many requests" in resp.get_json()["error"]

    def test_rate_limited_response_is_json_not_html(self, limited_client):
        """The API must not start emitting Werkzeug's HTML error page."""
        wrong = {"username": "nobody", "password": "wrong-password"}
        for _ in range(11):
            resp = limited_client.post("/api/login", json=wrong)

        assert resp.status_code == 429
        assert resp.content_type.startswith("application/json")
        assert "error" in resp.get_json()

    def test_registration_is_capped(self, limited_client):
        """Account creation is limited far more tightly than login."""
        for i in range(5):
            resp = limited_client.post(
                "/api/register",
                json={
                    "username": f"spam{i}",
                    "email": f"spam{i}@example.com",
                    "password": "correct-horse-9",
                },
            )
            assert resp.status_code == 201, f"registration {i + 1} was {resp.status_code}"

        resp = limited_client.post(
            "/api/register",
            json={
                "username": "spam-over",
                "email": "spam-over@example.com",
                "password": "correct-horse-9",
            },
        )
        assert resp.status_code == 429

    def test_a_correct_login_still_works_below_the_limit(self, limited_client):
        limited_client.post(
            "/api/register",
            json={
                "username": "realuser",
                "email": "realuser@example.com",
                "password": "correct-horse-9",
            },
        )
        for _ in range(3):
            assert (
                limited_client.post(
                    "/api/login",
                    json={
                        "username": "realuser",
                        "password": "wrong",
                    },
                ).status_code
                == 401
            )

        resp = limited_client.post(
            "/api/login",
            json={
                "username": "realuser",
                "password": "correct-horse-9",
            },
        )
        assert resp.status_code == 200


class TestProxyHeaderTrust:
    def test_forwarded_headers_are_ignored_by_default(self, app):
        """X-Forwarded-For must not reach the limiter unless a proxy is trusted.

        If it did, a client could send a different value on every request and
        get a fresh rate-limit bucket each time, which is worse than having no
        limiter at all because it looks like there is one.
        """
        from werkzeug.middleware.proxy_fix import ProxyFix

        assert not isinstance(app.wsgi_app, ProxyFix)
        assert config.TRUST_PROXY_HEADERS is False
