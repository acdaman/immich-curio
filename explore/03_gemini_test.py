#!/usr/bin/env python3
"""
Phase 0 exploration: Score a real Immich thumbnail with Gemini Flash 2.5.

Fetches N thumbnails from Immich, sends each to Gemini, and prints the scores.
Iterate on the prompt here before it gets baked into curio/gemini.py.

Usage:
    cd /path/to/curio
    source .venv/bin/activate
    pip install httpx google-genai python-dotenv
    python explore/03_gemini_test.py
    python explore/03_gemini_test.py --count 10   # score 10 photos
    python explore/03_gemini_test.py --favorites   # score only favorites
"""

import os
import sys
import json
import argparse
from dotenv import load_dotenv

load_dotenv()

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx")
    sys.exit(1)

try:
    from google import genai
    from google.genai import types
except ImportError:
    print("ERROR: google-genai not installed. Run: pip install google-genai")
    sys.exit(1)

IMMICH_URL = os.getenv("IMMICH_URL", "http://localhost:2283").rstrip("/")
IMMICH_API_KEY = os.getenv("IMMICH_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

if not IMMICH_API_KEY:
    print("ERROR: IMMICH_API_KEY not set in .env")
    sys.exit(1)
if not GEMINI_API_KEY:
    print("ERROR: GEMINI_API_KEY not set in .env")
    sys.exit(1)

SCORING_PROMPT = """You are evaluating a photo for physical printing on lustre paper for home wall display.
Score this photo strictly.

Respond with JSON only — no markdown, no explanation, just the JSON object:
{
  "score": "yes" | "maybe" | "no",
  "reason": "one sentence explanation"
}

Score "yes" if: sharp focus throughout, strong composition, meaningful or beautiful subject, clearly worth printing.
Score "maybe" if: decent photo with minor issues — slight blur, awkward crop, good subject but not exceptional.
Score "no" if: blurry, out of focus, duplicate/similar to many others, screenshot, document, text overlay, low light noise, not suitable for printing.

Target calibration: roughly 20% yes, 40% maybe, 40% no. Be strict — not every decent photo deserves to be printed."""


def get_immich_client():
    return httpx.Client(
        base_url=IMMICH_URL,
        headers={"x-api-key": IMMICH_API_KEY, "Accept": "application/json"},
        timeout=30,
    )


def get_sample_assets(client, count: int, favorites_only: bool) -> list[dict]:
    search_body = {
        "type": "IMAGE",
        "page": 1,
        "size": count,
    }
    if favorites_only:
        search_body["isFavorite"] = True

    resp = client.post("/api/search/metadata", json=search_body)
    if resp.status_code != 200:
        print(f"ERROR: Search failed: {resp.status_code} {resp.text[:200]}")
        return []

    body = resp.json()
    return body.get("assets", {}).get("items", [])


def get_thumbnail(client, asset_id: str) -> bytes | None:
    resp = client.get(f"/api/assets/{asset_id}/thumbnail", params={"size": "preview"})
    if resp.status_code == 200:
        return resp.content
    print(f"  WARNING: thumbnail fetch failed for {asset_id}: {resp.status_code}")
    return None


def score_photo(gemini_client, image_bytes: bytes, asset_id: str) -> dict | None:
    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                SCORING_PROMPT,
            ],
        )
        text = response.text.strip()

        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

        result = json.loads(text)
        assert result.get("score") in ("yes", "maybe", "no"), f"Invalid score: {result}"
        return result

    except json.JSONDecodeError as e:
        print(f"  WARNING: JSON parse failed for {asset_id}: {e}")
        print(f"  Raw response: {response.text[:300]}")
        # Retry with explicit JSON instruction
        try:
            retry_response = gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    SCORING_PROMPT + "\n\nIMPORTANT: Respond with ONLY valid JSON. No markdown. No explanation.",
                ],
            )
            text = retry_response.text.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
            result = json.loads(text)
            print(f"  Retry succeeded")
            return result
        except Exception as e2:
            print(f"  Retry also failed: {e2}")
            return None
    except Exception as e:
        print(f"  ERROR scoring {asset_id}: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Score Immich photos with Gemini")
    parser.add_argument("--count", type=int, default=5, help="Number of photos to score (default: 5)")
    parser.add_argument("--favorites", action="store_true", help="Score only favorites")
    args = parser.parse_args()

    print("=" * 60)
    print("Curio Phase 0 — Gemini Scoring Test")
    print("=" * 60)
    print(f"\nModel: {GEMINI_MODEL}")
    print(f"Scoring {args.count} photos{'(favorites only)' if args.favorites else ''}\n")

    immich_client = get_immich_client()
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)

    # Get sample assets
    print(f"Fetching {args.count} sample assets from Immich...")
    assets = get_sample_assets(immich_client, args.count, args.favorites)
    if not assets:
        print("ERROR: No assets found")
        sys.exit(1)
    print(f"Got {len(assets)} assets\n")

    # Score each
    results = {"yes": [], "maybe": [], "no": [], "error": []}

    for i, asset in enumerate(assets, 1):
        asset_id = asset["id"]
        is_fav = asset.get("isFavorite", False)
        fav_marker = "★" if is_fav else " "

        print(f"[{i:2d}/{len(assets)}] {fav_marker} {asset_id}")
        print(f"         Fetching thumbnail...", end=" ", flush=True)

        thumb = get_thumbnail(immich_client, asset_id)
        if not thumb:
            print("SKIP")
            results["error"].append(asset_id)
            continue

        print(f"{len(thumb):,} bytes — Scoring with Gemini...", end=" ", flush=True)

        result = score_photo(gemini_client, thumb, asset_id)
        if result:
            score = result["score"]
            reason = result["reason"]
            results[score].append(asset_id)
            score_symbol = {"yes": "✅", "maybe": "🤔", "no": "❌"}.get(score, "?")
            print(f"{score_symbol} {score.upper()}")
            print(f"         Reason: {reason}")
        else:
            print("ERROR")
            results["error"].append(asset_id)

        print()

    # Summary
    total = len(assets)
    print("=" * 60)
    print("SCORING SUMMARY")
    print("=" * 60)
    print(f"  Total scored:   {total}")
    print(f"  ✅ yes:         {len(results['yes']):3d}  ({len(results['yes'])/total*100:.0f}%)")
    print(f"  🤔 maybe:       {len(results['maybe']):3d}  ({len(results['maybe'])/total*100:.0f}%)")
    print(f"  ❌ no:          {len(results['no']):3d}  ({len(results['no'])/total*100:.0f}%)")
    print(f"  errors:         {len(results['error']):3d}")
    print(f"""
Target calibration: ~20% yes, ~40% maybe, ~40% no

Prompt used is in this file under SCORING_PROMPT.
If calibration is off, edit the prompt and re-run.
When happy, paste the final prompt into curio/gemini.py.
""")


if __name__ == "__main__":
    main()
