#!/usr/bin/env python3
"""
Test the burst group scoring workflow end-to-end with DRY_RUN=true.

Finds real unscored multi-photo groups, shows what Gemini decides,
and prints what tags would be applied — without writing anything.

Usage:
    cd /home/adam/projects/curio
    source .venv/bin/activate
    DB_HOST=localhost IMMICH_URL=http://localhost:2283 python explore/06_test_group_scoring.py
    DB_HOST=localhost IMMICH_URL=http://localhost:2283 python explore/06_test_group_scoring.py --groups 3
    DB_HOST=localhost IMMICH_URL=http://localhost:2283 python explore/06_test_group_scoring.py --min-size 4
"""

import asyncio
import argparse
import logging
import os
import sys

# Add project root to path so curio package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Force dry run before any config is loaded
os.environ["DRY_RUN"] = "true"

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
)
# Suppress httpx noise
logging.getLogger("httpx").setLevel(logging.WARNING)

from curio.db import get_unscored_asset_ids, get_burst_peers
from curio.gemini import score_photo_group
from curio.immich import get_thumbnail


async def find_test_groups(min_size: int, max_candidates: int = 100) -> list[tuple[str, list]]:
    """Return (seed_id, group) pairs for non-fav groups with >= min_size members."""
    assets = get_unscored_asset_ids(max_candidates)
    seen_ids: set[str] = set()
    results = []

    for seed_id, is_fav in assets:
        if seed_id in seen_ids:
            continue
        peers = get_burst_peers(seed_id)
        for p in peers:
            seen_ids.add(p[0])
        non_fav_group = [p for p in peers if not p[1]]  # exclude favorites from group scoring test
        if len(non_fav_group) >= min_size and not any(p[1] for p in peers):
            results.append((seed_id, peers))

    return results


async def test_group(seed_id: str, group: list, group_num: int) -> None:
    print(f"\n{'=' * 60}")
    print(f"GROUP {group_num}  (seed: {seed_id[:8]}...)")
    print(f"{'=' * 60}")
    print(f"Members: {len(group)}")
    for i, (asset_id, is_fav, w, h, dup_id) in enumerate(group, 1):
        seed_marker = " ← seed" if asset_id == seed_id else ""
        dup_marker = f" [dup:{dup_id[:8]}]" if dup_id else ""
        print(f"  {i}. {asset_id[:8]}... fav={is_fav} res={w}x{h}{dup_marker}{seed_marker}")

    print("\nFetching thumbnails...")
    thumbnails: list[tuple[bytes, str]] = []
    for asset_id, _, *_ in group:
        try:
            img = await get_thumbnail(asset_id)
            thumbnails.append((img, asset_id))
            print(f"  {asset_id[:8]}... {len(img)//1024}KB OK")
        except Exception as e:
            print(f"  {asset_id[:8]}... FAILED: {e}")

    if len(thumbnails) < 2:
        print(f"\nOnly {len(thumbnails)} thumbnail(s) — skipping Gemini group call")
        return

    print(f"\nCalling Gemini with {len(thumbnails)} photos...")
    scores = await score_photo_group(thumbnails)

    if scores is None:
        print("Gemini returned None (error)")
        return

    print("\nGemini scores:")
    yes_count = sum(1 for v in scores.values() if v["score"] == "yes")
    no_count  = sum(1 for v in scores.values() if v["score"] == "no")
    print(f"  yes={yes_count}  no={no_count}  (of {len(scores)} photos)")
    print()
    for asset_id, _, *_ in group:
        if asset_id in scores:
            s = scores[asset_id]
            marker = "✓ YES" if s["score"] == "yes" else "✗ no "
            print(f"  {marker}  {asset_id[:8]}...  {s['reason']}")
        else:
            print(f"  ? ???  {asset_id[:8]}...  (missing from response)")

    print(f"\n[DRY RUN] Would apply tags:")
    for asset_id, _, *_ in group:
        tag = f"print/scored/{scores[asset_id]['score']}" if asset_id in scores else "print/scored/no"
        print(f"  apply_tag({asset_id[:8]}..., {tag!r})")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--groups", type=int, default=2, help="Number of groups to test")
    parser.add_argument("--min-size", type=int, default=2, help="Minimum group size")
    args = parser.parse_args()

    print(f"Looking for {args.groups} non-fav burst groups with >= {args.min_size} members...")
    groups = await find_test_groups(min_size=args.min_size)

    if not groups:
        print(f"No non-fav groups of size >= {args.min_size} found in sample. Try --min-size 2.")
        sys.exit(1)

    print(f"Found {len(groups)} candidate group(s) — testing first {min(args.groups, len(groups))}")

    for i, (seed_id, group) in enumerate(groups[:args.groups], 1):
        await test_group(seed_id, group, i)

    print(f"\n{'=' * 60}")
    print("Done. No tags were written (DRY_RUN=true).")


if __name__ == "__main__":
    asyncio.run(main())
