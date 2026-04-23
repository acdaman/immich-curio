"""Queue filler coroutine — scores unscored photos and maintains the candidate pool."""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from curio.config import get_config
from curio.db import get_burst_peers, get_queue_depth, get_unscored_asset_ids, has_pending_sent_photo
from curio.gemini import score_photo, score_photo_group
from curio.immich import apply_tag, get_thumbnail

logger = logging.getLogger(__name__)

_FULL_QUEUE_SLEEP = 60   # seconds to wait when queue is already at target
_NO_ASSETS_SLEEP = 300   # seconds to wait when there are no unscored assets left


async def _score_and_tag(asset_id: str, is_favorite: bool = False) -> None:
    if is_favorite:
        try:
            await apply_tag(asset_id, "print/scored/yes")
            logger.info("Auto-scored %s → yes (favorite)", asset_id)
        except Exception as e:
            logger.error("Failed to apply tag print/scored/yes to %s: %s", asset_id, e)
        return

    try:
        image_bytes = await get_thumbnail(asset_id)
    except Exception as e:
        logger.warning("Failed to fetch thumbnail for %s: %s", asset_id, e)
        return

    result = await score_photo(image_bytes, asset_id)  # includes 13s rate-limit sleep
    if result is None:
        logger.warning("Gemini scoring failed for %s — skipping", asset_id)
        return

    score = result["score"]
    reason = result["reason"]
    tag_path = f"print/scored/{score}"

    try:
        await apply_tag(asset_id, tag_path)
        logger.info("Scored %s → %s (%s)", asset_id, score, reason)
    except Exception as e:
        logger.error("Failed to apply tag %s to %s: %s", tag_path, asset_id, e)


def _deduplicate_group(
    group: list[tuple[str, bool, int | None, int | None, str | None]],
) -> tuple[
    list[tuple[str, bool, int | None, int | None, str | None]],
    list[str],
]:
    """Within the group, keep only the highest-resolution asset per duplicateId.

    Returns (kept, discarded_ids). Non-duplicate assets (duplicateId=None) are always kept.
    """
    by_dup: dict[str, list[tuple[str, bool, int | None, int | None, str | None]]] = {}
    no_dup: list[tuple[str, bool, int | None, int | None, str | None]] = []

    for item in group:
        asset_id, is_fav, w, h, dup_id = item
        if dup_id:
            by_dup.setdefault(dup_id, []).append(item)
        else:
            no_dup.append(item)

    kept: list[tuple[str, bool, int | None, int | None, str | None]] = list(no_dup)
    discarded: list[str] = []

    for dup_id, dupes in by_dup.items():
        # Keep highest resolution; break ties by keeping favorite; then first seen
        best = max(
            dupes,
            key=lambda x: (
                x[1],               # is_favorite first
                (x[2] or 0) * (x[3] or 0),  # then resolution
            ),
        )
        kept.append(best)
        discarded.extend(item[0] for item in dupes if item[0] != best[0])

    return kept, discarded


