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

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyParameters,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from .config import Config, load_config
from .llm.provider import draft_application
from .models import Listing

log = logging.getLogger(__name__)

SOURCE_LABELS = {
    "wg_gesucht": "WG-Gesucht",
    "kleinanzeigen": "Kleinanzeigen",
    "immowelt": "Immowelt",
    "immoscout": "ImmoScout24",
}

# Inline-button callback_data is capped at 64 bytes by Telegram.
_CALLBACK_PREFIX = "gen"


def listing_key(source: str, external_id: str) -> str:
    return f"{source}:{external_id}"


def remember_listing(bot_data: dict, listing: Listing) -> None:
    """Cache a listing so the Generate callback can recover it later."""
    bot_data.setdefault("listings", {})[listing_key(listing.source, listing.external_id)] = listing


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
        if (listing.raw or {}).get("rent_estimated"):
            cold = listing.raw.get("rent_cold")
            facts.append(
                f"💶 ~{listing.rent_warm} € warm (gesch.)"
                + (f", {cold} € kalt" if cold else "")
            )
        else:
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
    """Handle the ✍️ Generate button: reply (quoting that listing) with the draft."""
    query = update.callback_query
    await query.answer()  # stop Telegram's spinner
    source, external_id = parse_generate_callback(query.data)
    log.info("Generate requested for %s:%s", source, external_id)

    # Quote the specific listing message so the text lands under the right one
    # even when several alerts arrived together.
    reply_to = ReplyParameters(
        message_id=query.message.message_id, allow_sending_without_reply=True
    )

    listing = context.bot_data.get("listings", {}).get(listing_key(source, external_id))
    config: Config | None = context.bot_data.get("config")
    if listing is None or config is None:
        await query.message.reply_text(
            "⚠️ Diese Anzeige ist nicht mehr im Zwischenspeicher (vermutlich Bot-Neustart). "
            "Bitte öffne den Link und bewirb dich direkt.",
            reply_parameters=reply_to,
        )
        return

    try:
        text = draft_application(listing, config.application)
    except Exception:
        log.exception("draft_application failed for %s:%s", source, external_id)
        await query.message.reply_text(
            "⚠️ Konnte die Bewerbung nicht generieren.", reply_parameters=reply_to
        )
        return
    await query.message.reply_text(text or "(leer)", reply_parameters=reply_to)


# --- Remote control (start/stop scanning from the phone) -------------------
#
# "Stopping" pauses portal scanning but keeps the process (and Telegram polling)
# alive, so the phone can still resume it — killing the OS process would make the
# bot unreachable. The flag is mirrored to the store so a reboot/crash-restart
# respects it. The Generate button keeps working while paused.

PAUSED_META_KEY = "scanning_paused"

# Commands shown in the phone's command menu (set via set_my_commands).
BOT_COMMANDS = [
    BotCommand("stop", "⏸ Pause scanning (no new alerts)"),
    BotCommand("start", "▶️ Resume scanning"),
    BotCommand("status", "ℹ️ Show whether scanning is active"),
    BotCommand("help", "Show available commands"),
]


def is_paused(bot_data: dict) -> bool:
    return bool(bot_data.get("paused"))


def set_paused(bot_data: dict, value: bool) -> None:
    """Flip the in-memory flag and persist it so it survives a restart."""
    bot_data["paused"] = value
    store = bot_data.get("store")
    if store is not None:
        store.set_meta(PAUSED_META_KEY, "1" if value else "0")


def _is_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Only the configured chat may control the bot."""
    config: Config | None = context.bot_data.get("config")
    chat = update.effective_chat
    if config is None or chat is None:
        return False
    return str(chat.id) == str(config.secrets.telegram_chat_id)


def _status_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    config: Config | None = context.bot_data.get("config")
    paused = is_paused(context.bot_data)
    state = "⏸ <b>pausiert</b>" if paused else "▶️ <b>aktiv</b>"
    lines = [f"FlatSniper: {state}"]
    if config is not None:
        sources = [SOURCE_LABELS.get(n, n) for n, on in config.sources.items() if on]
        lines.append(f"⏱ Intervall: {config.poll_interval_seconds}s")
        lines.append(f"🔎 Quellen: {', '.join(sources) or '—'}")
    return "\n".join(lines)


async def on_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update, context):
        return
    set_paused(context.bot_data, True)
    log.info("Scanning paused via Telegram")
    await update.message.reply_text(
        "⏸ Scanning pausiert. Keine neuen Benachrichtigungen, bis du /start sendest."
    )


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update, context):
        return
    was_paused = is_paused(context.bot_data)
    set_paused(context.bot_data, False)
    log.info("Scanning resumed via Telegram")
    prefix = "▶️ Scanning aktiv.\n\n" if was_paused else ""
    await update.message.reply_text(
        prefix + _status_text(context), parse_mode=ParseMode.HTML
    )


async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update, context):
        return
    await update.message.reply_text(_status_text(context), parse_mode=ParseMode.HTML)


async def on_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update, context):
        return
    await update.message.reply_text(
        "Befehle:\n"
        "/stop — Scanning pausieren\n"
        "/start — Scanning fortsetzen\n"
        "/status — Status anzeigen\n"
        "/help — diese Hilfe"
    )


def _register_control_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("stop", on_stop))
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("status", on_status))
    app.add_handler(CommandHandler("help", on_help))


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
    """Build the PTB Application with the Generate callback + control commands."""

    async def _post_init(application: Application) -> None:
        # Populate the phone's command menu, then run any caller-supplied hook.
        try:
            await application.bot.set_my_commands(BOT_COMMANDS)
        except Exception:
            log.exception("Failed to set bot command menu")
        if post_init is not None:
            await post_init(application)

    app = ApplicationBuilder().token(config.secrets.telegram_bot_token).post_init(_post_init).build()
    app.bot_data["config"] = config
    app.add_handler(CallbackQueryHandler(on_generate, pattern=rf"^{_CALLBACK_PREFIX}:"))
    _register_control_handlers(app)
    return app


def run_test_message() -> int:
    """Send one sample listing, then keep polling so the buttons work."""
    config = load_config()

    async def _post_init(application: Application) -> None:
        chat_id = config.secrets.telegram_chat_id
        listing = sample_listing()
        remember_listing(application.bot_data, listing)  # so Generate works
        try:
            await send_listing(application.bot, chat_id, listing)
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
