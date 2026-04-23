"""Exports approved photos to Syncthing folder for Mac pickup."""

import asyncio
import logging
from pathlib import Path

from curio.config import get_config
from curio.db import get_unexported_queued_asset_ids
from curio.immich import apply_tag, download_original

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 60


async def exporter_loop() -> None:
    cfg = get_config()
    export_dir = Path(cfg.export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)

    while True:
        try:
            asset_ids = get_unexported_queued_asset_ids()
            if asset_ids:
                logger.info("Exporting %d approved photo(s)", len(asset_ids))
            for asset_id in asset_ids:
                try:
                    file_bytes, raw_filename = await download_original(asset_id)
                    safe_name = raw_filename.replace(" ", "_")
                    dest = export_dir / f"{asset_id[:8]}_{safe_name}"
                    if cfg.dry_run:
                        logger.info("[DRY RUN] would write %s (%d KB)", dest.name, len(file_bytes) // 1024)
                    else:
                        dest.write_bytes(file_bytes)
                        logger.info("Exported %s → %s (%d KB)", asset_id[:8], dest.name, len(file_bytes) // 1024)
                    await apply_tag(asset_id, "print/exported")
                except Exception:
                    logger.exception("Failed to export %s — will retry next cycle", asset_id)
        except Exception:
            logger.exception("Exporter loop error")

        await asyncio.sleep(_POLL_INTERVAL)
