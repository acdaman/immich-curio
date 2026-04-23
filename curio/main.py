"""Entry point — runs queue filler and Telegram bot on a single event loop."""

import asyncio
import logging
import signal

from curio.bot_responder import build_application, send_next_photo
from curio.config import get_config
from curio.db import get_orphaned_sent_asset_ids
from curio.immich import remove_tag
from curio.queue_filler import queue_filler_loop
from curio.schema_validator import validate_schema

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    cfg = get_config()
    logging.basicConfig(
        level=cfg.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def run() -> None:
    _configure_logging()
    logger.info("Curio starting — validating schema")
    validate_schema()
    logger.info("Schema OK")

    cfg = get_config()
    app = build_application()

    async with app:
        await app.start()
        from telegram import Update
        await app.updater.start_polling(drop_pending_updates=True, allowed_updates=list(Update.ALL_TYPES))
        logger.info("Bot polling started")

        # Recover any photos sent before a previous restart but never decided on
        orphans = get_orphaned_sent_asset_ids()
        if orphans:
            logger.info("Recovering %d orphaned photo(s) — removing print/telegram/sent", len(orphans))
            for asset_id in orphans:
                await remove_tag(asset_id, "print/telegram/sent")

        # Send the first photo immediately so the queue isn't silent at startup
        sent = await send_next_photo(app.bot, cfg.telegram_chat_id)
        if not sent:
            logger.info("No queued photos at startup — queue filler will populate")

        async def _notify_queue_populated() -> None:
            await send_next_photo(app.bot, cfg.telegram_chat_id)

        await queue_filler_loop(on_queue_populated=_notify_queue_populated)

        # Reached only on clean shutdown
        await app.updater.stop()
        await app.stop()


def main() -> None:
    loop = asyncio.new_event_loop()

    def _handle_signal():
        logger.info("Shutdown signal received")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    try:
        loop.run_until_complete(run())
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()
        logger.info("Curio stopped")


if __name__ == "__main__":
    main()
