"""Load and validate FlatSniper configuration.

Two sources of truth:
  - .env       -> secrets (Telegram, Groq). Loaded via python-dotenv.
  - config.yaml -> user search criteria. Loaded via PyYAML.

Both are git-ignored. This module turns them into typed dataclasses and fails
loudly with a clear message if anything required is missing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Repo root = three levels up from this file (src/flatsniper/config.py).
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"
DEFAULT_ENV_PATH = REPO_ROOT / ".env"


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


# WG acceptance modes (config: wg_rule.mode). One knob, three states — the
# earlier `allow_empty_2person_wg` boolean could only express two of them.
#   none          -> standalone single-person flats only; WG rooms are dropped.
#   empty_2person -> only an *empty* 2-person WG (user takes the whole flat and
#                    picks the flatmate). Needs the LLM classifier.
#   any           -> ordinary WG rooms are fine, alongside single flats. No
#                    classification: rent/area/move-in decide, then the send-gate.
WG_NONE = "none"
WG_EMPTY_2P = "empty_2person"
WG_ANY = "any"
WG_MODES = (WG_NONE, WG_EMPTY_2P, WG_ANY)


@dataclass(frozen=True)
class RentTier:
    min_sqm: float
    max_rent_warm: int


@dataclass(frozen=True)
class SearchConfig:
    min_sqm: float
    rent_tiers: list[RentTier]
    min_rooms: float
    earliest_move_in: date
    areas: list[str]
    postal_codes: list[str]
    # Upper end of the move-in window. `earliest_move_in` alone is an open-ended
    # floor, which lets a room free from December match an October search; this
    # closes it. None = no upper bound (the old behaviour).
    latest_move_in: date | None = None
    # keyword backstop (swaps/sublets/temporary) — deterministic reject. Defaults
    # empty so direct constructions (tests) stay valid; load_config always sets it.
    blocklist: list[str] = field(default_factory=list)
    # Location names/PLZ that veto a listing even when `areas` matched. `areas`
    # is deliberately broad ("München" scopes the portal queries); this carves the
    # outskirts and surrounding towns back out. Matched whole-word, so
    # "Dachauer Straße" in Maxvorstadt is not read as Dachau.
    exclude_areas: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ApplicationConfig:
    """Template for the generated application. Kept in config.yaml (git-ignored)
    so personal copy isn't hardcoded. `garching_line` is appended to the intro
    only for listings in Garching."""

    intro: str = ""
    garching_line: str = ""
    outro: str = ""


@dataclass(frozen=True)
class Secrets:
    """Secret values from .env. Never logged or printed."""

    telegram_bot_token: str
    telegram_chat_id: str
    groq_api_key: str
    llm_model: str


@dataclass(frozen=True)
class Config:
    search: SearchConfig
    wg_mode: str
    sources: dict[str, bool]
    poll_interval_seconds: int
    about_me: str
    secrets: Secrets
    application: ApplicationConfig = field(default_factory=ApplicationConfig)

    @property
    def wg_allowed(self) -> bool:
        """True if WG rooms are wanted at all (drives which portal categories we
        even query)."""
        return self.wg_mode != WG_NONE

    @property
    def wg_needs_classification(self) -> bool:
        """True only for the strict empty-2-person mode, the one case where a
        WG's text has to be read by the LLM before it can match."""
        return self.wg_mode == WG_EMPTY_2P

    def public_dict(self) -> dict:
        """Config with secrets redacted — safe to print/log."""
        s = self.search
        return {
            "search": {
                "min_sqm": s.min_sqm,
                "rent_tiers": [
                    {"min_sqm": t.min_sqm, "max_rent_warm": t.max_rent_warm}
                    for t in s.rent_tiers
                ],
                "min_rooms": s.min_rooms,
                "earliest_move_in": s.earliest_move_in.isoformat(),
                "latest_move_in": s.latest_move_in.isoformat() if s.latest_move_in else None,
                "areas": s.areas,
                "exclude_areas": s.exclude_areas,
                "postal_codes": s.postal_codes,
                "blocklist": s.blocklist,
            },
            "wg_rule": {"mode": self.wg_mode},
            "sources": self.sources,
            "poll_interval_seconds": self.poll_interval_seconds,
            "about_me": _preview(self.about_me),
            "application": {
                "intro": _preview(self.application.intro),
                "garching_line": _preview(self.application.garching_line),
                "outro": _preview(self.application.outro),
            },
            "secrets": {
                "telegram_bot_token": _redact(self.secrets.telegram_bot_token),
                "telegram_chat_id": _redact(self.secrets.telegram_chat_id),
                "groq_api_key": _redact(self.secrets.groq_api_key),
                "llm_model": self.secrets.llm_model,  # not secret
            },
        }


def _redact(value: str) -> str:
    """Show only that a secret is set, never its value."""
    return "<set>" if value else "<MISSING>"


def _preview(text: str, limit: int = 80) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[:limit] + "…"


