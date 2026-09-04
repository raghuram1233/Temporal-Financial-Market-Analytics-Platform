"""Centralised, validated application configuration.

Every setting is read from the environment, optionally seeded by a local
.env file. Secrets deliberately have no default: a missing value raises
ConfigError at import time instead of silently falling back to a
well-known credential that ships in the source tree.
"""

import os
import secrets
from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unusable."""


# Values that were previously hardcoded as fallbacks. Refusing them stops a
# leaked development credential from quietly becoming the production one.
_REJECTED_SECRETS = {
    "change-me-in-production",
    "your_secret_key_here",
    "secret",
    "changeme",
}


def _required(name, hint=""):
    value = os.getenv(name)
    if not value or not value.strip():
        suffix = f"\n  {hint}" if hint else ""
        raise ConfigError(f"Required environment variable {name} is not set.{suffix}")
    return value.strip()


def _text(name, default):
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _integer(name, default):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _flag(name, default):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv(name):
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


# --- Runtime mode ---------------------------------------------------------
DEBUG = _flag("FLASK_DEBUG", False)

# --- Database -------------------------------------------------------------
DB_HOST = _text("DB_HOST", "localhost")
DB_NAME = _text("DB_NAME", "temporal")
DB_USER = _text("DB_USER", "postgres")
DB_PORT = _integer("DB_PORT", 5432)
DB_PASS = _required(
    "DB_PASS",
    "Set it in .env (see .env.example). There is no default password.",
)

DB_POOL_MIN = _integer("DB_POOL_MIN", 1)
DB_POOL_MAX = _integer("DB_POOL_MAX", 20)

# --- Flask ----------------------------------------------------------------
SECRET_KEY = _required(
    "FLASK_SECRET_KEY",
    'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"',
)
if SECRET_KEY.lower() in _REJECTED_SECRETS:
    raise ConfigError(
        "FLASK_SECRET_KEY is set to a well-known placeholder value. "
        "Anyone who reads this repository could forge session cookies.\n"
        '  Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
    )
if len(SECRET_KEY) < 32:
    raise ConfigError("FLASK_SECRET_KEY must be at least 32 characters.")

# Session cookies are HTTPS-only unless explicitly relaxed for local dev.
SESSION_COOKIE_SECURE = _flag("SESSION_COOKIE_SECURE", not DEBUG)
SESSION_COOKIE_SAMESITE = _text("SESSION_COOKIE_SAMESITE", "Lax")
PERMANENT_SESSION_LIFETIME_MINUTES = _integer("SESSION_LIFETIME_MINUTES", 720)

# Empty by default: the UI is served by this app, so no cross-origin browser
# client needs access. Listing an origin here also enables credentialed CORS,
# so it must never be widened to "*" while session cookies are in use.
CORS_ORIGINS = _csv("CORS_ORIGINS")

# --- Behaviour ------------------------------------------------------------
AUTO_INIT_DB = _flag("AUTO_INIT_DB", False)
ENABLE_SCHEDULER = _flag("ENABLE_SCHEDULER", True)
MAX_DEPOSIT = _integer("MAX_DEPOSIT", 500_000)
MAX_PASSWORD_BYTES = 72  # bcrypt truncates silently beyond this
MIN_PASSWORD_LENGTH = _integer("MIN_PASSWORD_LENGTH", 8)


def generate_secret_key():
    """Convenience helper for setup scripts and documentation."""
    return secrets.token_hex(32)
