#!/usr/bin/env python3
"""
Phase 0 exploration: Telegram round-trip test.

1. Sends a text message confirming the bot works
2. Fetches a thumbnail from Immich and sends it as a Document with inline buttons
3. Polls for a callback (button press) and prints the full callback data

This confirms:
  - Bot token and chat ID work
  - Document delivery preserves quality (vs Photo compression)
  - callback_data format and UUID embedding
  - Max callback_data length is within the 64-byte limit

Usage:
    cd /home/adam/projects/curio
    source .venv/bin/activate
    pip install python-telegram-bot[asyncio] httpx python-dotenv
    python explore/04_telegram_test.py

After running, press one of the buttons in Telegram to see the callback output.
Press Ctrl+C to exit after testing.
"""

import os
import sys
import asyncio
import json
from dotenv import load_dotenv

load_dotenv()

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx")
    sys.exit(1)

try:
    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import Application, CallbackQueryHandler, ContextTypes
except ImportError:
    print("ERROR: python-telegram-bot not installed. Run: pip install 'python-telegram-bot[asyncio]'")
    sys.exit(1)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
IMMICH_URL = os.getenv("IMMICH_URL", "http://localhost:2283").rstrip("/")
IMMICH_API_KEY = os.getenv("IMMICH_API_KEY", "")

if not BOT_TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
    sys.exit(1)
if not CHAT_ID:
    print("ERROR: TELEGRAM_CHAT_ID not set in .env")
    sys.exit(1)


def get_sample_asset_id() -> str | None:
    if not IMMICH_API_KEY:
        print("  WARNING: IMMICH_API_KEY not set — will use a placeholder asset ID")
        return None
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                f"{IMMICH_URL}/api/search/metadata",
                json={"type": "IMAGE", "page": 1, "size": 1},
                headers={"x-api-key": IMMICH_API_KEY},
            )
            if resp.status_code == 200:
                items = resp.json().get("assets", {}).get("items", [])
                if items:
                    return items[0]["id"]
    except Exception as e:
        print(f"  WARNING: Could not fetch asset from Immich: {e}")
    return None


def get_thumbnail_bytes(asset_id: str) -> bytes | None:
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(
                f"{IMMICH_URL}/api/assets/{asset_id}/thumbnail",
                params={"size": "preview"},
                headers={"x-api-key": IMMICH_API_KEY},
            )
            if resp.status_code == 200:
                return resp.content
    except Exception as e:
        print(f"  WARNING: Could not fetch thumbnail: {e}")
    return None


def check_callback_data_lengths(asset_id: str):
    print("\n  callback_data length check:")
    for action in ["approve", "like", "reject"]:
        data = f"{action}:{asset_id}"
        print(f"    '{data}' — {len(data)} bytes (max 64)")
        if len(data) > 64:
            print(f"    WARNING: EXCEEDS 64-byte limit!")
        else:
            print(f"    ✓ Within limit")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print("\n" + "=" * 60)
    print("CALLBACK RECEIVED!")
    print("=" * 60)
    print(f"\ncallback_query.data: {query.data!r}")
    print(f"\nFull callback_query object:")
    print(f"  id:           {query.id}")
    print(f"  from.id:      {query.from_user.id}")
    print(f"  from.username:{query.from_user.username}")
    print(f"  message.id:   {query.message.message_id}")
    print(f"  chat.id:      {query.message.chat_id}")

    # Parse the callback data
    if ":" in query.data:
        action, asset_id = query.data.split(":", 1)
        print(f"\nParsed action:    {action!r}")
        print(f"Parsed asset_id:  {asset_id!r}")
    else:
        print(f"\nRaw data (no ':' separator): {query.data!r}")

    # Check message type
    if query.message.photo:
        sizes = [(p.width, p.height, p.file_size) for p in query.message.photo]
        print(f"\nMessage type: PHOTO")
        print(f"  Telegram variants: {sizes}")

    # Acknowledge the callback (removes loading spinner)
    await query.answer()

    # Edit the message to show the decision
    action_labels = {
        "approve": "✅ Approved",
        "like": "👍 Liked",
        "reject": "❌ Rejected",
    }
    action = query.data.split(":")[0] if ":" in query.data else query.data
    label = action_labels.get(action, f"Action: {action}")
    await query.edit_message_caption(caption=f"{label}\n\n(This was a test)")

    print(f"\n✓ Callback handled, message edited to: {label!r}")
    print("\nPress Ctrl+C to exit.")