# Required env var name -> Secrets field.
_REQUIRED_ENV = {
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_CHAT_ID": "telegram_chat_id",
    "GROQ_API_KEY": "groq_api_key",
}


def _load_secrets(env_path: Path) -> Secrets:
    load_dotenv(env_path)
    missing = [name for name in _REQUIRED_ENV if not os.environ.get(name, "").strip()]
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): "
            + ", ".join(sorted(missing))
            + f"\nSet them in {env_path} (see .env.example)."
        )
    return Secrets(
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"].strip(),
        telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"].strip(),
        groq_api_key=os.environ["GROQ_API_KEY"].strip(),
        # LLM_MODEL has a sensible default; not strictly required.
        llm_model=os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile").strip(),
    )


def _require(data: dict, key: str, where: str):
    if key not in data:
        raise ConfigError(f"Missing '{key}' in {where} of config.yaml")
    return data[key]


# Default keyword blocklist (case-insensitive substring match over title +
# description). Deterministic backstop to the LLM send-gate so explicit swaps and
# sublets are dropped even when the LLM is unavailable. Override via search.blocklist.
_DEFAULT_BLOCKLIST = [
    "Tauschwohnung", "Wohnungstausch", "Tausch", "tausche",
    "Untermiete", "Untermieter", "Untervermietung",
    "Zwischenmiete", "Zwischenmieter", "Zwischenmieten",
    "befristet", "auf Zeit", "Wohnen auf Zeit",
]


def _parse_search(raw: dict) -> SearchConfig:
    tiers_raw = _require(raw, "rent_tiers", "search")
    if not tiers_raw:
        raise ConfigError("search.rent_tiers must contain at least one tier")
    tiers = [
        RentTier(min_sqm=float(t["min_sqm"]), max_rent_warm=int(t["max_rent_warm"]))
        for t in tiers_raw
    ]
    move_in_raw = _require(raw, "earliest_move_in", "search")
    move_in = move_in_raw if isinstance(move_in_raw, date) else date.fromisoformat(str(move_in_raw))
    latest_raw = raw.get("latest_move_in")
    latest = None
    if latest_raw not in (None, ""):
        latest = latest_raw if isinstance(latest_raw, date) else date.fromisoformat(str(latest_raw))
        if latest < move_in:
            raise ConfigError(
                f"search.latest_move_in ({latest}) is before search.earliest_move_in "
                f"({move_in}) — the move-in window is empty, nothing could ever match."
            )
    return SearchConfig(
        min_sqm=float(_require(raw, "min_sqm", "search")),
        rent_tiers=tiers,
        min_rooms=float(raw.get("min_rooms", 1)),
        earliest_move_in=move_in,
        areas=list(_require(raw, "areas", "search")),
        postal_codes=list(raw.get("postal_codes") or []),
        latest_move_in=latest,
        blocklist=[str(x).strip() for x in (raw.get("blocklist") or _DEFAULT_BLOCKLIST) if str(x).strip()],
        exclude_areas=[str(x).strip() for x in (raw.get("exclude_areas") or []) if str(x).strip()],
    )


def _parse_wg_mode(raw: dict) -> str:
    wg_raw = raw.get("wg_rule") or {}
    if "mode" not in wg_raw and "allow_empty_2person_wg" in wg_raw:
        raise ConfigError(
            "wg_rule.allow_empty_2person_wg was replaced by wg_rule.mode.\n"
            f"Set one of: {', '.join(WG_MODES)} "
            f"({WG_EMPTY_2P} is the old `true`, {WG_NONE} the old `false`)."
        )
    mode = str(wg_raw.get("mode", WG_NONE)).strip()
    if mode not in WG_MODES:
        raise ConfigError(
            f"Invalid wg_rule.mode {mode!r} in config.yaml. Expected one of: "
            + ", ".join(WG_MODES)
        )
    return mode


def load_config(
    config_path: Path | None = None,
    env_path: Path | None = None,
) -> Config:
    """Load .env + config.yaml into a typed Config. Raises ConfigError on problems."""
    config_path = config_path or DEFAULT_CONFIG_PATH
    env_path = env_path or DEFAULT_ENV_PATH

    if not config_path.exists():
        raise ConfigError(
            f"Config file not found: {config_path}\n"
            "Copy config.ready.yaml (or config.example.yaml) to config.yaml."
        )

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse {config_path}: {exc}") from exc

    secrets = _load_secrets(env_path)
    search = _parse_search(_require(raw, "search", "top level"))
    app_raw = raw.get("application") or {}
    application = ApplicationConfig(
        intro=str(app_raw.get("intro", "")).strip(),
        garching_line=str(app_raw.get("garching_line", "")).strip(),
        outro=str(app_raw.get("outro", "")).strip(),
    )

    return Config(
        search=search,
        wg_mode=_parse_wg_mode(raw),
        sources={k: bool(v) for k, v in (raw.get("sources") or {}).items()},
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 60)),
        about_me=str(raw.get("about_me", "")).strip(),
        application=application,
        secrets=secrets,
    )
