"""Queue filler coroutine — scores unscored photos and maintains the candidate pool."""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from curio.config import get_config
from curio.db import get_queue_depth, get_unscored_asset_ids, has_pending_sent_photo
from curio.gemini import score_photo
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
            needed = cfg.queue_target_size - depth
            asset_ids = get_unscored_asset_ids(needed)

            if not asset_ids:
                logger.info("No unscored assets available — sleeping %ds", _NO_ASSETS_SLEEP)
                await asyncio.sleep(_NO_ASSETS_SLEEP)
                continue

            logger.info("Scoring %d asset(s) to fill queue (%d/%d)", len(asset_ids), depth, cfg.queue_target_size)
            for asset_id, is_favorite in asset_ids:
                await _score_and_tag(asset_id, is_favorite)

            if was_empty and on_queue_populated and not has_pending_sent_photo():
                logger.info("Queue was empty and now has photos — triggering send")
                await on_queue_populated()

        except Exception as e:
            logger.error("Queue filler error: %s — retrying in 60s", e)
            await asyncio.sleep(60)
