"""Telegram bot responder — delivers photos for review and processes approve/reject callbacks."""

import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, ContextTypes

from curio.config import get_config
from curio.db import get_next_queued_asset_id
from curio.immich import add_to_print_album, apply_tag, get_thumbnail, mark_favorite

logger = logging.getLogger(__name__)


async def send_next_photo(bot: Bot, chat_id: str) -> bool:
    """Send the next queued photo to Telegram. Returns True if a photo was sent."""
    asset_id = get_next_queued_asset_id()
    if not asset_id:
        await bot.send_message(chat_id=chat_id, text="Queue is empty — waiting for more scored photos.")
        logger.info("Queue empty, no photo to send")
        return False

    try:
        image_bytes = await get_thumbnail(asset_id)
    except Exception as e:
        logger.error("Failed to fetch thumbnail for %s: %s", asset_id, e)
        await bot.send_message(chat_id=chat_id, text=f"Error fetching photo {asset_id} — skipping.")
        await apply_tag(asset_id, "print/telegram/sent")  # prevent retry loop
        return False

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✓ Approve", callback_data=f"approve:{asset_id}"),
            InlineKeyboardButton("★ Like", callback_data=f"like:{asset_id}"),
            InlineKeyboardButton("✗ Reject", callback_data=f"reject:{asset_id}"),
        ]
    ])

    await bot.send_photo(chat_id=chat_id, photo=image_bytes, reply_markup=keyboard)
    await apply_tag(asset_id, "print/telegram/sent")
    logger.info("Sent %s for review", asset_id)
    return True


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()  # must respond quickly — Telegram invalidates after ~5 min

    cfg = get_config()
    action, asset_id = query.data.split(":", 1)

    logger.info("Callback: action=%s asset=%s", action, asset_id)

    try:
        if action == "approve":
            await apply_tag(asset_id, "print/queued")
            await add_to_print_album(asset_id)
            await query.edit_message_caption(caption="✓ Approved")

        elif action == "like":
            await apply_tag(asset_id, "print/queued")
            await add_to_print_album(asset_id)
            await mark_favorite(asset_id)
            await query.edit_message_caption(caption="★ Liked + Approved")

        elif action == "reject":
            await apply_tag(asset_id, "print/rejected")
            await query.edit_message_caption(caption="✗ Rejected")

        else:
            logger.warning("Unknown action %r for asset %s", action, asset_id)
            return

    except Exception as e:
        logger.error("Error handling %s for %s: %s", action, asset_id, e)
        await context.bot.send_message(chat_id=cfg.telegram_chat_id, text=f"Error processing {action}: {e}")

    await send_next_photo(context.bot, cfg.telegram_chat_id)


def build_application() -> Application:
    cfg = get_config()
    app = ApplicationBuilder().token(cfg.telegram_bot_token).build()
    app.add_handler(CallbackQueryHandler(handle_callback))
    return app
