"""Tests for the polite/browser-like HTTP layer (anti-ban). No network."""

from __future__ import annotations

import asyncio
import random
import re

import httpx
import pytest

from flatsniper.sources import http_session as hs


def test_profiles_are_internally_coherent():
    """A profile's UA Chrome version must match its sec-ch-ua — a mismatch is a
    bot tell — and desktop profiles must declare mobile=?0."""
    for p in hs.PROFILES:
        m = re.search(r"Chrome/(\d+)", p.user_agent)
        assert m, f"no Chrome version in UA: {p.user_agent}"
        version = m.group(1)
        assert f'v="{version}"' in p.sec_ch_ua  # UA version present in Client Hints
        assert p.mobile == "?0"
        assert p.platform.startswith('"') and p.platform.endswith('"')


def test_pick_profile_is_from_pool_and_seedable():
    rng = random.Random(1234)
    chosen = hs.pick_profile(rng)
    assert chosen in hs.PROFILES
    # Same seed -> same choice (stable identity selection).
    assert hs.pick_profile(random.Random(1234)) == chosen


def test_jitter_stays_within_bounds_and_nonnegative():
    rng = random.Random(0)
    for _ in range(500):
        v = hs.jitter(10.0, 0.4, rng)
        assert 6.0 <= v <= 14.0
    # frac is clamped to [0,1]; never returns negative even for a huge frac.
    for _ in range(100):
        assert hs.jitter(1.0, 5.0, rng) >= 0.0


def test_jitter_zero_frac_is_exact():
    assert hs.jitter(3.0, 0.0) == 3.0


def test_client_headers_match_profile_and_prefer_german():
    p = hs.PROFILES[0]
    h = hs.client_headers(p)
    assert h["User-Agent"] == p.user_agent
    assert h["sec-ch-ua"] == p.sec_ch_ua
    assert h["sec-ch-ua-platform"] == p.platform
    assert h["sec-ch-ua-mobile"] == p.mobile
    assert h["Accept-Language"].startswith("de-DE")  # German portals
    # Accept-Encoding is left to httpx so advertised == decodable.
    assert "Accept-Encoding" not in h


def test_navigation_headers_referer_drives_fetch_site():
    no_ref = hs.navigation_headers()
    assert no_ref["Sec-Fetch-Site"] == "none"
    assert "Referer" not in no_ref
    assert no_ref["Sec-Fetch-Mode"] == "navigate"
    assert "text/html" in no_ref["Accept"]

    with_ref = hs.navigation_headers(referer="https://x/y")
    assert with_ref["Sec-Fetch-Site"] == "same-origin"
    assert with_ref["Referer"] == "https://x/y"


def test_api_headers_look_like_xhr_and_merge_extra():
    h = hs.api_headers(referer="https://x/offer/1", extra={"X-Client-Id": "abc"})
    assert h["Accept"].startswith("application/json")
    assert h["X-Requested-With"] == "XMLHttpRequest"
    assert h["Sec-Fetch-Mode"] == "cors"
    assert h["Referer"] == "https://x/offer/1"
    assert h["X-Client-Id"] == "abc"


def test_new_client_is_usable_and_closeable():
    async def go():
        client = hs.new_client()
        try:
            # Backend-agnostic contract: an async GET and a working closer.
            assert callable(getattr(client, "get", None))
        finally:
            await hs.aclose(client)  # works for curl_cffi (close) and httpx (aclose)

    asyncio.run(go())


def test_curl_cffi_is_the_active_backend():
    # The whole point of the anti-detection work: impersonation must be active.
    # (If curl_cffi is somehow missing in CI, the httpx fallback is still valid.)
    if hs._HAS_CURL:
        assert hs.IMPERSONATE == "chrome"
        client = hs.new_client()
        assert type(client).__module__.startswith("curl_cffi")
        asyncio.run(hs.aclose(client))
    else:  # pragma: no cover
        assert isinstance(hs.new_client(), httpx.AsyncClient)


def test_circuit_breaker_opens_and_cools_down():
    t = {"now": 0.0}
    br = hs.CircuitBreaker("wg", threshold=2, cooldown=600.0, clock=lambda: t["now"])
    assert br.allow() is True
    assert br.record_failure() is False  # 1st failure: not open yet
    assert br.allow() is True
    assert br.record_failure() is True   # 2nd failure: opens, returns True once
    assert br.is_open() and br.allow() is False
    assert 0 < br.seconds_left() <= 600
    # Still open just before cooldown elapses; closed after.
    t["now"] = 599
    assert br.allow() is False
    t["now"] = 600
    assert br.allow() is True


