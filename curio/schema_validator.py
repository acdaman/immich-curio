"""
Validates that the Immich Postgres schema matches documented expectations.
Exits with a non-zero code if any check fails — the container is considered unhealthy.
"""

import sys
import psycopg2
import psycopg2.extras

from curio.config import get_config

EXPECTED_TABLES = {
    "asset": {
        "required_columns": ["id", "type", "isFavorite", "status", "visibility", "ownerId"],
        "forbidden_columns": ["isArchived", "isTrashed"],
    },
    "tag": {
        "required_columns": ["id", "value"],
        "forbidden_columns": ["name"],
    },
    "tag_asset": {
        "required_columns": ["assetId", "tagId"],
    },
    "album": {
        "required_columns": ["id", "albumName"],
    },
    "album_asset": {
        "required_columns": ["albumId", "assetId"],
    },
}


def _get_table_columns(conn, table_name: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = %s
            """,
            (table_name,),
        )
        return {row[0] for row in cur.fetchall()}


def validate_schema(conn=None) -> None:
    cfg = get_config()
    owns_conn = conn is None

    if owns_conn:
        conn = psycopg2.connect(**cfg.db_dsn)
        conn.set_session(readonly=True)

    try:
        errors: list[str] = []

        for table, checks in EXPECTED_TABLES.items():
            cols = _get_table_columns(conn, table)

            if not cols:
                errors.append(f"Table '{table}' not found")
                continue

            for col in checks.get("required_columns", []):
                if col not in cols:
                    errors.append(f"Table '{table}': expected column '{col}' not found (got: {sorted(cols)})")

            for col in checks.get("forbidden_columns", []):
                if col in cols:
                    errors.append(
                        f"Table '{table}': forbidden column '{col}' exists — schema has changed"
                    )

        if errors:
            for msg in errors:
                print(f"SCHEMA ERROR: {msg}", file=sys.stderr)
            sys.exit(1)

    finally:
        if owns_conn:
            conn.close()
