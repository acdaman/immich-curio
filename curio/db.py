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


def get_unscored_asset_ids(limit: int) -> list[tuple[str, bool]]:
    """Return (asset_id, is_favorite) tuples with no print/* tag, 50/50 split between favorites and general."""
    cfg = get_config()
    base_filter = """
        FROM asset a
        WHERE a."ownerId" = %s
        AND a.type = 'IMAGE'
        AND a.status = 'active'
        AND a.visibility != 'archive'
        AND NOT EXISTS (
            SELECT 1 FROM tag_asset ta JOIN tag t ON t.id = ta."tagId"
            WHERE ta."assetId" = a.id AND t.value LIKE 'print/%%'
        )
    """
    half = max(1, limit // 2)
    sql = f"""
        (SELECT a.id, a."isFavorite" {base_filter} AND a."isFavorite" = true  ORDER BY RANDOM() LIMIT %s)
        UNION ALL
        (SELECT a.id, a."isFavorite" {base_filter} AND a."isFavorite" = false ORDER BY RANDOM() LIMIT %s)
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (cfg.immich_user_id, half, cfg.immich_user_id, half))
            return [(row[0], row[1]) for row in cur.fetchall()]


def get_sample_approved_asset_ids(limit: int) -> list[str]:
    """Return a random sample of assets approved for printing (tagged print/queued)."""
    cfg = get_config()
    sql = """
        SELECT a.id FROM asset a
        JOIN tag_asset ta ON ta."assetId" = a.id
        JOIN tag t ON t.id = ta."tagId"
        WHERE a."ownerId" = %s
        AND t.value = 'print/queued'
        ORDER BY RANDOM()
        LIMIT %s
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (cfg.immich_user_id, limit))
            return [row[0] for row in cur.fetchall()]


def get_sample_rejected_asset_ids(limit: int) -> list[str]:
    """Return a random sample of assets rejected from printing (tagged print/rejected)."""
    cfg = get_config()
    sql = """
        SELECT a.id FROM asset a
        JOIN tag_asset ta ON ta."assetId" = a.id
        JOIN tag t ON t.id = ta."tagId"
        WHERE a."ownerId" = %s
        AND t.value = 'print/rejected'
        ORDER BY RANDOM()
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


def has_pending_sent_photo() -> bool:
    """Return True if a photo is currently awaiting review (sent but not decided)."""
    cfg = get_config()
    sql = """
        SELECT 1 FROM asset a
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
        LIMIT 1
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (cfg.immich_user_id,))
            return cur.fetchone() is not None


def get_stats() -> dict:
    """Return a dict of pipeline statistics for the /stats command."""
    cfg = get_config()
    uid = cfg.immich_user_id

    def count(cur, sql, params=()):
        cur.execute(sql, params)
        return cur.fetchone()[0]

    with _connect() as conn:
        with conn.cursor() as cur:
            # Library
            library_total = count(cur, """
                SELECT COUNT(*) FROM asset
                WHERE "ownerId" = %s AND type = 'IMAGE' AND status = 'active' AND visibility != 'archive'
            """, (uid,))

            library_favs = count(cur, """
                SELECT COUNT(*) FROM asset
                WHERE "ownerId" = %s AND type = 'IMAGE' AND status = 'active'
                AND visibility != 'archive' AND "isFavorite" = true
            """, (uid,))

            # Pipeline
            def tag_count(tag):
                return count(cur, """
                    SELECT COUNT(DISTINCT ta."assetId") FROM tag_asset ta
                    JOIN tag t ON t.id = ta."tagId"
                    JOIN asset a ON a.id = ta."assetId"
                    WHERE a."ownerId" = %s AND t.value = %s
                """, (uid, tag))

            gemini_yes   = tag_count("print/scored/yes")
            gemini_maybe = tag_count("print/scored/maybe")
            gemini_no    = tag_count("print/scored/no")
            total_scored = gemini_yes + gemini_maybe + gemini_no

            queue_depth = count(cur, """
                SELECT COUNT(DISTINCT a.id) FROM asset a
                JOIN tag_asset ta ON ta."assetId" = a.id
                JOIN tag t ON t.id = ta."tagId"
                WHERE a."ownerId" = %s
                AND t.value IN ('print/scored/yes', 'print/scored/maybe')
                AND NOT EXISTS (
                    SELECT 1 FROM tag_asset ta2 JOIN tag t2 ON t2.id = ta2."tagId"
                    WHERE ta2."assetId" = a.id
                    AND t2.value IN ('print/queued','print/rejected','print/printed','print/telegram/sent')
                )
            """, (uid,))

            unscored = count(cur, """
                SELECT COUNT(*) FROM asset a
                WHERE a."ownerId" = %s AND a.type = 'IMAGE' AND a.status = 'active'
                AND a.visibility != 'archive'
                AND NOT EXISTS (
                    SELECT 1 FROM tag_asset ta JOIN tag t ON t.id = ta."tagId"
                    WHERE ta."assetId" = a.id AND t.value LIKE 'print/%%'
                )
            """, (uid,))

            # Decisions
            approved = tag_count("print/queued")
            liked    = tag_count("print/liked")
            rejected_total = tag_count("print/rejected")
            rejected = rejected_total - liked
            total_reviewed = approved + rejected_total

            # Gemini calibration: of yes/maybe photos that have been decided
            def tag_pair_count(score_tag, decision_tag):
                return count(cur, """
                    SELECT COUNT(DISTINCT a.id) FROM asset a
                    JOIN tag_asset ta1 ON ta1."assetId" = a.id JOIN tag t1 ON t1.id = ta1."tagId"
                    JOIN tag_asset ta2 ON ta2."assetId" = a.id JOIN tag t2 ON t2.id = ta2."tagId"
                    WHERE a."ownerId" = %s AND t1.value = %s AND t2.value = %s
                """, (uid, score_tag, decision_tag))

            yes_approved = tag_pair_count("print/scored/yes",   "print/queued")
            yes_rejected = tag_pair_count("print/scored/yes",   "print/rejected")
            maybe_approved = tag_pair_count("print/scored/maybe", "print/queued")
            maybe_rejected = tag_pair_count("print/scored/maybe", "print/rejected")

            # Liked breakdown: yes-scored liked = likely pre-existing favs, maybe-scored = new finds
            liked_from_yes   = tag_pair_count("print/scored/yes",   "print/liked")
            liked_from_maybe = tag_pair_count("print/scored/maybe", "print/liked")

    return {
        "library_total": library_total,
        "library_favs": library_favs,
        "unscored": unscored,
        "gemini_yes": gemini_yes,
        "gemini_maybe": gemini_maybe,
        "gemini_no": gemini_no,
        "total_scored": total_scored,
        "queue_depth": queue_depth,
        "approved": approved,
        "liked": liked,
        "rejected": rejected,
        "total_reviewed": total_reviewed,
        "yes_approved": yes_approved,
        "yes_rejected": yes_rejected,
        "maybe_approved": maybe_approved,
        "maybe_rejected": maybe_rejected,
        "liked_from_yes": liked_from_yes,
        "liked_from_maybe": liked_from_maybe,
    }


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
