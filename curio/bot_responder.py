"""Telegram bot responder — delivers photos for review and processes approve/reject callbacks."""

import logging
from datetime import datetime, timezone

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes

from curio.config import get_config
from curio.db import get_next_queued_asset_id, get_pending_batch_count, get_sample_approved_asset_ids, get_sample_rejected_asset_ids, get_stats
from curio.gemini import analyze_decisions
from curio.immich import add_to_print_album, apply_tag, get_asset_info, get_thumbnail, mark_favorite

logger = logging.getLogger(__name__)


def _format_date(asset_info: dict) -> str:
    local_dt_str = asset_info.get("localDateTime") or asset_info.get("fileCreatedAt", "")
    if not local_dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(local_dt_str.replace("Z", "+00:00"))
        return dt.strftime("%-d %B %Y")
    except ValueError:
        return local_dt_str[:10]


def _build_caption(asset_info: dict) -> str:
    parts = []

    date_str = _format_date(asset_info)
    if date_str:
        parts.append(date_str)

    cfg = get_config()
    if cfg.immich_public_url:
        asset_id = asset_info["id"]
        parts.append(f"{cfg.immich_public_url.rstrip('/')}/photos/{asset_id}")

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
        asset_info = await get_asset_info(asset_id)
        date_str = _format_date(asset_info)

        if action == "approve":
            await apply_tag(asset_id, "print/queued")
            await add_to_print_album(asset_id)
            await mark_favorite(asset_id)
            await query.edit_message_caption(caption=f"{date_str}\n✓ Approved" if date_str else "✓ Approved")

        elif action == "like":
            await mark_favorite(asset_id)
            await apply_tag(asset_id, "print/rejected")
            await apply_tag(asset_id, "print/liked")
            await query.edit_message_caption(caption=f"{date_str}\n★ Liked" if date_str else "★ Liked")

        elif action == "reject":
            await apply_tag(asset_id, "print/rejected")
            await query.edit_message_caption(caption=f"{date_str}\n✗ Rejected" if date_str else "✗ Rejected")

        else:
            logger.warning("Unknown action %r for asset %s", action, asset_id)
            return

    except Exception as e:
        logger.error("Error handling %s for %s: %s", action, asset_id, e)
        await context.bot.send_message(chat_id=cfg.telegram_chat_id, text=f"Error processing {action}: {e}")

    await send_next_photo(context.bot, cfg.telegram_chat_id)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = get_config()
    await send_next_photo(context.bot, cfg.telegram_chat_id)


def _pct(n: int, d: int) -> str:
    return f"{round(100 * n / d)}%" if d else "—"


