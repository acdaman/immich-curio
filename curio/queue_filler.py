"""Queue filler — submits Gemini batch scoring jobs and applies results."""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from curio.config import get_config
from curio.db import (
    get_active_batch_job_ids,
    get_batch_assets,
    get_burst_peers,
    get_pending_batch_count,
    get_queue_depth,
    get_unscored_asset_ids,
    has_pending_sent_photo,
)
from curio.gemini import (
    SUCCESS_STATE,
    get_batch_result,
    is_terminal,
    parse_batch_result,
    poll_batch_job,
    submit_group_batch_job,
    submit_single_batch_job,
)
from curio.immich import apply_tag, delete_tag, get_thumbnail, remove_tag

logger = logging.getLogger(__name__)

_FULL_QUEUE_SLEEP = 60   # seconds to wait when effective queue is at target
_NO_ASSETS_SLEEP = 300   # seconds to wait when there are no unscored assets left


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
        best = max(
            dupes,
            key=lambda x: (
                x[1],                        # is_favorite first
                (x[2] or 0) * (x[3] or 0),  # then resolution
            ),
        )
        kept.append(best)
        discarded.extend(item[0] for item in dupes if item[0] != best[0])

    return kept, discarded


async def _apply_batch_scores(job_id: str, asset_ids: list[str], result_text: str) -> None:
    """Parse batch result and apply score tags; remove the batch tag from all assets."""
    batch_tag = f"print/scored/batch/{job_id}"
    parsed = parse_batch_result(result_text, asset_ids)

    if parsed is None:
        logger.warning("Cannot parse batch result for job %s — releasing assets", job_id)
        await _release_batch_assets(job_id, asset_ids, "parse failure")
        return

    for asset_id in asset_ids:
        entry = parsed.get(asset_id, {})
        score_val = entry.get("score", "no")
        reason = entry.get("reason", "")
        if len(asset_ids) == 1:
            tag_path = f"print/scored/{score_val}"
        else:
            tag_path = "print/scored/yes" if score_val == "yes" else "print/scored/no/group"
        await apply_tag(asset_id, tag_path)
        await remove_tag(asset_id, batch_tag)
        logger.info("Batch-scored %s → %s (%s)", asset_id, tag_path, reason)
    try:
        await delete_tag(batch_tag)
    except Exception as e:
        logger.warning("Failed to delete batch tag %s: %s", batch_tag, e)


async def _release_batch_assets(job_id: str, asset_ids: list[str], reason: str) -> None:
    """Remove batch tags so assets return to the unscored pool."""
    batch_tag = f"print/scored/batch/{job_id}"
    logger.warning("Releasing %d assets from batch %s: %s", len(asset_ids), job_id, reason)
    for asset_id in asset_ids:
        try:
            await remove_tag(asset_id, batch_tag)
        except Exception as e:
            logger.error("Failed to remove batch tag from %s: %s", asset_id, e)
    try:
        await delete_tag(batch_tag)
    except Exception as e:
        logger.warning("Failed to delete batch tag %s: %s", batch_tag, e)


async def _process_completed_batches(
    on_queue_populated: Callable[[], Coroutine[Any, Any, None]] | None = None,
) -> None:
    """Poll all active batch jobs and apply results or release failed assets."""
    try:
        job_ids = get_active_batch_job_ids()
    except Exception as e:
        logger.error("Failed to get active batch job IDs: %s", e)
        return

    if not job_ids:
        return

    depth_before = get_queue_depth()

    for job_id in job_ids:
        try:
            state = await asyncio.to_thread(poll_batch_job, job_id)
        except Exception as e:
            logger.warning("Failed to poll batch job %s: %s", job_id, e)
            continue

        if not is_terminal(state):
            logger.debug("Batch job %s still running (%s)", job_id, state)
            continue

        asset_ids = get_batch_assets(job_id)
        if not asset_ids:
            logger.warning("No assets found for completed batch job %s", job_id)
            continue

        if state == SUCCESS_STATE:
            try:
                result_text = await asyncio.to_thread(get_batch_result, job_id)
                if result_text:
                    await _apply_batch_scores(job_id, asset_ids, result_text)
                else:
                    await _release_batch_assets(job_id, asset_ids, "empty response")
            except Exception as e:
                logger.error("Error processing batch %s results: %s", job_id, e)
                await _release_batch_assets(job_id, asset_ids, str(e))
        else:
            await _release_batch_assets(job_id, asset_ids, f"terminal state: {state}")

    if on_queue_populated and depth_before == 0:
        depth_after = get_queue_depth()
        if depth_after > 0 and not has_pending_sent_photo():
            logger.info("Queue was empty and now has photos — triggering send")
            await on_queue_populated()


