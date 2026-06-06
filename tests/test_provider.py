"""Tests for the Groq LLM provider with a mocked client (no live calls)."""

from __future__ import annotations

from unittest.mock import MagicMock

from flatsniper.config import Secrets
from flatsniper.llm import provider
from flatsniper.models import Listing


def _secrets() -> Secrets:
    return Secrets("tok", "chat", "key", "test-model")


def _fake_client(content: str) -> MagicMock:
    client = MagicMock()
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    client.chat.completions.create.return_value = MagicMock(choices=[choice])
    return client


def _wg(desc: str) -> Listing:
    return Listing("wg_gesucht", "1", "u", "2er WG", is_wg=True, wg_size=2, description=desc)


def test_classify_true():
    client = _fake_client('{"empty_2p_wg": true, "reason": "komplette Wohnung frei"}')
    assert provider.classify_empty_2p_wg(_wg("Ganze Wohnung frei, beide Zimmer leer"), _secrets(), client=client) is True


def test_classify_false():
    client = _fake_client('{"empty_2p_wg": false, "reason": "Zimmer in bestehender WG"}')
    assert provider.classify_empty_2p_wg(_wg("Wir sind 1 Person und suchen Mitbewohner"), _secrets(), client=client) is False


def test_classify_bad_json_defaults_false():
    client = _fake_client("totally not json")
    assert provider.classify_empty_2p_wg(_wg("x"), _secrets(), client=client) is False


def test_draft_returns_text_and_uses_model():
    client = _fake_client("Hallo, ich interessiere mich sehr für Ihre Wohnung ...")
    text = provider.draft_application(
        Listing("wg_gesucht", "1", "u", "Wohnung in Garching", sqm=40, rent_warm=1100),
        "Ich bin Patrik, Student an der TUM.",
        _secrets(),
        client=client,
    )
    assert "Hallo" in text
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "test-model"
    assert kwargs["messages"][0]["role"] == "system"
    # the about-me text must reach the prompt
    assert "Patrik" in kwargs["messages"][1]["content"]
