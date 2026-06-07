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