def test_circuit_breaker_success_resets():
    br = hs.CircuitBreaker("x", threshold=2, cooldown=10, clock=lambda: 0.0)
    br.record_failure()
    br.record_success()  # clears the failure count
    assert br.record_failure() is False  # back to needing `threshold` again
    assert br.allow() is True


def test_get_breaker_is_shared_by_name():
    a = hs.get_breaker("shared-name-test")
    b = hs.get_breaker("shared-name-test")
    assert a is b  # scheduler and /health see the same instance


def test_immoscout_identity_is_an_app_not_a_browser():
    """The IS24 identity must look like the app: its own UA + JSON Accept, and NOT
    carry a browser's Client Hints (a Chrome sec-ch-ua over an app UA is a tell)."""
    ident = hs.IDENTITIES["immoscout"]
    assert ident.headers["User-Agent"].startswith("ImmoScout24_")
    assert ident.headers["Accept"] == "application/json"
    assert ident.headers["Accept-Language"].startswith("de-DE")
    assert "sec-ch-ua" not in ident.headers
    assert ident.impersonate == "chrome_android"  # mobile TLS, coherent with the app


def test_proxy_for_reads_env(monkeypatch):
    monkeypatch.delenv("IMMOSCOUT_PROXY", raising=False)
    monkeypatch.delenv("FLATSNIPER_PROXY_IMMOSCOUT", raising=False)
    assert hs.proxy_for("immoscout") is None  # unset -> direct connection (default)

    monkeypatch.setenv("IMMOSCOUT_PROXY", "http://user:pass@host:8080")
    assert hs.proxy_for("immoscout") == "http://user:pass@host:8080"

    # Generic fallback, and blank/whitespace is treated as unset.
    monkeypatch.delenv("IMMOSCOUT_PROXY", raising=False)
    monkeypatch.setenv("FLATSNIPER_PROXY_IMMOSCOUT", "socks5://h:1")
    assert hs.proxy_for("immoscout") == "socks5://h:1"
    monkeypatch.setenv("FLATSNIPER_PROXY_IMMOSCOUT", "   ")
    assert hs.proxy_for("immoscout") is None


def test_identity_client_uses_app_headers():
    """An identity client carries the app UA (curl_cffi: default_headers off so no
    Chrome hints leak; httpx fallback: identity headers are the client defaults)."""
    client = hs.new_client(identity=hs.IDENTITIES["immoscout"])
    try:
        if not hs._HAS_CURL:  # pragma: no cover - exercised only on the fallback path
            assert client.headers["User-Agent"].startswith("ImmoScout24_")
            assert "sec-ch-ua" not in client.headers
        else:
            assert type(client).__module__.startswith("curl_cffi")
    finally:
        asyncio.run(hs.aclose(client))


def test_session_pool_gives_immoscout_its_identity(monkeypatch):
    """The pool builds each source's client with its registered identity/proxy."""
    captured = {}

    def fake_new_client(*, rng=None, identity=None, proxy=None):
        captured[len(captured)] = (identity, proxy)
        return object()  # stand-in client; never used for I/O here

    monkeypatch.setattr(hs, "new_client", fake_new_client)
    monkeypatch.delenv("IMMOSCOUT_PROXY", raising=False)
    monkeypatch.delenv("FLATSNIPER_PROXY_IMMOSCOUT", raising=False)
    pool = hs.SessionPool()
    pool.client_for("immoscout")
    pool.client_for("wg_gesucht")
    assert captured[0] == (hs.IDENTITIES["immoscout"], None)  # app identity, no proxy
    assert captured[1] == (None, None)  # browser source -> default identity


def test_session_pool_reuses_one_client_per_source():
    async def go():
        pool = hs.SessionPool()
        try:
            a1 = pool.client_for("wg_gesucht")
            a2 = pool.client_for("wg_gesucht")
            b1 = pool.client_for("kleinanzeigen")
            assert a1 is a2  # same source -> same (cookie-keeping) client
            assert a1 is not b1  # different source -> different client
        finally:
            await pool.aclose()
        # aclose empties the pool.
        assert pool._clients == {}

    asyncio.run(go())