async def handle_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Stats command received")
    cfg = get_config()
    try:
        s = get_stats()
        pending_batch = get_pending_batch_count()
    except Exception as e:
        logger.error("Stats query failed: %s", e)
        await context.bot.send_message(chat_id=cfg.telegram_chat_id, text=f"Stats failed: {e}")
        return

    body = "\n".join([
        "Library",
        f"  Total photos     {s['library_total']:>7,}",
        f"  Favourites       {s['library_favs']:>7,}  ({_pct(s['library_favs'], s['library_total'])})",
        "",
        "Pipeline",
        f"  Unscored backlog {s['unscored']:>7,}",
        f"  Batches pending  {pending_batch:>7,}",
        f"  Gemini yes       {s['gemini_yes']:>7,}  ({_pct(s['gemini_yes'], s['total_scored'])})",
        f"  Auto yes (fav)   {s['auto_yes']:>7,}  ({_pct(s['auto_yes'], s['total_scored'])})",
        f"  Gemini maybe     {s['gemini_maybe']:>7,}  ({_pct(s['gemini_maybe'], s['total_scored'])})",
        f"  Gemini no        {s['gemini_no_total']:>7,}  ({_pct(s['gemini_no_total'], s['total_scored'])})",
        f"    solo           {s['gemini_no']:>7,}",
        f"    group loser    {s['gemini_no_group']:>7,}",
        f"    duplicate      {s['gemini_no_dup']:>7,}",
        f"  Ready to review  {s['queue_depth']:>7,}",
        "",
        "Your decisions",
        f"  Total reviewed   {s['total_reviewed']:>7,}",
        f"    Approved       {s['approved']:>7,}  ({_pct(s['approved'], s['total_reviewed'])})",
        f"    Liked          {s['liked']:>7,}  ({_pct(s['liked'], s['total_reviewed'])})",
        f"    Rejected       {s['rejected']:>7,}  ({_pct(s['rejected'], s['total_reviewed'])})",
        f"  Library reviewed {_pct(s['total_reviewed'], s['library_total']):>7}",
        "",
        "Gemini calibration",
        f"  Yes -> approved  {_pct(s['yes_approved'], s['yes_approved'] + s['yes_rejected']):>7}",
        f"  Yes -> rejected  {_pct(s['yes_rejected'], s['yes_approved'] + s['yes_rejected']):>7}",
        f"  Maybe -> approved{_pct(s['maybe_approved'], s['maybe_approved'] + s['maybe_rejected']):>7}",
        f"  Maybe -> rejected{_pct(s['maybe_rejected'], s['maybe_approved'] + s['maybe_rejected']):>7}",
        "",
        "Fav calibration",
        f"  Auto -> approved {_pct(s['auto_approved'], s['auto_approved'] + s['auto_rejected']):>7}",
        f"  Auto -> rejected {_pct(s['auto_rejected'], s['auto_approved'] + s['auto_rejected']):>7}",
        "",
        "Liked breakdown",
        f"  From Gemini yes  {s['liked_from_yes']:>7,}  ({_pct(s['liked_from_yes'], s['liked'])})",
        f"  From auto (fav)  {s['liked_from_auto']:>7,}  ({_pct(s['liked_from_auto'], s['liked'])})",
        f"  From maybe       {s['liked_from_maybe']:>7,}  ({_pct(s['liked_from_maybe'], s['liked'])})",
    ])

    await context.bot.send_message(
        chat_id=cfg.telegram_chat_id,
        text=f"📊 Curio Stats\n\n```\n{body}\n```",
        parse_mode="Markdown",
    )


async def handle_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import asyncio
    logger.info("Feedback command received")
    cfg = get_config()
    chat_id = cfg.telegram_chat_id

    try:
        approved_ids = get_sample_approved_asset_ids(10)
        rejected_ids = get_sample_rejected_asset_ids(10)
    except Exception as e:
        logger.error("Feedback DB query failed: %s", e)
        await context.bot.send_message(chat_id=chat_id, text=f"Feedback failed (DB error): {e}")
        return

    logger.info("Feedback: %d approved, %d rejected available", len(approved_ids), len(rejected_ids))

    if len(approved_ids) < 3 or len(rejected_ids) < 3:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Not enough data yet — need at least 3 approved and 3 rejected photos "
                 f"(have {len(approved_ids)} approved, {len(rejected_ids)} rejected).",
        )
        return

    await context.bot.send_message(chat_id=chat_id, text="Analyzing your decisions…")

    try:
        approved_images, rejected_images = await asyncio.gather(
            asyncio.gather(*[get_thumbnail(aid) for aid in approved_ids]),
            asyncio.gather(*[get_thumbnail(rid) for rid in rejected_ids]),
        )
        analysis = await analyze_decisions(list(approved_images), list(rejected_images))
        header = f"\U0001f4ca Feedback ({len(approved_ids)} approved / {len(rejected_ids)} rejected sampled)\n\n"
        await context.bot.send_message(chat_id=chat_id, text=header + analysis)
    except Exception as e:
        logger.error("Feedback analysis failed: %s", e)
        await context.bot.send_message(chat_id=chat_id, text=f"Feedback analysis failed: {e}")


def build_application() -> Application:
    cfg = get_config()
    app = ApplicationBuilder().token(cfg.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("stats", handle_stats))
    app.add_handler(CommandHandler("feedback", handle_feedback))
    app.add_handler(CallbackQueryHandler(handle_callback))
    return app
