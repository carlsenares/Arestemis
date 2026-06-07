"""Tests for the Groq LLM provider with a mocked client (no live calls)."""

from __future__ import annotations

from unittest.mock import MagicMock

from flatsniper.config import ApplicationConfig, Secrets
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


_APP = ApplicationConfig(
    intro="Hallo, mein Name ist Patrik und ich studiere an der TUM.",
    garching_line="Deshalb ist die Lage perfekt für mich.",
    outro="Ich lebe sauber und die Zahlung ist gesichert.",
)


def test_is_garching_detects_by_name_and_plz():
    assert provider.is_garching(Listing("k", "1", "u", "WG", raw={"city_name": "85748 Garching"}))
    assert provider.is_garching(Listing("k", "1", "u", "Schöne Wohnung in Garching b. München"))
    assert not provider.is_garching(Listing("k", "1", "u", "WG", raw={"city_name": "80538 München"}))


def test_draft_includes_garching_line_only_for_garching():
    in_garching = Listing("wg_gesucht", "1", "u", "Wohnung", raw={"city_name": "85748 Garching"})
    text = provider.draft_application(in_garching, _APP)
    assert text.startswith("Hallo, mein Name ist Patrik")
    assert "Deshalb ist die Lage perfekt" in text
    assert text.endswith("Ich lebe sauber und die Zahlung ist gesichert.")
    # Two paragraphs: intro+garching, then outro.
    assert "\n\n" in text


def test_draft_omits_garching_line_elsewhere():
    in_munich = Listing("wg_gesucht", "1", "u", "Wohnung", raw={"city_name": "80538 München"})
    text = provider.draft_application(in_munich, _APP)
    assert "Deshalb ist die Lage perfekt" not in text
    assert text.startswith("Hallo, mein Name ist Patrik")