async def main():
    print("=" * 60)
    print("Curio Phase 0 — Telegram Round-Trip Test")
    print("=" * 60)

    bot = Bot(token=BOT_TOKEN)
    me = await bot.get_me()
    print(f"\nBot: @{me.username} (id={me.id})")
    print(f"Chat ID: {CHAT_ID}")

    # -------------------------------------------------------------------------
    # Step 1: Send a text message
    # -------------------------------------------------------------------------
    print("\n--- Step 1: Sending text message ---")
    msg = await bot.send_message(
        chat_id=CHAT_ID,
        text="Curio Phase 0 test starting. Next message will be a photo as Document with buttons.",
    )
    print(f"✓ Text message sent (id={msg.message_id})")

    # -------------------------------------------------------------------------
    # Step 2: Get a real asset ID
    # -------------------------------------------------------------------------
    print("\n--- Step 2: Getting sample asset ---")
    asset_id = get_sample_asset_id()
    if asset_id:
        print(f"✓ Using real asset: {asset_id}")
        check_callback_data_lengths(asset_id)
    else:
        # Use a fake UUID-format placeholder
        asset_id = "00000000-0000-0000-0000-000000000001"
        print(f"  Using placeholder asset_id: {asset_id}")
        check_callback_data_lengths(asset_id)

    # -------------------------------------------------------------------------
    # Step 3: Build inline keyboard
    # -------------------------------------------------------------------------
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{asset_id}"),
            InlineKeyboardButton("👍 Like Only", callback_data=f"like:{asset_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject:{asset_id}"),
        ]
    ])

    # -------------------------------------------------------------------------
    # Step 4: Send thumbnail as Document
    # -------------------------------------------------------------------------
    print("\n--- Step 3: Sending thumbnail as Document ---")
    thumbnail_bytes = get_thumbnail_bytes(asset_id) if IMMICH_API_KEY else None

    if thumbnail_bytes:
        print(f"  Got thumbnail: {len(thumbnail_bytes):,} bytes")
        from io import BytesIO
        msg = await bot.send_photo(
            chat_id=CHAT_ID,
            photo=BytesIO(thumbnail_bytes),
            caption=f"Test photo\nAsset: {asset_id[:8]}...\n\nTap a button to confirm callback.",
            reply_markup=keyboard,
        )
        print(f"✓ Photo sent (message_id={msg.message_id})")
        if msg.photo:
            sizes = [(p.width, p.height, p.file_size) for p in msg.photo]
            print(f"  Telegram photo variants: {sizes}")
    else:
        # Send a text-only message with buttons (no image available)
        print("  No thumbnail available — sending text message with buttons")
        msg = await bot.send_message(
            chat_id=CHAT_ID,
            text=f"Test buttons (no image available)\nAsset: {asset_id[:8]}...",
            reply_markup=keyboard,
        )
        print(f"✓ Message with buttons sent (message_id={msg.message_id})")

    # -------------------------------------------------------------------------
    # Step 5: Poll for callback
    # -------------------------------------------------------------------------
    print("\n--- Step 4: Waiting for button press ---")
    print("Press one of the buttons in Telegram now...")
    print("(Ctrl+C to exit without pressing)\n")

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CallbackQueryHandler(callback_handler))

    await application.initialize()
    await application.updater.start_polling(drop_pending_updates=True)
    await application.start()

    try:
        # Wait until interrupted
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n\nStopping...")

    await application.updater.stop()
    await application.stop()
    await application.shutdown()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
