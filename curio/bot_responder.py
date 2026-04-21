"""Telegram bot responder — delivers photos for review and processes approve/reject callbacks."""

import logging
from datetime import datetime, timezone

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, ContextTypes

from curio.config import get_config
from curio.db import get_next_queued_asset_id
from curio.immich import add_to_print_album, apply_tag, get_asset_info, get_thumbnail, mark_favorite

logger = logging.getLogger(__name__)


def _build_caption(asset_info: dict) -> str:
    parts = []

    local_dt_str = asset_info.get("localDateTime") or asset_info.get("fileCreatedAt", "")
    if local_dt_str:
        try:
            dt = datetime.fromisoformat(local_dt_str.replace("Z", "+00:00"))
            parts.append(dt.strftime("%-d %B %Y"))
        except ValueError:
            parts.append(local_dt_str[:10])

    cfg = get_config()
    if cfg.immich_public_url:
        asset_id = asset_info["id"]
        url = f"{cfg.immich_public_url.rstrip('/')}/photos/{asset_id}"
        parts.append(url)

    return "\n".join(parts)


async def send_next_photo(bot: Bot, chat_id: str) -> bool:
    """Send the next queued photo to Telegram. Returns True if a photo was sent."""
    asset_id = get_next_queued_asset_id()
    if not asset_id:
        await bot.send_message(chat_id=chat_id, text="Queue is empty — waiting for more scored photos.")
        logger.info("Queue empty, no photo to send")
        return False

    try:
        image_bytes, asset_info = await _fetch_photo_and_info(asset_id)
    except Exception as e:
        logger.error("Failed to fetch photo %s: %s", asset_id, e)
        await bot.send_message(chat_id=chat_id, text=f"Error fetching photo {asset_id} — skipping.")
        await apply_tag(asset_id, "print/telegram/sent")  # prevent retry loop
        return False

    buttons = [InlineKeyboardButton("✓ Approve", callback_data=f"approve:{asset_id}")]
    if not asset_info.get("isFavorite"):
        buttons.append(InlineKeyboardButton("★ Like", callback_data=f"like:{asset_id}"))
    buttons.append(InlineKeyboardButton("✗ Reject", callback_data=f"reject:{asset_id}"))
    keyboard = InlineKeyboardMarkup([buttons])

    caption = _build_caption(asset_info)
    await bot.send_photo(chat_id=chat_id, photo=image_bytes, caption=caption or None, reply_markup=keyboard)
    await apply_tag(asset_id, "print/telegram/sent")
    logger.info("Sent %s for review", asset_id)
    return True


async def _fetch_photo_and_info(asset_id: str) -> tuple[bytes, dict]:
    import asyncio
    image_bytes, asset_info = await asyncio.gather(
        get_thumbnail(asset_id),
        get_asset_info(asset_id),
    )
    return image_bytes, asset_info


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
