"""Tests for config loading — specifically the WG mode, whose three states decide
whether WG rooms are searched at all. Getting it wrong silently narrows the search,
so an unknown or stale value must fail loudly rather than fall back to a default."""

from __future__ import annotations

from datetime import date

import pytest

from flatsniper.config import WG_ANY, WG_EMPTY_2P, WG_NONE, ConfigError, load_config

_MINIMAL = """
search:
  min_sqm: 0
  rent_tiers:
    - {{ min_sqm: 0, max_rent_warm: 900 }}
  earliest_move_in: "2026-10-01"
{latest}  areas:
    - München
  exclude_areas:
    - Garching
{wg}
"""

_ENV = "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=c\nGROQ_API_KEY=k\n"


def _load(tmp_path, wg_block: str, latest: str = ""):
    (tmp_path / "config.yaml").write_text(
        _MINIMAL.format(wg=wg_block, latest=latest), encoding="utf-8"
    )
    (tmp_path / ".env").write_text(_ENV, encoding="utf-8")
    return load_config(tmp_path / "config.yaml", tmp_path / ".env")


def test_move_in_window_is_parsed(tmp_path):
    config = _load(tmp_path, "wg_rule:\n  mode: any\n", latest='  latest_move_in: "2026-10-15"\n')
    assert config.search.latest_move_in == date(2026, 10, 15)


def test_latest_move_in_is_optional(tmp_path):
    assert _load(tmp_path, "").search.latest_move_in is None


def test_backwards_move_in_window_is_rejected(tmp_path):
    """An empty window matches nothing — fail loudly instead of going silent."""
    with pytest.raises(ConfigError, match="before search.earliest_move_in"):
        _load(tmp_path, "", latest='  latest_move_in: "2026-09-01"\n')


def test_wg_mode_any(tmp_path):
    config = _load(tmp_path, "wg_rule:\n  mode: any\n")
    assert config.wg_mode == WG_ANY
    assert config.wg_allowed is True
    assert config.wg_needs_classification is False
    assert config.search.exclude_areas == ["Garching"]


def test_wg_mode_empty_2person_needs_classification(tmp_path):
    config = _load(tmp_path, f"wg_rule:\n  mode: {WG_EMPTY_2P}\n")
    assert config.wg_allowed is True
    assert config.wg_needs_classification is True


def test_wg_mode_defaults_to_none(tmp_path):
    config = _load(tmp_path, "")
    assert config.wg_mode == WG_NONE
    assert config.wg_allowed is False


def test_unknown_wg_mode_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="Invalid wg_rule.mode"):
        _load(tmp_path, "wg_rule:\n  mode: sometimes\n")


def test_legacy_wg_flag_is_rejected(tmp_path):
    """The old boolean would otherwise be ignored and WGs would go quiet."""
    with pytest.raises(ConfigError, match="wg_rule.mode"):
        _load(tmp_path, "wg_rule:\n  allow_empty_2person_wg: true\n")
