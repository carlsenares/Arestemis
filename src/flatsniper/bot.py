"""Telegram bot — long-polling, inline buttons.

Sends one formatted message per matching listing with two inline buttons:
  🔗 Open listing   (URL button)
  ✍️ Generate application  (callback -> drafts a German application)

No webhook, no domain, no inbound ports. The message formatting and send helper
here are reused by the scheduler (Milestone 5). `--test-message` sends a single
hardcoded sample listing and then keeps polling so the buttons work.

Milestone 2: the Generate callback replies with a German placeholder. Milestone 4
swaps in the real `draft_application`.
"""

from __future__ import annotations

import argparse
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ContextTypes,
)

from .config import Config, load_config
from .models import Listing

log = logging.getLogger(__name__)

SOURCE_LABELS = {
    "wg_gesucht": "WG-Gesucht",
    "kleinanzeigen": "Kleinanzeigen",
    "immoscout": "ImmoScout24",
}

# Inline-button callback_data is capped at 64 bytes by Telegram.
_CALLBACK_PREFIX = "gen"
_GENERATE_PLACEHOLDER = (
    "✍️ <b>Bewerbung</b>\n\n"
    "Hier erscheint deine maßgeschneiderte Bewerbung auf Deutsch. "
    "Die KI-Generierung wird in Milestone 4 angeschlossen."
)


def generate_callback_data(listing: Listing) -> str:
    """Encode which listing the Generate button refers to."""
    return f"{_CALLBACK_PREFIX}:{listing.source}:{listing.external_id}"[:64]


def parse_generate_callback(data: str) -> tuple[str, str]:
    """Inverse of generate_callback_data -> (source, external_id)."""
    _, source, external_id = data.split(":", 2)
    return source, external_id


def format_listing_message(listing: Listing) -> tuple[str, InlineKeyboardMarkup]:
    """Render a listing into (HTML text, inline keyboard)."""
    parts = [f"🏠 <b>{html.escape(listing.title)}</b>"]

    facts: list[str] = []
    if listing.rent_warm is not None:
        facts.append(f"💶 {listing.rent_warm} € warm")
    if listing.sqm is not None:
        facts.append(f"📐 {listing.sqm:g} m²")
    if listing.rooms is not None:
        facts.append(f"🚪 {listing.rooms:g} Zi.")
    if facts:
        parts.append(" · ".join(facts))

    if listing.is_wg:
        wg = "WG" + (f" ({listing.wg_size}er)" if listing.wg_size else "")
        parts.append(f"👥 {wg}")

    parts.append(f"🔎 {SOURCE_LABELS.get(listing.source, listing.source)}")
    text = "\n".join(parts)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔗 Open listing", url=listing.url),
                InlineKeyboardButton(
                    "✍️ Generate application",
                    callback_data=generate_callback_data(listing),
                ),
            ]
        ]
    )
    return text, keyboard


async def send_listing(bot, chat_id: str | int, listing: Listing) -> None:
    """Send one formatted listing message. Reused by the scheduler (M5)."""
    text, keyboard = format_listing_message(listing)
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False,
    )


async def on_generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the ✍️ Generate button. Placeholder reply for now (M2)."""
    query = update.callback_query
    await query.answer()  # stop Telegram's spinner
    source, external_id = parse_generate_callback(query.data)
    log.info("Generate requested for %s:%s", source, external_id)
    await query.message.reply_text(_GENERATE_PLACEHOLDER, parse_mode=ParseMode.HTML)


def sample_listing() -> Listing:
    """A hardcoded listing for --test-message."""
    return Listing(
        source="wg_gesucht",
        external_id="sample-0",
        url="https://www.wg-gesucht.de/",
        title="Test: 2-Zimmer-Wohnung in Garching",
        rent_warm=980,
        rooms=2,
        sqm=46,
        is_wg=False,
        description="Helle 2-Zimmer-Wohnung nahe der U6, frei ab 01.08.2026.",
    )


def build_application(config: Config, post_init=None) -> Application:
    """Build the PTB Application with the Generate callback wired up."""
    builder = ApplicationBuilder().token(config.secrets.telegram_bot_token)
    if post_init is not None:
        builder = builder.post_init(post_init)
    app = builder.build()
    app.bot_data["config"] = config
    app.add_handler(CallbackQueryHandler(on_generate, pattern=rf"^{_CALLBACK_PREFIX}:"))
    return app


def run_test_message() -> int:
    """Send one sample listing, then keep polling so the buttons work."""
    config = load_config()

    async def _post_init(application: Application) -> None:
        chat_id = config.secrets.telegram_chat_id
        try:
            await send_listing(application.bot, chat_id, sample_listing())
            log.info("Test message sent to chat %s", chat_id)
        except Exception:
            log.exception("Failed to send test message")

    app = build_application(config, post_init=_post_init)
    log.info("Bot running (long-polling). Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(prog="flatsniper.bot", description="Telegram notifier")
    parser.add_argument(
        "--test-message",
        action="store_true",
        help="Send one hardcoded sample listing, then keep polling for button taps.",
    )
    args = parser.parse_args(argv)

    if args.test_message:
        return run_test_message()

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
