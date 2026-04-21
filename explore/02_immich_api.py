#!/usr/bin/env python3
"""
Phase 0 exploration: Validate every Immich REST API endpoint.

Run this to confirm auth header format, request/response shapes, and
that tag create/assign/delete lifecycle works.

Paste findings into CLAUDE.md under "Validated Immich API Endpoints".

Usage:
    cd /home/adam/projects/curio
    source .venv/bin/activate
    pip install httpx python-dotenv
    python explore/02_immich_api.py
"""

import os
import sys
import json
from dotenv import load_dotenv

load_dotenv()

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx")
    sys.exit(1)

IMMICH_URL = os.getenv("IMMICH_URL", "http://localhost:2283").rstrip("/")
API_KEY = os.getenv("IMMICH_API_KEY", "")

if not API_KEY:
    print("ERROR: IMMICH_API_KEY not set in .env")
    sys.exit(1)


def make_client(auth_style: str = "x-api-key") -> httpx.Client:
    if auth_style == "x-api-key":
        headers = {"x-api-key": API_KEY, "Accept": "application/json"}
    else:
        headers = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}
    return httpx.Client(base_url=IMMICH_URL, headers=headers, timeout=30)


def print_response(label: str, resp: httpx.Response, show_body: bool = True):
    print(f"\n  Status: {resp.status_code}")
    if show_body and resp.headers.get("content-type", "").startswith("application/json"):
        try:
            body = resp.json()
            if isinstance(body, list):
                print(f"  Body: list of {len(body)} items")
                if body:
                    print(f"  First item keys: {list(body[0].keys()) if isinstance(body[0], dict) else type(body[0])}")
            elif isinstance(body, dict):
                print(f"  Body keys: {list(body.keys())}")
                # Show a few key values
                for k in ["id", "name", "type", "isFavorite", "albumName", "version"]:
                    if k in body:
                        print(f"    {k}: {body[k]!r}")
        except Exception:
            print(f"  Body (raw): {resp.text[:200]}")
    elif show_body:
        print(f"  Content-Type: {resp.headers.get('content-type', 'unknown')}")
        print(f"  Content-Length: {len(resp.content)} bytes")


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def main():
    print("=" * 60)
    print("Curio Phase 0 — Immich API Validation")
    print("=" * 60)
    print(f"\nImmich URL: {IMMICH_URL}")

    # -------------------------------------------------------------------------
    # 1. Auth header format — try both styles
    # -------------------------------------------------------------------------
    section("1. AUTH HEADER FORMAT")
    working_auth = None

    for style in ["x-api-key", "bearer"]:
        print(f"\n  Trying auth style: {style}")
        client = make_client(style)
        try:
            resp = client.get("/api/server/about")
            print(f"  GET /api/server/about → {resp.status_code}")
            if resp.status_code == 200:
                print(f"  ✓ Auth style '{style}' WORKS")
                working_auth = style
                body = resp.json()
                print(f"  Server version: {body.get('version', 'unknown')}")
                break
            else:
                print(f"  ✗ Auth style '{style}' failed: {resp.status_code}")
        except Exception as e:
            print(f"  ERROR: {e}")

    if not working_auth:
        print("\nERROR: Neither auth style worked. Check IMMICH_URL and IMMICH_API_KEY.")
        sys.exit(1)

    client = make_client(working_auth)
    print(f"\n  Using auth style: {working_auth}")

    # -------------------------------------------------------------------------
    # 2. Get a sample asset ID to use in subsequent tests
    # -------------------------------------------------------------------------
    section("2. GET SAMPLE ASSET ID")
    sample_asset_id = None
    sample_asset = None

    # Try search endpoint to find any image
    try:
        resp = client.post("/api/search/metadata", json={
            "type": "IMAGE",
            "page": 1,
            "size": 1,
        })
        print(f"  POST /api/search/metadata → {resp.status_code}")
        if resp.status_code == 200:
            body = resp.json()
            assets = body.get("assets", {}).get("items", [])
            if assets:
                sample_asset_id = assets[0]["id"]
                sample_asset = assets[0]
                print(f"  Sample asset ID: {sample_asset_id}")
                print(f"  Asset keys: {list(assets[0].keys())}")
                print(f"  isFavorite: {assets[0].get('isFavorite')}")
                print(f"  type: {assets[0].get('type')}")
            else:
                print("  No assets found in search response")
        else:
            print(f"  Body: {resp.text[:300]}")
    except Exception as e:
        print(f"  ERROR: {e}")

    if not sample_asset_id:
        print("\nWARNING: Could not find a sample asset. Some tests will be skipped.")

    # -------------------------------------------------------------------------
    # 3. GET /api/assets/{id}
    # -------------------------------------------------------------------------
    section("3. GET /api/assets/{id}")
    if sample_asset_id:
        try:
            resp = client.get(f"/api/assets/{sample_asset_id}")
            print(f"  GET /api/assets/{sample_asset_id}")
            print_response("asset", resp)
        except Exception as e:
            print(f"  ERROR: {e}")
    else:
        print("  Skipped — no sample asset ID")

    # -------------------------------------------------------------------------
    # 4. GET /api/assets/{id}/thumbnail
    # -------------------------------------------------------------------------
    section("4. GET /api/assets/{id}/thumbnail?size=preview")
    thumbnail_bytes = None
    if sample_asset_id:
        try:
            resp = client.get(f"/api/assets/{sample_asset_id}/thumbnail", params={"size": "preview"})
            print(f"  GET /api/assets/{sample_asset_id}/thumbnail?size=preview")
            print(f"  Status: {resp.status_code}")
            print(f"  Content-Type: {resp.headers.get('content-type', 'unknown')}")
            print(f"  Content-Length: {len(resp.content):,} bytes")
            if resp.status_code == 200:
                thumbnail_bytes = resp.content
                print(f"  ✓ Thumbnail OK")
        except Exception as e:
            print(f"  ERROR: {e}")
    else:
        print("  Skipped — no sample asset ID")

    # -------------------------------------------------------------------------
    # 5. POST /api/tags — create a test tag
    # -------------------------------------------------------------------------
    section("5. POST /api/tags (create test tag)")
    test_tag_id = None
    test_tag_name = "curio/test-tag"
    try:
        resp = client.post("/api/tags", json={"name": test_tag_name})
        print(f"  POST /api/tags {{name: '{test_tag_name}'}}")
        print_response("tag", resp)
        if resp.status_code in (200, 201):
            body = resp.json()
            test_tag_id = body.get("id")
            print(f"  ✓ Tag created, ID: {test_tag_id}")
        elif resp.status_code == 409:
            print("  Tag already exists (409) — fetching existing ID")
            tags_resp = client.get("/api/tags")
            if tags_resp.status_code == 200:
                for tag in tags_resp.json():
                    if tag.get("name") == test_tag_name:
                        test_tag_id = tag["id"]
                        print(f"  Found existing tag ID: {test_tag_id}")
                        break
        else:
            print(f"  ✗ Unexpected status: {resp.status_code}")
            print(f"  Body: {resp.text[:300]}")
    except Exception as e:
        print(f"  ERROR: {e}")

    # -------------------------------------------------------------------------
    # 6. PUT /api/tags/{id}/assets — assign tag
    # -------------------------------------------------------------------------
    section("6. PUT /api/tags/{id}/assets (assign tag)")
    if test_tag_id and sample_asset_id:
        # Try {"ids": [...]} format first
        for body_format, body in [
            ('{"ids": [...]}', {"ids": [sample_asset_id]}),
            ('{"assetIds": [...]}', {"assetIds": [sample_asset_id]}),
        ]:
            try:
                resp = client.put(f"/api/tags/{test_tag_id}/assets", json=body)
                print(f"  PUT /api/tags/{test_tag_id}/assets with {body_format}")
                print(f"  Status: {resp.status_code}")
                if resp.status_code in (200, 201, 204):
                    print(f"  ✓ Tag assigned with format: {body_format}")
                    if resp.content:
                        print(f"  Body: {resp.text[:200]}")
                    break
                else:
                    print(f"  ✗ Failed: {resp.text[:200]}")
            except Exception as e:
                print(f"  ERROR: {e}")
    else:
        print("  Skipped — missing tag ID or asset ID")

    # -------------------------------------------------------------------------
    # 7. DELETE /api/tags/{id}/assets — remove tag
    # -------------------------------------------------------------------------
    section("7. DELETE /api/tags/{id}/assets (remove tag)")
    if test_tag_id and sample_asset_id:
        for body_format, body in [
            ('{"ids": [...]}', {"ids": [sample_asset_id]}),
            ('{"assetIds": [...]}', {"assetIds": [sample_asset_id]}),
        ]:
            try:
                resp = client.request("DELETE", f"/api/tags/{test_tag_id}/assets", json=body)
                print(f"  DELETE /api/tags/{test_tag_id}/assets with {body_format}")
                print(f"  Status: {resp.status_code}")
                if resp.status_code in (200, 201, 204):
                    print(f"  ✓ Tag removed with format: {body_format}")
                    break
                else:
                    print(f"  ✗ Failed ({resp.status_code}): {resp.text[:200]}")
            except Exception as e:
                print(f"  ERROR: {e}")
    else:
        print("  Skipped — missing tag ID or asset ID")

    # -------------------------------------------------------------------------
    # 8. Clean up: delete the test tag
    # -------------------------------------------------------------------------
    section("8. DELETE /api/tags/{id} (cleanup)")
    if test_tag_id:
        try:
            resp = client.delete(f"/api/tags/{test_tag_id}")
            print(f"  DELETE /api/tags/{test_tag_id}")
            print(f"  Status: {resp.status_code}")
            if resp.status_code in (200, 204):
                print("  ✓ Test tag deleted")
            else:
                print(f"  ✗ {resp.text[:200]}")
        except Exception as e:
            print(f"  ERROR: {e}")

    # -------------------------------------------------------------------------
    # 9. GET /api/albums
    # -------------------------------------------------------------------------
    section("9. GET /api/albums")
    print_album_id = None
    try:
        resp = client.get("/api/albums")
        print(f"  GET /api/albums → {resp.status_code}")
        if resp.status_code == 200:
            albums = resp.json()
            print(f"  Found {len(albums)} albums")
            if albums:
                print(f"  First album keys: {list(albums[0].keys())}")
                for album in albums[:5]:
                    name = album.get("albumName") or album.get("name") or "(no name key found)"
                    print(f"    id={album['id']!r}  name={name!r}")
                    if "print" in name.lower():
                        print_album_id = album["id"]
                        print(f"    ^ This looks like the Print album!")
            # Check which name key exists
            if albums:
                for key in ["albumName", "name", "title"]:
                    if key in albums[0]:
                        print(f"\n  ✓ Album name key is: '{key}'")
                        break
    except Exception as e:
        print(f"  ERROR: {e}")

    # -------------------------------------------------------------------------
    # 10. PUT /api/albums/{id}/assets
    # -------------------------------------------------------------------------
    section("10. PUT /api/albums/{id}/assets")
    if print_album_id and sample_asset_id:
        for body_format, body in [
            ('{"ids": [...]}', {"ids": [sample_asset_id]}),
            ('{"assetIds": [...]}', {"assetIds": [sample_asset_id]}),
        ]:
            try:
                resp = client.put(f"/api/albums/{print_album_id}/assets", json=body)
                print(f"  PUT /api/albums/{print_album_id}/assets with {body_format}")
                print(f"  Status: {resp.status_code}")
                if resp.status_code in (200, 201, 204):
                    print(f"  ✓ Asset added to album with format: {body_format}")
                    if resp.content:
                        print(f"  Body: {resp.text[:200]}")
                    break
                else:
                    print(f"  ✗ Failed: {resp.text[:200]}")
            except Exception as e:
                print(f"  ERROR: {e}")
    else:
        print("  Skipped — no Print album found or no sample asset")
        print("  (Create a 'Print' album in Immich first)")

    # -------------------------------------------------------------------------
    # 11. PUT /api/assets — bulk isFavorite update
    # -------------------------------------------------------------------------
    section("11. PUT /api/assets (bulk isFavorite update)")
    if sample_asset_id:
        # Get current favorite status first
        current_fav = sample_asset.get("isFavorite", False) if sample_asset else False
        print(f"  Current isFavorite: {current_fav} (will toggle and restore)")

        # Try different request body formats
        for body_format, body in [
            ('{"ids": [...], "isFavorite": bool}', {"ids": [sample_asset_id], "isFavorite": current_fav}),
            ('{"assetIds": [...], "isFavorite": bool}', {"assetIds": [sample_asset_id], "isFavorite": current_fav}),
        ]:
            try:
                resp = client.put("/api/assets", json=body)
                print(f"  PUT /api/assets with {body_format}")
                print(f"  Status: {resp.status_code}")
                if resp.status_code in (200, 201, 204):
                    print(f"  ✓ Works with format: {body_format}")
                    if resp.content:
                        print(f"  Body: {resp.text[:300]}")
                    break
                else:
                    print(f"  ✗ Failed: {resp.text[:200]}")
            except Exception as e:
                print(f"  ERROR: {e}")
    else:
        print("  Skipped — no sample asset ID")

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("SUMMARY — paste into CLAUDE.md 'Validated Immich API Endpoints'")
    print("=" * 60)
    print(f"""
  Auth header:    {working_auth}
  Immich URL:     {IMMICH_URL}
  Sample asset:   {sample_asset_id}
  Print album ID: {print_album_id}

Next steps:
  - Note which body format worked for PUT /api/tags/{{id}}/assets
  - Note which body format worked for DELETE /api/tags/{{id}}/assets
  - Note album name key (albumName vs name)
  - Record all findings in CLAUDE.md
""")

    print("Done.")


if __name__ == "__main__":
    main()
