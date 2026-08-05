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


def _system_prompt(client: MagicMock) -> str:
    return client.chat.completions.create.call_args.kwargs["messages"][0]["content"]


def test_send_gate_prompt_follows_allow_wg():
    """The gate that rejects every shared flat must not run when WG rooms are
    exactly what the user is looking for."""
    client = _fake_client('{"send": true, "reason": "ok"}')

    provider.should_notify(_wg("Zimmer in 3er WG"), _secrets(), allow_wg=False, client=client)
    single_only = _system_prompt(client)
    assert "WG-Zimmer" in single_only  # listed as an exclusion

    provider.should_notify(_wg("Zimmer in 3er WG"), _secrets(), allow_wg=True, client=client)
    wg_ok = _system_prompt(client)
    assert "Zimmer in einer WG" in wg_ok  # listed as wanted
    # Both still refuse swaps/sublets/temporary lets.
    for prompt in (single_only, wg_ok):
        assert "Wohnungstausch" in prompt and "Untermiete" in prompt and "Zwischenmiete" in prompt


def test_send_gate_fails_open_on_llm_error():
    client = MagicMock()
    client.chat.completions.create.side_effect = RuntimeError("groq down")
    send, reason = provider.should_notify(_wg("x"), _secrets(), allow_wg=True, client=client)
    assert send is True and reason == "llm-error-fail-open"


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