async def _submit_burst_group(
    seed_id: str,
    seed_is_favorite: bool,
    processed: set[str],
) -> None:
    """Find burst peers for seed_id, handle deduplication and favourites, submit a batch job.

    Updates `processed` in-place with every asset ID handled.
    """
    cfg = get_config()

    try:
        group = get_burst_peers(seed_id, window_seconds=cfg.burst_window_seconds)
    except Exception as e:
        logger.warning("get_burst_peers failed for %s: %s — single photo", seed_id, e)
        group = []

    if not group:
        # No EXIF data: treat as a single standalone photo
        processed.add(seed_id)
        if seed_is_favorite:
            try:
                await apply_tag(seed_id, "print/scored/yes/auto")
                logger.info("Auto-scored %s → yes/auto (favorite)", seed_id)
            except Exception as e:
                logger.error("Failed to apply yes/auto tag to %s: %s", seed_id, e)
            return
        try:
            image_bytes = await get_thumbnail(seed_id)
            job_id = await submit_single_batch_job(image_bytes, seed_id)
            await apply_tag(seed_id, f"print/scored/batch/{job_id}")
        except Exception as e:
            logger.warning("Failed to submit batch for %s: %s — skipping", seed_id, e)
        return

    # Mark all peers as handled before any async work
    processed.update(item[0] for item in group)

    # Deduplicate Immich-flagged duplicates within the burst
    group, dup_discarded = _deduplicate_group(group)
    for asset_id in dup_discarded:
        try:
            await apply_tag(asset_id, "print/scored/no/duplicate")
            logger.info("Discarded duplicate %s → no/duplicate", asset_id)
        except Exception as e:
            logger.error("Failed to tag duplicate %s: %s", asset_id, e)

    # Cap group size — seed + earliest N-1 others (chronological order from DB)
    max_size = cfg.burst_group_max_size
    if len(group) > max_size:
        seed_entry = next((g for g in group if g[0] == seed_id), group[0])
        others = [g for g in group if g[0] != seed_id]
        group = [seed_entry] + others[:max_size - 1]
        logger.info("Capped burst group to %d for seed %s", max_size, seed_id)

    logger.info("Processing burst group of %d (seed: %s)", len(group), seed_id)

    # Auto-tag any favourites immediately; still pass full group to Gemini
    for asset_id, is_fav, *_ in group:
        if is_fav:
            try:
                await apply_tag(asset_id, "print/scored/yes/auto")
                logger.info("Auto-tagged %s → yes/auto (favourite in group)", asset_id)
            except Exception as e:
                logger.error("Failed to tag %s → yes/auto: %s", asset_id, e)

    # Single photo after deduplication
    if len(group) == 1:
        asset_id, is_fav, *_ = group[0]
        if not is_fav:
            try:
                image_bytes = await get_thumbnail(asset_id)
                job_id = await submit_single_batch_job(image_bytes, asset_id)
                await apply_tag(asset_id, f"print/scored/batch/{job_id}")
            except Exception as e:
                logger.warning("Failed to submit batch for %s: %s — skipping", asset_id, e)
        return

    # Multi-photo group: fetch thumbnails, submit group batch job
    thumbnails: list[tuple[bytes, str, bool]] = []
    for asset_id, is_fav, *_ in group:
        try:
            image_bytes = await get_thumbnail(asset_id)
            thumbnails.append((image_bytes, asset_id, is_fav))
        except Exception as e:
            logger.warning("Failed thumbnail for %s in burst: %s — excluding", asset_id, e)

    if not thumbnails:
        logger.warning("All thumbnails failed for burst (seed %s) — skipping group", seed_id)
        return

    if len(thumbnails) == 1:
        # Reduced to one photo after thumbnail failures
        asset_id, is_fav = thumbnails[0][1], thumbnails[0][2]
        for other_id, *_ in group:
            if other_id != asset_id:
                try:
                    await apply_tag(other_id, "print/scored/no/group")
                except Exception:
                    pass
        if not is_fav:
            try:
                job_id = await submit_single_batch_job(thumbnails[0][0], asset_id)
                await apply_tag(asset_id, f"print/scored/batch/{job_id}")
            except Exception as e:
                logger.warning("Failed to submit batch for %s: %s — skipping", asset_id, e)
        return

    try:
        job_id = await submit_group_batch_job(thumbnails)
        for _, asset_id, _ in thumbnails:
            await apply_tag(asset_id, f"print/scored/batch/{job_id}")
        logger.info("Submitted group batch job %s for %d photos", job_id, len(thumbnails))
    except Exception as e:
        logger.warning("Failed to submit group batch (seed %s): %s — skipping", seed_id, e)


async def queue_filler_loop(
    on_queue_populated: Callable[[], Coroutine[Any, Any, None]] | None = None,
) -> None:
    logger.info("Queue filler started")
    cfg = get_config()

    while True:
        try:
            # Always check for completed batch jobs first (also handles restart recovery)
            await _process_completed_batches(on_queue_populated)

            queue_depth = get_queue_depth()
            pending_batch = get_pending_batch_count()
            effective_depth = queue_depth + pending_batch
            logger.debug(
                "Queue depth: %d scored + %d pending batch = %d effective / %d target",
                queue_depth, pending_batch, effective_depth, cfg.queue_target_size,
            )

            if effective_depth >= cfg.queue_target_size:
                await asyncio.sleep(_FULL_QUEUE_SLEEP)
                continue

            if effective_depth > cfg.queue_batch_trigger:
                # Healthy but below target — existing batches will fill the gap
                await asyncio.sleep(_FULL_QUEUE_SLEEP)
                continue

            # Effective depth at or below trigger: submit more batch jobs
            needed = cfg.queue_target_size - effective_depth
            asset_ids = get_unscored_asset_ids(needed)

            if not asset_ids:
                logger.info("No unscored assets available — sleeping %ds", _NO_ASSETS_SLEEP)
                await asyncio.sleep(_NO_ASSETS_SLEEP)
                continue

            logger.info(
                "Effective queue at %d (trigger: %d) — submitting batch jobs for %d candidates",
                effective_depth, cfg.queue_batch_trigger, len(asset_ids),
            )
            processed: set[str] = set()

            for seed_id, is_favorite in asset_ids:
                if seed_id in processed:
                    continue
                await _submit_burst_group(seed_id, is_favorite, processed)

        except Exception as e:
            logger.error("Queue filler error: %s — retrying in 60s", e)
            await asyncio.sleep(60)
