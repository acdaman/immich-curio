#!/usr/bin/env python3
"""
Phase 0 exploration: Introspect the Immich Postgres schema.

Run this script to validate actual column names and test the queue SQL.
Paste the output into CLAUDE.md under "Validated DB Schema".

Usage:
    cd /home/adam/projects/curio
    source .venv/bin/activate
    pip install psycopg2-binary python-dotenv
    python explore/01_db_schema.py
"""

import os
import sys
import json
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

TABLES_OF_INTEREST = ["assets", "tags", "tag_asset", "albums", "album_assets"]


def run(conn, sql, params=None):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def main():
    print("=" * 60)
    print("Curio Phase 0 — DB Schema Discovery")
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
    # 1. Column names and types for all tables of interest
    # -------------------------------------------------------------------------
    print("=" * 60)
    print("1. COLUMN NAMES & TYPES")
    print("=" * 60)

    rows = run(conn, """
        SELECT table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = ANY(%s)
        ORDER BY table_name, ordinal_position
    """, (TABLES_OF_INTEREST,))

    current_table = None
    schema_map = {}
    for row in rows:
        tbl = row["table_name"]
        if tbl not in schema_map:
            schema_map[tbl] = []
        schema_map[tbl].append(row["column_name"])
        if tbl != current_table:
            print(f"\n  [{tbl}]")
            current_table = tbl
        nullable = "NULL" if row["is_nullable"] == "YES" else "NOT NULL"
        print(f"    {row['column_name']:40s} {row['data_type']:20s} {nullable}")

    missing = [t for t in TABLES_OF_INTEREST if t not in schema_map]
    if missing:
        print(f"\nWARNING: Tables not found: {missing}")
    else:
        print(f"\nAll {len(TABLES_OF_INTEREST)} expected tables found.")

    # -------------------------------------------------------------------------
    # 2. Row counts
    # -------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("2. ROW COUNTS")
    print("=" * 60)
    for tbl in TABLES_OF_INTEREST:
        if tbl in schema_map:
            try:
                rows_count = run(conn, f'SELECT COUNT(*) as n FROM "{tbl}"')
                print(f"  {tbl:20s} {rows_count[0]['n']:>10,} rows")
            except Exception as e:
                print(f"  {tbl:20s} ERROR: {e}")

    # -------------------------------------------------------------------------
    # 3. assets.type enum values
    # -------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("3. assets.type DISTINCT VALUES (need 'IMAGE' confirmed)")
    print("=" * 60)
    try:
        type_col = "type"
        if "type" in schema_map.get("assets", []):
            rows2 = run(conn, 'SELECT DISTINCT "type", COUNT(*) as n FROM assets GROUP BY "type" ORDER BY n DESC')
            for r in rows2:
                print(f"  '{r['type']}' — {r['n']:,} assets")
        else:
            print("  WARNING: 'type' column not found in assets table")
    except Exception as e:
        print(f"  ERROR: {e}")

    # -------------------------------------------------------------------------
    # 4. Existing tags
    # -------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("4. EXISTING TAGS")
    print("=" * 60)
    try:
        tag_rows = run(conn, "SELECT name, id FROM tags ORDER BY name")
        if tag_rows:
            for r in tag_rows:
                print(f"  {r['name']:50s}  id={r['id']}")
        else:
            print("  (no tags yet)")
    except Exception as e:
        print(f"  ERROR: {e}")

    # -------------------------------------------------------------------------
    # 5. isFavorite / isArchived / isTrashed — confirm column names
    # -------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("5. KEY COLUMN NAME CHECK (camelCase vs snake_case)")
    print("=" * 60)
    asset_cols = schema_map.get("assets", [])
    checks = {
        "isFavorite": ["isFavorite", "is_favorite"],
        "isArchived": ["isArchived", "is_archived"],
        "isTrashed": ["isTrashed", "is_trashed"],
        "deletedAt": ["deletedAt", "deleted_at"],
    }
    for logical, candidates in checks.items():
        found = next((c for c in candidates if c in asset_cols), None)
        if found:
            print(f"  {logical:15s} → actual column: \"{found}\"")
        else:
            print(f"  {logical:15s} → NOT FOUND (columns: {asset_cols})")

    # -------------------------------------------------------------------------
    # 6. albums — albumName vs name
    # -------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("6. albums TABLE — albumName vs name")
    print("=" * 60)
    album_cols = schema_map.get("albums", [])
    print(f"  All albums columns: {album_cols}")
    name_col = None
    for candidate in ["albumName", "album_name", "name"]:
        if candidate in album_cols:
            name_col = candidate
            break
    if name_col:
        print(f"  Name column is: \"{name_col}\"")
        sample = run(conn, f'SELECT "{name_col}" FROM albums LIMIT 5')
        print(f"  Sample album names: {[r[name_col] for r in sample]}")
    else:
        print("  WARNING: Could not identify name column in albums table")

    # -------------------------------------------------------------------------
    # 7. join table column names
    # -------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("7. JOIN TABLE COLUMNS (assetsId vs assets_id etc.)")
    print("=" * 60)
    for tbl in ["tag_asset", "album_assets"]:
        cols = schema_map.get(tbl, [])
        print(f"  {tbl}: {cols}")

    # -------------------------------------------------------------------------
    # 8. Test the queue SQL from the brief
    # -------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("8. QUEUE SQL TEST")
    print("=" * 60)

    # Detect the correct column names from what we found
    assets_cols = schema_map.get("assets", [])
    tag_asset_cols = schema_map.get("tag_asset", [])

    # Try to detect join column names
    assets_id_col = next((c for c in tag_asset_cols if "asset" in c.lower()), None)
    tags_id_col = next((c for c in tag_asset_cols if "tag" in c.lower()), None)

    if assets_id_col and tags_id_col:
        print(f"  tag_asset join columns: {assets_id_col}, {tags_id_col}")
        try:
            queue_sql = f"""
                SELECT COUNT(*) as n FROM assets a
                JOIN tag_asset ta ON ta."{assets_id_col}" = a.id
                JOIN tags t ON t.id = ta."{tags_id_col}"
                WHERE t.name IN ('print/scored/yes', 'print/scored/maybe')
                AND NOT EXISTS (
                    SELECT 1 FROM tag_asset ta2
                    JOIN tags t2 ON t2.id = ta2."{tags_id_col}"
                    WHERE ta2."{assets_id_col}" = a.id
                    AND t2.name IN ('print/queued', 'print/rejected', 'print/printed')
                )
            """
            result = run(conn, queue_sql)
            print(f"  Queue SQL ran OK — current queue depth: {result[0]['n']}")
        except Exception as e:
            print(f"  Queue SQL FAILED: {e}")
    else:
        print(f"  WARNING: Could not determine join column names from: {tag_asset_cols}")

    # -------------------------------------------------------------------------
    # 9. Test unscored candidate query
    # -------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("9. UNSCORED CANDIDATE QUERY TEST")
    print("=" * 60)

    # Detect isFavorite, isArchived, isTrashed column names
    def find_col(candidates, available):
        return next((c for c in candidates if c in available), None)

    is_favorite_col = find_col(["isFavorite", "is_favorite"], assets_cols)
    is_archived_col = find_col(["isArchived", "is_archived"], assets_cols)
    is_trashed_col = find_col(["isTrashed", "is_trashed"], assets_cols)

    if all([is_favorite_col, is_archived_col, is_trashed_col, assets_id_col, tags_id_col]):
        try:
            unscored_sql = f"""
                SELECT COUNT(*) as n FROM assets a
                WHERE a.type = 'IMAGE'
                AND a."{is_archived_col}" = false
                AND a."{is_trashed_col}" = false
                AND NOT EXISTS (
                    SELECT 1 FROM tag_asset ta JOIN tags t ON t.id = ta."{tags_id_col}"
                    WHERE ta."{assets_id_col}" = a.id AND t.name LIKE 'print/%'
                )
            """
            result = run(conn, unscored_sql)
            print(f"  Unscored candidate query ran OK — {result[0]['n']:,} unscored IMAGE assets")

            # Check favourites
            fav_sql = f"""
                SELECT COUNT(*) as n FROM assets a
                WHERE a.type = 'IMAGE'
                AND a."{is_archived_col}" = false
                AND a."{is_trashed_col}" = false
                AND a."{is_favorite_col}" = true
                AND NOT EXISTS (
                    SELECT 1 FROM tag_asset ta JOIN tags t ON t.id = ta."{tags_id_col}"
                    WHERE ta."{assets_id_col}" = a.id AND t.name LIKE 'print/%'
                )
            """
            result = run(conn, fav_sql)
            print(f"  Unscored FAVOURITE assets: {result[0]['n']:,}")
        except Exception as e:
            print(f"  FAILED: {e}")
    else:
        print(f"  Skipping — could not detect all required columns")
        print(f"  isFavorite={is_favorite_col}, isArchived={is_archived_col}, isTrashed={is_trashed_col}")
        print(f"  tag_asset: assetsId={assets_id_col}, tagsId={tags_id_col}")

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("SUMMARY — paste into CLAUDE.md 'Validated DB Schema' section")
    print("=" * 60)
    print(f"""
Tables found: {list(schema_map.keys())}

Column name findings:
  isFavorite column: {find_col(["isFavorite", "is_favorite"], assets_cols)}
  isArchived column: {find_col(["isArchived", "is_archived"], assets_cols)}
  isTrashed  column: {find_col(["isTrashed", "is_trashed"], assets_cols)}
  albums name column: {name_col}
  tag_asset assetsId: {assets_id_col}
  tag_asset tagsId:   {tags_id_col}
""")

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
