"""Apply pending database migrations.

`schema.sql` builds a fresh database. Every change made after that point lives
in a numbered file under `migrations/`, and this runner applies the ones a
given database has not seen yet, recording each in `schema_migrations`.

    python -m scripts.migrate              # apply pending migrations
    python -m scripts.migrate --status     # show what is applied and pending
    python -m scripts.migrate --dry-run    # print without applying

Each migration runs in its own transaction: a failure rolls that file back and
stops, leaving earlier migrations applied and the ledger accurate.
"""

import argparse
import hashlib
import logging
import pathlib
import sys

from backend import database

logger = logging.getLogger("migrate")

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    checksum    TEXT
)
"""


def discover():
    """Every migration file, ordered by filename."""
    if not MIGRATIONS_DIR.is_dir():
        raise SystemExit(f"No migrations directory at {MIGRATIONS_DIR}")
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def checksum(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_ledger():
    with database.transaction() as cur:
        cur.execute(LEDGER_DDL)


def applied_versions():
    ensure_ledger()
    rows = database.query_all(
        "SELECT version, checksum FROM schema_migrations ORDER BY version")
    return {version: digest for version, digest in rows}


def apply_one(path):
    """Run a single migration and record it, atomically."""
    text = path.read_text(encoding="utf-8")
    version = path.stem

    with database.transaction() as cur:
        cur.execute(text)
        cur.execute(
            "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s) "
            "ON CONFLICT (version) DO UPDATE SET checksum = EXCLUDED.checksum",
            (version, checksum(text)),
        )


def show_status():
    applied = applied_versions()
    files = discover()

    print(f"{'Migration':<40} {'Status':<10} Applied at")
    print("-" * 76)
    for path in files:
        version = path.stem
        if version in applied:
            when = database.query_value(
                "SELECT applied_at FROM schema_migrations WHERE version = %s",
                (version,))
            # A changed checksum means the file was edited after being applied,
            # which silently diverges this database from the repository.
            drifted = applied[version] not in (
                None, checksum(path.read_text(encoding="utf-8")))
            label = "CHANGED" if drifted else "applied"
            print(f"{version:<40} {label:<10} {when:%Y-%m-%d %H:%M:%S}")
        else:
            print(f"{version:<40} {'pending':<10} -")

    orphans = set(applied) - {p.stem for p in files}
    for version in sorted(orphans):
        print(f"{version:<40} {'ORPHAN':<10} recorded but no file present")

    pending = [p for p in files if p.stem not in applied]
    print(f"\n{len(files) - len(pending)} applied, {len(pending)} pending")
    return pending


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true",
                        help="show applied and pending migrations, then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would run without applying it")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.status:
        show_status()
        return

    applied = applied_versions()
    pending = [p for p in discover() if p.stem not in applied]

    if not pending:
        logger.info("Database is up to date (%d migrations applied)", len(applied))
        return

    logger.info("%d migration(s) pending", len(pending))
    for path in pending:
        if args.dry_run:
            logger.info("  would apply %s", path.stem)
            continue
        logger.info("  applying %s ...", path.stem)
        try:
            apply_one(path)
        except Exception as exc:
            logger.error("  FAILED %s: %s", path.stem, str(exc).splitlines()[0])
            logger.error("  Rolled back. Earlier migrations remain applied.")
            sys.exit(1)
        logger.info("  applied %s", path.stem)

    if not args.dry_run:
        logger.info("Done. %d migrations applied in total.",
                    len(applied) + len(pending))


if __name__ == "__main__":
    main()
