"""Entry point — runs queue filler and Telegram bot on a single event loop."""

import asyncio
import logging
import signal

from curio.bot_responder import build_application, send_next_photo
from curio.config import get_config
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
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("Bot polling started")

        # Send the first photo immediately so the queue isn't silent at startup
        sent = await send_next_photo(app.bot, cfg.telegram_chat_id)
        if not sent:
            logger.info("No queued photos at startup — queue filler will populate")

        await queue_filler_loop()  # runs forever; PTB tasks run concurrently

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
