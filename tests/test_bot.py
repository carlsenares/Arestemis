"""Offline tests for the Telegram notifier formatting + callback wiring.

These don't touch the network; the live phone-buzz check is the manual
acceptance (`python -m flatsniper.bot --test-message`).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from flatsniper import bot
from flatsniper.bot import (
    format_listing_message,
    generate_callback_data,
    on_generate,
    parse_generate_callback,
)
from flatsniper.models import Listing


def _listing(**kw) -> Listing:
    base = dict(
        source="wg_gesucht",
        external_id="abc123",
        url="https://example.com/abc123",
        title="2-Zimmer-Wohnung",
        rent_warm=980,
        rooms=2,
        sqm=46,
    )
    base.update(kw)
    return Listing(**base)


def test_callback_data_roundtrip():
    listing = _listing(source="kleinanzeigen", external_id="x:y:z")
    data = generate_callback_data(listing)
    assert data.startswith("gen:")
    assert len(data.encode()) <= 64
    assert parse_generate_callback(data) == ("kleinanzeigen", "x:y:z")


def test_format_message_has_facts_and_buttons():
    text, markup = format_listing_message(_listing())

    assert "2-Zimmer-Wohnung" in text
    assert "980 € warm" in text
    assert "46 m²" in text

    buttons = markup.inline_keyboard[0]
    assert len(buttons) == 2
    open_btn, gen_btn = buttons
    assert open_btn.url == "https://example.com/abc123"
    assert gen_btn.callback_data == "gen:wg_gesucht:abc123"


def test_format_message_escapes_html():
    text, _ = format_listing_message(_listing(title="A & B <script>"))
    assert "&amp;" in text and "&lt;script&gt;" in text
    assert "<script>" not in text


def test_format_message_labels_estimated_rent():
    text, _ = format_listing_message(
        _listing(source="immowelt", rent_warm=1190,
                 raw={"rent_estimated": True, "rent_cold": 1050})
    )
    assert "~1190 € warm (gesch.)" in text
    assert "1050 € kalt" in text
    assert "Immowelt" in text


def test_format_message_marks_wg():
    text, _ = format_listing_message(_listing(is_wg=True, wg_size=2))
    assert "WG (2er)" in text


def test_on_generate_drafts_for_cached_listing(monkeypatch):
    monkeypatch.setattr(
        bot, "draft_application", lambda listing, application: "Hallo, ich bewerbe mich ..."
    )
    query = MagicMock()
    query.data = "gen:wg_gesucht:abc123"
    query.answer = AsyncMock()
    query.message.message_id = 4242
    query.message.reply_text = AsyncMock()
    update = MagicMock(callback_query=query)

    listing = _listing()  # source wg_gesucht, external_id abc123
    config = MagicMock()
    context = MagicMock()
    context.bot_data = {"listings": {"wg_gesucht:abc123": listing}, "config": config}

    asyncio.run(on_generate(update, context))

    query.answer.assert_awaited_once()
    # The draft is sent as a reply quoting the originating listing message.
    args, kwargs = query.message.reply_text.call_args
    assert args[0] == "Hallo, ich bewerbe mich ..."
    assert kwargs["reply_parameters"].message_id == 4242


def test_on_generate_handles_missing_listing():
    query = MagicMock()
    query.data = "gen:wg_gesucht:gone"
    query.answer = AsyncMock()
    query.message.reply_text = AsyncMock()
    update = MagicMock(callback_query=query)
    context = MagicMock()
    context.bot_data = {"listings": {}, "config": MagicMock()}

    asyncio.run(on_generate(update, context))

    query.message.reply_text.assert_awaited_once()
    assert "nicht mehr" in query.message.reply_text.call_args.args[0]


def test_remember_listing():
    bot_data = {}
    bot.remember_listing(bot_data, _listing())
    assert bot_data["listings"]["wg_gesucht:abc123"].title == "2-Zimmer-Wohnung"


# --- remote control (start/stop scanning) ----------------------------------


class _FakeStore:
    def __init__(self):
        self.meta = {}

    def set_meta(self, key, value):
        self.meta[key] = value

    def get_meta(self, key, default=None):
        return self.meta.get(key, default)


def _control_ctx(owner_id="555", chat_id=555, store=None):
    config = MagicMock()
    config.secrets.telegram_chat_id = owner_id
    config.poll_interval_seconds = 60
    config.sources = {"wg_gesucht": True, "kleinanzeigen": True}
    context = MagicMock()
    context.bot_data = {"config": config}
    if store is not None:
        context.bot_data["store"] = store
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    return update, context


def test_set_paused_persists_to_store():
    store = _FakeStore()
    bot_data = {"store": store}
    bot.set_paused(bot_data, True)
    assert bot_data["paused"] is True
    assert store.get_meta(bot.PAUSED_META_KEY) == "1"
    bot.set_paused(bot_data, False)
    assert store.get_meta(bot.PAUSED_META_KEY) == "0"


def test_stop_then_start_toggles_flag():
    store = _FakeStore()
    update, context = _control_ctx(store=store)

    asyncio.run(bot.on_stop(update, context))
    assert bot.is_paused(context.bot_data) is True
    assert store.get_meta(bot.PAUSED_META_KEY) == "1"
    update.message.reply_text.assert_awaited_once()

    asyncio.run(bot.on_start(update, context))
    assert bot.is_paused(context.bot_data) is False
    assert store.get_meta(bot.PAUSED_META_KEY) == "0"


def test_control_ignores_non_owner():
    store = _FakeStore()
    # chat id differs from the configured owner -> command ignored.
    update, context = _control_ctx(owner_id="555", chat_id=999, store=store)
    asyncio.run(bot.on_stop(update, context))
    assert "paused" not in context.bot_data
    update.message.reply_text.assert_not_awaited()


def test_on_health_probes_and_reports(monkeypatch):
    from flatsniper.health import PortalHealth

    async def fake_check_all(get_client=None):
        return [PortalHealth("WG-Gesucht", True, "OK"),
                PortalHealth("Kleinanzeigen", False, "BLOCKED (403)")]

    monkeypatch.setattr(bot.health, "check_all", fake_check_all)
    note = MagicMock()
    note.edit_text = AsyncMock()
    update, context = _control_ctx()
    update.message.reply_text = AsyncMock(return_value=note)

    asyncio.run(bot.on_health(update, context))

    report = note.edit_text.call_args.args[0]
    assert "WG-Gesucht: OK" in report and "BLOCKED (403)" in report


def test_status_reports_state():
    update, context = _control_ctx()
    context.bot_data["paused"] = True
    asyncio.run(bot.on_status(update, context))
    msg = update.message.reply_text.call_args.args[0]
    assert "pausiert" in msg
