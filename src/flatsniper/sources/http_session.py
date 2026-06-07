"""Polite, browser-like HTTP for source adapters (anti-ban layer).

Scraping WG-Gesucht/Kleinanzeigen is against their ToS, so the priority is to
not get the user's IP (or accounts) banned. The dominant levers for a single
user polling every ~60s are (1) keeping the request rate low and (2) not looking
like an obvious bot. This module centralises (2) so every adapter shares one
coherent, maintainable disguise:

  * **Coherent fingerprints.** A `Profile` bundles a User-Agent with the
    `sec-ch-ua*` Client Hints that a real Chrome of that version/platform would
    send. We never emit a mobile UA with desktop hints (or vice-versa) — a
    mismatch there is itself a bot tell. One profile is chosen per client and
    stays stable for that client's life, the way a real browser session does.
  * **Full request headers.** Real browsers send Accept, Accept-Language (de
    first — these are German portals), Sec-Fetch-* and a Referer appropriate to
    whether the request is a top-level navigation (an HTML page) or an XHR/fetch
    (a JSON API). `navigation_headers` and `api_headers` produce each shape.
  * **HTTP/2 + persistent cookies.** Chrome speaks HTTP/2; an HTTP/1.1 request
    from a "Chrome" UA is suspicious, so clients negotiate h2. Cookies persist
    for the client's lifetime, and `SessionPool` keeps one client per source
    alive across poll cycles so we look like a returning visitor, not a fresh
    anonymous hit every minute.
  * **Jitter.** `jitter()` spreads out the fixed politeness delays (and the poll
    interval) so traffic isn't a metronome — perfectly periodic requests are an
    easy automated-client signature.

What this deliberately does NOT do: TLS/JA3 fingerprint spoofing (needs
curl_cffi) or proxy/IP rotation. Both are heavier and, per the project's
single-user threat model, overkill unless bans actually start happening. See
docs/ARCHITECTURE.md for the escalation path.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

import httpx

try:  # Browser impersonation: real Chrome TLS (JA3/JA4) + HTTP/2 fingerprint.
    from curl_cffi import requests as _curl

    _HAS_CURL = True
except Exception:  # pragma: no cover - fallback path when curl_cffi is absent
    _curl = None
    _HAS_CURL = False

# curl_cffi impersonation target. "chrome" tracks curl_cffi's newest supported
# Chrome (coherent TLS + HTTP/2/3 + Client-Hint headers). When this backend is
# active we let impersonation own the identity headers (UA, sec-ch-ua, ...) so
# they always match the fingerprint — we only layer on Accept-Language + Referer.
IMPERSONATE = "chrome"
ACCEPT_LANGUAGE = "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"


@dataclass(frozen=True)
class Profile:
    """A self-consistent browser identity: UA + matching Client Hints."""

    user_agent: str
    sec_ch_ua: str
    platform: str  # value for sec-ch-ua-platform, incl. quotes
    mobile: str = "?0"  # value for sec-ch-ua-mobile


# A small pool of *current, coherent* desktop-Chrome identities. Kept short on
# purpose: a handful of plausible browsers is realistic; a huge rotating pool is
# not, and stale Chrome versions are a tell, so bump these occasionally. The UA
# version and the sec-ch-ua version must agree within each entry.
PROFILES: list[Profile] = [
    Profile(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        sec_ch_ua='"Google Chrome";v="126", "Chromium";v="126", "Not_A Brand";v="24"',
        platform='"Windows"',
    ),
    Profile(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        sec_ch_ua='"Google Chrome";v="126", "Chromium";v="126", "Not_A Brand";v="24"',
        platform='"macOS"',
    ),
    Profile(
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
        sec_ch_ua='"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
        platform='"Linux"',
    ),
]


def pick_profile(rng: random.Random | None = None) -> Profile:
    """Choose one browser identity, kept stable for a whole client/session."""
    return (rng or random).choice(PROFILES)


def jitter(base: float, frac: float = 0.4, rng: random.Random | None = None) -> float:
    """Spread `base` seconds by ±`frac` so delays aren't a fixed metronome.

    Returns a value in [base*(1-frac), base*(1+frac)], clamped to >= 0. Perfectly
    periodic requests are an obvious automated-client signature; this breaks that
    up for both inter-request gaps and the poll interval itself.
    """
    frac = max(0.0, min(frac, 1.0))
    delta = base * frac
    value = (rng or random).uniform(base - delta, base + delta)
    return max(0.0, value)


def client_headers(profile: Profile) -> dict[str, str]:
    """Stable headers a browser sends on *every* request to a host.

    These become the client's defaults; per-request builders below add the
    Accept / Sec-Fetch-* / Referer that vary by request type. We intentionally
    do NOT set Accept-Encoding (httpx negotiates it) so the advertised encodings
    match what the client can actually decode.
    """
    return {
        "User-Agent": profile.user_agent,
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        "sec-ch-ua": profile.sec_ch_ua,
        "sec-ch-ua-mobile": profile.mobile,
        "sec-ch-ua-platform": profile.platform,
        "DNT": "1",
    }


def navigation_headers(referer: str | None = None) -> dict[str, str]:
    """Per-request headers for a top-level HTML page load (a 'navigation')."""
    headers = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin" if referer else "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def api_headers(
    referer: str | None = None, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """Per-request headers for an XHR/fetch to a JSON API (as a page would make)."""
    headers = {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if referer:
        headers["Referer"] = referer
    if extra:
        headers.update(extra)
    return headers


def new_client(
    profile: Profile | None = None,
    *,
    timeout: float = 20.0,
    rng: random.Random | None = None,
):
    """Build an HTTP client with the strongest available browser disguise.

    Primary: a `curl_cffi` session impersonating Chrome — this is the deepest
    masking layer, replicating Chrome's TLS (JA3/JA4) and HTTP/2 fingerprints,
    which header-only spoofing can't. A WAF that fingerprints the handshake (e.g.
    DataDome) sees a real browser. Impersonation owns the identity headers (so
    they match the fingerprint); we only set Accept-Language (German) here and
    pass Referer/Accept per request via the header builders.

    Fallback (curl_cffi unavailable): an httpx client wearing one coherent
    desktop-Chrome `Profile` over HTTP/2. Weaker (non-Chrome TLS) but functional.

    Either way cookies persist for the client's lifetime, so reusing one client
    across cycles looks like a returning session (see `SessionPool`).
    """
    if _HAS_CURL:
        return _curl.AsyncSession(
            impersonate=IMPERSONATE,
            headers={"Accept-Language": ACCEPT_LANGUAGE},
            timeout=timeout,
            default_headers=True,
        )
    profile = profile or pick_profile(rng)
    kwargs = dict(
        headers=client_headers(profile),
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
    )
    try:
        return httpx.AsyncClient(http2=True, **kwargs)
    except ImportError:  # h2 not installed — degrade gracefully
        return httpx.AsyncClient(**kwargs)


async def aclose(client) -> None:
    """Close a client from either backend (httpx: aclose; curl_cffi: close)."""
    if hasattr(client, "aclose"):
        await client.aclose()
    elif hasattr(client, "close"):
        await client.close()


# --- Rate-limit circuit breaker -------------------------------------------


class RateLimited(Exception):
    """A request was rate-limited (HTTP 429) after exhausting in-call retries."""


class CircuitBreaker:
    """Stops hitting a rate-limited endpoint instead of hammering it.

    When a guarded endpoint (e.g. WG-Gesucht's detail API) returns 429, retrying
    it every cycle *sustains* the penalty and risks escalating a rate-limit into
    a real ban. After `threshold` failures the breaker "opens" for `cooldown`
    seconds — callers check `allow()` and skip the endpoint entirely while open,
    letting the limit recover. A single success closes it again.
    """

    def __init__(self, name: str, *, threshold: int = 2, cooldown: float = 600.0,
                 clock=time.monotonic) -> None:
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown
        self._clock = clock
        self.fails = 0
        self._open_until = 0.0

    def allow(self) -> bool:
        """True if the endpoint may be called now (circuit closed or cooled down)."""
        return self._clock() >= self._open_until

    def is_open(self) -> bool:
        return not self.allow()

    def seconds_left(self) -> float:
        return max(0.0, self._open_until - self._clock())

    def record_success(self) -> None:
        self.fails = 0
        self._open_until = 0.0

    def record_failure(self) -> bool:
        """Register a 429. Returns True if this just *opened* the circuit."""
        self.fails += 1
        if self.fails >= self.threshold and not self.is_open():
            self._open_until = self._clock() + self.cooldown
            return True
        return False


_BREAKERS: dict[str, CircuitBreaker] = {}


def get_breaker(name: str) -> CircuitBreaker:
    """Shared (process-global) breaker per name, so the scheduler and the
    /health command see the same state."""
    return _BREAKERS.setdefault(name, CircuitBreaker(name))


class SessionPool:
    """One long-lived, cookie-keeping client per source, reused across cycles.

    Keeping the same client (and thus the same profile + cookie jar) alive for
    the life of the scheduler makes repeat polling look like one returning
    browser session rather than a fresh anonymous hit every minute.
    """

    def __init__(self, *, rng: random.Random | None = None) -> None:
        self._clients: dict = {}
        self._rng = rng

    def client_for(self, source: str):
        client = self._clients.get(source)
        if client is None:
            client = new_client(rng=self._rng)
            self._clients[source] = client
        return client

    async def aclose(self) -> None:
        for client in self._clients.values():
            await aclose(client)
        self._clients.clear()
