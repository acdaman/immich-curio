#!/usr/bin/env python3
"""
Phase 0 exploration: Discover timestamp/EXIF schema and measure burst group sizes.

Run this before implementing burst group processing.

Usage:
    cd /path/to/curio
    source .venv/bin/activate
    python explore/05_burst_groups.py
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: psycopg2-binary not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "immich"),
    "user": os.getenv("DB_USER", "immich_reader"),
    "password": os.getenv("DB_PASSWORD", ""),
}
IMMICH_USER_ID = os.getenv("IMMICH_USER_ID", "")


def run(conn, sql, params=None):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def run_scalar(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


def main():
    print("=" * 60)
    print("Curio — Burst Group Schema Discovery")
    print("=" * 60)
    print(f"\nConnecting to {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']} as {DB_CONFIG['user']}...")

    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.set_session(readonly=True)
        print("Connected.\n")
    except Exception as e:
        print(f"ERROR: Could not connect: {e}")
        sys.exit(1)

    # -------------------------------------------------------------------------
    # 1. Does asset_exif exist? What columns?
    # -------------------------------------------------------------------------
    print("=" * 60)
    print("1. asset_exif TABLE — columns and key fields")
    print("=" * 60)

    exif_cols = run(conn, """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'asset_exif'
        ORDER BY ordinal_position
    """)

    asset_exif_exists = bool(exif_cols)
    if asset_exif_exists:
        print(f"  asset_exif EXISTS — {len(exif_cols)} columns:")
        for row in exif_cols:
            nullable = "NULL" if row["is_nullable"] == "YES" else "NOT NULL"
            print(f"    {row['column_name']:40s} {row['data_type']:20s} {nullable}")
        exif_col_names = {r["column_name"] for r in exif_cols}
        for key_col in ["assetId", "dateTimeOriginal", "make", "model", "exifImageWidth", "exifImageHeight", "fileSizeInByte"]:
            found = key_col in exif_col_names
            print(f"  Key column '{key_col}': {'FOUND' if found else 'MISSING'}")
    else:
        print("  asset_exif table NOT FOUND")

    # -------------------------------------------------------------------------
    # 2. Timestamp columns in asset table
    # -------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("2. asset TABLE — timestamp columns")
    print("=" * 60)

    ts_cols = run(conn, """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'asset'
          AND column_name IN (
            'localDateTime', 'fileCreatedAt', 'fileModifiedAt',
            'createdAt', 'updatedAt', 'deletedAt', 'localDate'
          )
        ORDER BY ordinal_position
    """)

    asset_ts_col_names = {r["column_name"] for r in ts_cols}
    if ts_cols:
        print("  Found timestamp columns in asset table:")
        for row in ts_cols:
            print(f"    {row['column_name']:30s} {row['data_type']}")
    else:
        print("  No expected timestamp columns found in asset table")

    # -------------------------------------------------------------------------
    # 3. duplicateId on asset table?
    # -------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("3. DUPLICATE DETECTION — duplicateId column or table")
    print("=" * 60)

    dup_col = run(conn, """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'asset'
          AND column_name ILIKE '%duplicate%'
    """)
    if dup_col:
        print(f"  asset.duplicateId FOUND: {dup_col}")
    else:
        print("  No 'duplicate*' column in asset table")

    dup_table = run(conn, """
        SELECT table_name FROM information_schema.tables
        WHERE table_name ILIKE '%duplicate%'
    """)
    if dup_table:
        print(f"  Duplicate-related tables: {[r['table_name'] for r in dup_table]}")
    else:
        print("  No duplicate-related tables found")

    # -------------------------------------------------------------------------
    # 4. Validate join and sample EXIF data
    # -------------------------------------------------------------------------
    if asset_exif_exists and "dateTimeOriginal" in exif_col_names and IMMICH_USER_ID:
        print("\n" + "=" * 60)
        print("4. EXIF JOIN VALIDATION — sample rows")
        print("=" * 60)
        try:
            sample = run(conn, """
                SELECT a.id, ae."dateTimeOriginal", ae.make, ae.model,
                       ae."exifImageWidth", ae."exifImageHeight"
                FROM asset a
                JOIN asset_exif ae ON ae."assetId" = a.id
                WHERE a."ownerId" = %s
                  AND a.type = 'IMAGE'
                  AND a.status = 'active'
                  AND ae."dateTimeOriginal" IS NOT NULL
                ORDER BY ae."dateTimeOriginal" DESC
                LIMIT 5
            """, (IMMICH_USER_ID,))
            print(f"  Sample rows ({len(sample)}):")
            for r in sample:
                print(f"    id={r['id'][:8]}... ts={r['dateTimeOriginal']} make={r['make']} model={r['model']} res={r['exifImageWidth']}x{r['exifImageHeight']}")
        except Exception as e:
            print(f"  ERROR: {e}")

        # -------------------------------------------------------------------------
        # 5. Burst group size distribution (60-second window, same model)
        # -------------------------------------------------------------------------
        print("\n" + "=" * 60)
        print("5. BURST GROUP SIZES — 60-second window, same camera model")
        print("=" * 60)

        try:
            # Group by: camera model + 60-second bucket
            groups = run(conn, """
                SELECT
                    ae.model,
                    DATE_TRUNC('minute', ae."dateTimeOriginal") +
                        (EXTRACT(SECOND FROM ae."dateTimeOriginal")::int / 60 * 60 ||' seconds')::interval AS bucket,
                    COUNT(*) AS group_size
                FROM asset a
                JOIN asset_exif ae ON ae."assetId" = a.id
                WHERE a."ownerId" = %s
                  AND a.type = 'IMAGE'
                  AND a.status = 'active'
                  AND a.visibility != 'archive'
                  AND ae."dateTimeOriginal" IS NOT NULL
                GROUP BY ae.model, bucket
                ORDER BY group_size DESC
                LIMIT 25
            """, (IMMICH_USER_ID,))

            print(f"  Top {len(groups)} largest groups:")
            for r in groups:
                print(f"    size={r['group_size']:3d}  model={str(r['model'])[:30]:30s}  bucket={r['bucket']}")

            # Histogram
            hist_sql = """
                SELECT
                    CASE
                        WHEN group_size = 1 THEN '1'
                        WHEN group_size BETWEEN 2 AND 5 THEN '2-5'
                        WHEN group_size BETWEEN 6 AND 15 THEN '6-15'
                        ELSE '16+'
                    END AS bucket,
                    COUNT(*) AS num_groups,
                    SUM(group_size) AS total_photos
                FROM (
                    SELECT
                        ae.model,
                        DATE_TRUNC('minute', ae."dateTimeOriginal") +
                            (EXTRACT(SECOND FROM ae."dateTimeOriginal")::int / 60 * 60 ||' seconds')::interval AS ts_bucket,
                        COUNT(*) AS group_size
                    FROM asset a
                    JOIN asset_exif ae ON ae."assetId" = a.id
                    WHERE a."ownerId" = %s
                      AND a.type = 'IMAGE'
                      AND a.status = 'active'
                      AND a.visibility != 'archive'
                      AND ae."dateTimeOriginal" IS NOT NULL
                    GROUP BY ae.model, ts_bucket
                ) subq
                GROUP BY bucket
                ORDER BY bucket
            """
            hist = run(conn, hist_sql, (IMMICH_USER_ID,))
            print("\n  Histogram:")
            print(f"  {'Group size':12s} {'# groups':12s} {'total photos':12s}")
            for r in hist:
                print(f"  {r['bucket']:12s} {r['num_groups']:12,} {r['total_photos']:12,}")

        except Exception as e:
            print(f"  ERROR: {e}")

        # -------------------------------------------------------------------------
        # 6. Test the burst peer query with a real seed
        # -------------------------------------------------------------------------
        print("\n" + "=" * 60)
        print("6. BURST PEER QUERY TEST — using a real group-of-2+ seed")
        print("=" * 60)

        try:
            # Find a seed that has at least one peer within 60s
            seed_row = run(conn, """
                SELECT a.id, ae."dateTimeOriginal", ae.model
                FROM asset a
                JOIN asset_exif ae ON ae."assetId" = a.id
                WHERE a."ownerId" = %s
                  AND a.type = 'IMAGE'
                  AND a.status = 'active'
                  AND a.visibility != 'archive'
                  AND ae."dateTimeOriginal" IS NOT NULL
                  AND ae.model IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM asset a2
                      JOIN asset_exif ae2 ON ae2."assetId" = a2.id
                      WHERE a2."ownerId" = %s
                        AND a2.type = 'IMAGE'
                        AND a2.status = 'active'
                        AND a2.id != a.id
                        AND ae2.model = ae.model
                        AND ae2."dateTimeOriginal" BETWEEN ae."dateTimeOriginal" - INTERVAL '60 seconds'
                                                        AND ae."dateTimeOriginal" + INTERVAL '60 seconds'
                  )
                LIMIT 1
            """, (IMMICH_USER_ID, IMMICH_USER_ID))

            if seed_row:
                seed = seed_row[0]
                print(f"  Seed: id={seed['id'][:8]}... ts={seed['dateTimeOriginal']} model={seed['model']}")
                peers = run(conn, """
                    SELECT a.id, a."isFavorite", ae."dateTimeOriginal", ae.model
                    FROM asset a
                    JOIN asset_exif ae ON ae."assetId" = a.id
                    WHERE a."ownerId" = %s
                      AND a.type = 'IMAGE'
                      AND a.status = 'active'
                      AND a.visibility != 'archive'
                      AND ae.model = %s
                      AND ae."dateTimeOriginal" BETWEEN %s - INTERVAL '1 second' * %s
                                                      AND %s + INTERVAL '1 second' * %s
                    ORDER BY ae."dateTimeOriginal"
                """, (IMMICH_USER_ID, seed['model'],
                      seed['dateTimeOriginal'], 60,
                      seed['dateTimeOriginal'], 60))
                print(f"  Burst peers ({len(peers)}):")
                for r in peers:
                    marker = " ← seed" if r['id'] == seed['id'] else ""
                    print(f"    id={r['id'][:8]}... ts={r['dateTimeOriginal']} fav={r['isFavorite']}{marker}")
            else:
                print("  No seed with burst peers found")
        except Exception as e:
            print(f"  ERROR: {e}")

        # -------------------------------------------------------------------------
        # 7. Coverage: how many scoreable images have EXIF timestamp?
        # -------------------------------------------------------------------------
        print("\n" + "=" * 60)
        print("7. EXIF COVERAGE — scoreable images with/without dateTimeOriginal")
        print("=" * 60)
        try:
            total = run_scalar(conn, """
                SELECT COUNT(*) FROM asset
                WHERE "ownerId" = %s AND type = 'IMAGE' AND status = 'active' AND visibility != 'archive'
            """, (IMMICH_USER_ID,))
            with_exif = run_scalar(conn, """
                SELECT COUNT(*) FROM asset a
                JOIN asset_exif ae ON ae."assetId" = a.id
                WHERE a."ownerId" = %s AND a.type = 'IMAGE' AND a.status = 'active'
                  AND a.visibility != 'archive' AND ae."dateTimeOriginal" IS NOT NULL
            """, (IMMICH_USER_ID,))
            print(f"  Total scoreable images: {total:,}")
            print(f"  With EXIF dateTimeOriginal: {with_exif:,} ({with_exif/total*100:.1f}%)")
            print(f"  Without (will use single-photo mode): {total-with_exif:,} ({(total-with_exif)/total*100:.1f}%)")
        except Exception as e:
            print(f"  ERROR: {e}")

    else:
        print("\n  Skipping sections 4-7 (asset_exif missing, no dateTimeOriginal, or IMMICH_USER_ID not set)")

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"""
asset_exif table exists: {asset_exif_exists}
Timestamp column to use: {"asset_exif.dateTimeOriginal" if asset_exif_exists and "dateTimeOriginal" in {r["column_name"] for r in exif_cols} else ("asset." + next(iter(asset_ts_col_names), "NONE"))}
Camera model filtering: {"YES (ae.model)" if asset_exif_exists and "model" in {r["column_name"] for r in exif_cols} else "NO"}
Resolution fields: {"YES (exifImageWidth/Height)" if asset_exif_exists and "exifImageWidth" in {r["column_name"] for r in exif_cols} else "NO"}
Recommended burst_window_seconds: 60
Recommended burst_group_max_size: 15
""")

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
