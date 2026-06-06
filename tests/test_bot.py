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


def test_format_message_marks_wg():
    text, _ = format_listing_message(_listing(is_wg=True, wg_size=2))
    assert "WG (2er)" in text


def test_on_generate_drafts_for_cached_listing(monkeypatch):
    monkeypatch.setattr(
        bot, "draft_application", lambda listing, about_me, secrets: "Hallo, ich bewerbe mich ..."
    )
    note = MagicMock()
    note.edit_text = AsyncMock()
    query = MagicMock()
    query.data = "gen:wg_gesucht:abc123"
    query.answer = AsyncMock()
    query.message.reply_text = AsyncMock(return_value=note)
    update = MagicMock(callback_query=query)

    listing = _listing()  # source wg_gesucht, external_id abc123
    config = MagicMock()
    config.about_me = "Ich bin Patrik"
    context = MagicMock()
    context.bot_data = {"listings": {"wg_gesucht:abc123": listing}, "config": config}

    asyncio.run(on_generate(update, context))

    query.answer.assert_awaited_once()
    note.edit_text.assert_awaited_once_with("Hallo, ich bewerbe mich ...")


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