async def _process_burst_group(
    seed_id: str,
    seed_is_favorite: bool,
    processed: set[str],
) -> None:
    """Find all burst peers of seed_id, score the group, apply tags.

    Updates `processed` in-place with every asset ID handled.
    """
    cfg = get_config()

    try:
        group = get_burst_peers(seed_id, window_seconds=cfg.burst_window_seconds)
    except Exception as e:
        logger.warning("get_burst_peers failed for %s: %s — falling back to single", seed_id, e)
        group = []

    # No EXIF → fall back to single-photo scoring
    if not group:
        logger.debug("No burst peers for %s — single-photo scoring", seed_id)
        await _score_and_tag(seed_id, seed_is_favorite)
        processed.add(seed_id)
        return

    # Mark all peers as processed before any async work
    processed.update(item[0] for item in group)

    # Deduplicate within the group (Immich-flagged duplicates)
    group, dup_discarded = _deduplicate_group(group)
    for asset_id in dup_discarded:
        try:
            await apply_tag(asset_id, "print/scored/no/duplicate")
            logger.info("Discarded duplicate %s → no/duplicate", asset_id)
        except Exception as e:
            logger.error("Failed to tag duplicate %s: %s", asset_id, e)

    # Cap group size — keep seed + earliest N-1 others (chronological order from DB)
    max_size = cfg.burst_group_max_size
    if len(group) > max_size:
        logger.info("Burst group for %s has %d members — capping to %d", seed_id, len(group), max_size)
        seed_entry = next((g for g in group if g[0] == seed_id), group[0])
        others = [g for g in group if g[0] != seed_id]
        group = [seed_entry] + others[: max_size - 1]

    logger.info("Processing burst group of %d (seed: %s)", len(group), seed_id)

    # Case: any member is a favorite → auto-tag, no Gemini
    if any(is_fav for _, is_fav, *_ in group):
        logger.info("Burst group has favorite(s) — auto-tagging (yes/no/group)")
        for asset_id, is_fav, *_ in group:
            tag = "print/scored/yes" if is_fav else "print/scored/no/group"
            try:
                await apply_tag(asset_id, tag)
                logger.info("Auto-tagged %s → %s (burst favorite rule)", asset_id, tag)
            except Exception as e:
                logger.error("Failed to tag %s → %s: %s", asset_id, tag, e)
        return

    # Case: single photo after deduplication
    if len(group) == 1:
        asset_id, is_fav, *_ = group[0]
        await _score_and_tag(asset_id, is_fav)
        return

    # Case: multi-photo group → batch Gemini review
    thumbnails: list[tuple[bytes, str]] = []
    for asset_id, _, *_ in group:
        try:
            image_bytes = await get_thumbnail(asset_id)
            thumbnails.append((image_bytes, asset_id))
        except Exception as e:
            logger.warning("Failed thumbnail for %s in burst: %s — excluding", asset_id, e)

    if not thumbnails:
        logger.warning("All thumbnails failed for burst (seed %s) — skipping group", seed_id)
        return

    if len(thumbnails) == 1:
        single_id = thumbnails[0][1]
        single_is_fav = next(is_fav for aid, is_fav, *_ in group if aid == single_id)
        await _score_and_tag(single_id, single_is_fav)
        for asset_id, *_ in group:
            if asset_id != single_id:
                try:
                    await apply_tag(asset_id, "print/scored/no/group")
                except Exception:
                    pass
        return

    scores = await score_photo_group(thumbnails)  # includes 13s rate-limit sleep

    if scores is None:
        logger.warning("Group scoring failed for burst (seed %s) — tagging all no", seed_id)
        for asset_id, *_ in group:
            try:
                await apply_tag(asset_id, "print/scored/no")
            except Exception as e:
                logger.error("Failed to tag %s no after group failure: %s", asset_id, e)
        return

    scored_ids = set(scores.keys())
    for asset_id, *_ in group:
        if asset_id in scored_ids:
            score_val = scores[asset_id]["score"]
            reason = scores[asset_id].get("reason", "")
            tag_path = "print/scored/yes" if score_val == "yes" else "print/scored/no/group"
        else:
            logger.warning("Asset %s missing from group scores — tagging no/group", asset_id)
            tag_path = "print/scored/no/group"
            reason = "missing from group response"
        try:
            await apply_tag(asset_id, tag_path)
            logger.info("Group-scored %s → %s (%s)", asset_id, tag_path, reason)
        except Exception as e:
            logger.error("Failed to tag %s → %s: %s", asset_id, tag_path, e)


async def queue_filler_loop(
    on_queue_populated: Callable[[], Coroutine[Any, Any, None]] | None = None,
) -> None:
    logger.info("Queue filler started")
    cfg = get_config()

    while True:
        try:
            depth = get_queue_depth()
            logger.debug("Queue depth: %d / %d", depth, cfg.queue_target_size)

            if depth >= cfg.queue_target_size:
                await asyncio.sleep(_FULL_QUEUE_SLEEP)
                continue

            was_empty = depth == 0
            asset_ids = get_unscored_asset_ids(cfg.queue_target_size)

            if not asset_ids:
                logger.info("No unscored assets available — sleeping %ds", _NO_ASSETS_SLEEP)
                await asyncio.sleep(_NO_ASSETS_SLEEP)
                continue

            logger.info("Filling queue (%d/%d) — %d candidate seeds", depth, cfg.queue_target_size, len(asset_ids))
            processed: set[str] = set()

            for seed_id, is_favorite in asset_ids:
                if seed_id in processed:
                    continue  # already handled as burst peer of an earlier seed

                await _process_burst_group(seed_id, is_favorite, processed)

                # Re-check after each group — a burst group might fill the queue on its own
                if get_queue_depth() >= cfg.queue_target_size:
                    logger.debug("Queue full after group — stopping inner loop")
                    break

            if was_empty and on_queue_populated and not has_pending_sent_photo():
                logger.info("Queue was empty and now has photos — triggering send")
                await on_queue_populated()

        except Exception as e:
            logger.error("Queue filler error: %s — retrying in 60s", e)
            await asyncio.sleep(60)
