"""Read-only Postgres queries. All mutations go through the Immich REST API."""

import psycopg2
import psycopg2.extras

from curio.config import get_config


def _connect():
    cfg = get_config()
    conn = psycopg2.connect(**cfg.db_dsn)
    conn.set_session(readonly=True)
    return conn


def get_queue_depth() -> int:
    """Count scored assets not yet sent/decided."""
    cfg = get_config()
    sql = """
        SELECT COUNT(*) FROM asset a
        JOIN tag_asset ta ON ta."assetId" = a.id
        JOIN tag t ON t.id = ta."tagId"
        WHERE a."ownerId" = %s
        AND t.value IN ('print/scored/yes', 'print/scored/maybe')
        AND NOT EXISTS (
            SELECT 1 FROM tag_asset ta2
            JOIN tag t2 ON t2.id = ta2."tagId"
            WHERE ta2."assetId" = a.id
            AND t2.value IN ('print/queued', 'print/rejected', 'print/printed', 'print/telegram/sent')
        )
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (cfg.immich_user_id,))
            return cur.fetchone()[0]


def get_unscored_asset_ids(limit: int) -> list[str]:
    """Return asset IDs that have no print/* tag, favorites first."""
    cfg = get_config()
    sql = """
        SELECT a.id FROM asset a
        WHERE a."ownerId" = %s
        AND a.type = 'IMAGE'
        AND a.status = 'active'
        AND a.visibility != 'archive'
        AND NOT EXISTS (
            SELECT 1 FROM tag_asset ta JOIN tag t ON t.id = ta."tagId"
            WHERE ta."assetId" = a.id AND t.value LIKE 'print/%%'
        )
        ORDER BY
            CASE WHEN a."isFavorite" = true THEN 0 ELSE 1 END,
            RANDOM()
        LIMIT %s
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (cfg.immich_user_id, limit))
            return [row[0] for row in cur.fetchall()]


def get_orphaned_sent_asset_ids() -> list[str]:
    """Return assets tagged print/telegram/sent but with no decision — orphaned by a restart."""
    cfg = get_config()
    sql = """
        SELECT a.id FROM asset a
        JOIN tag_asset ta ON ta."assetId" = a.id
        JOIN tag t ON t.id = ta."tagId"
        WHERE a."ownerId" = %s
        AND t.value = 'print/telegram/sent'
        AND NOT EXISTS (
            SELECT 1 FROM tag_asset ta2
            JOIN tag t2 ON t2.id = ta2."tagId"
            WHERE ta2."assetId" = a.id
            AND t2.value IN ('print/queued', 'print/rejected', 'print/printed')
        )
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (cfg.immich_user_id,))
            return [row[0] for row in cur.fetchall()]


def get_next_queued_asset_id() -> str | None:
    """Return one scored/queued asset that hasn't been sent to Telegram yet."""
    cfg = get_config()
    sql = """
        SELECT a.id FROM asset a
        JOIN tag_asset ta ON ta."assetId" = a.id
        JOIN tag t ON t.id = ta."tagId"
        WHERE a."ownerId" = %s
        AND t.value IN ('print/scored/yes', 'print/scored/maybe')
        AND NOT EXISTS (
            SELECT 1 FROM tag_asset ta2
            JOIN tag t2 ON t2.id = ta2."tagId"
            WHERE ta2."assetId" = a.id
            AND t2.value IN ('print/queued', 'print/rejected', 'print/printed', 'print/telegram/sent')
        )
        LIMIT 1
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (cfg.immich_user_id,))
            row = cur.fetchone()
            return row[0] if row else None
