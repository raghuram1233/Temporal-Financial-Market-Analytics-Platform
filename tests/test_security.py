"""Security tests: authentication, CSRF, and input validation.

None of these need a database - every check short-circuits before the view
body runs, which is exactly the property being asserted.
"""

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
    "/dashboard", "/markets", "/portfolio",
    "/trade", "/analytics", "/transactions",
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

    @pytest.mark.parametrize("path,body", [
        ("/api/login", {"username": "a", "password": "b"}),
        ("/api/register", {"username": "a", "email": "a@b.c", "password": "abcd1234"}),
        ("/api/logout", {}),
        ("/api/wallet/deposit", {"amount": 100}),
        ("/api/wallet/withdraw", {"amount": 100}),
        ("/api/order", {"asset_id": 1, "order_type": "buy", "quantity": 1}),
    ])
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

    @pytest.mark.parametrize("payload,expected", [
        ({}, "required"),
        ({"username": "a", "email": "a@b.c"}, "required"),
        ({"username": "a", "email": "not-an-email", "password": "abcd1234"}, "valid email"),
        ({"username": "a", "email": "a@b.c", "password": "short"}, "at least"),
        ({"username": "x" * 65, "email": "a@b.c", "password": "abcd1234"}, "64 characters"),
    ])
    def test_bad_input_is_rejected(self, client, payload, expected):
        resp = client.post("/api/register", json=payload)
        assert resp.status_code == 400
        assert expected.lower() in resp.get_json()["error"].lower()

    def test_password_over_bcrypt_limit_is_rejected(self, client):
        """bcrypt silently truncates past 72 bytes, weakening long passphrases."""
        resp = client.post("/api/register", json={
            "username": "bob",
            "email": "bob@example.com",
            "password": "a" * (config.MAX_PASSWORD_BYTES + 1),
        })
        assert resp.status_code == 400
        assert "72" in resp.get_json()["error"]


class TestAmountValidation:
    """parse_amount guards every monetary endpoint."""

    @pytest.mark.parametrize("raw,expected", [
        (None, "required"),
        ("abc", "Invalid"),
        (0, "greater than zero"),
        (-50, "greater than zero"),
        (float("nan"), "Invalid"),
        (float("inf"), "Invalid"),
    ])
    def test_rejects_bad_amounts(self, raw, expected):
        from backend.app import parse_amount
        value, error = parse_amount(raw)
        assert value is None
        assert expected.lower() in error.lower()

    def test_accepts_and_rounds_valid_amount(self):
        from backend.app import parse_amount
        value, error = parse_amount("100.567")
        assert error is None
        assert value == 100.57
