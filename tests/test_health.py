"""Tests for portal health classification / ban detection (offline)."""

from __future__ import annotations

import asyncio

from flatsniper import health
from flatsniper.health import PortalHealth


def test_classify_keys_on_card_count_not_substring():
    # 200 + real cards -> OK (even if a 'captcha' widget is embedded).
    assert health.classify("WG", "wg_gesucht", 200, "<div> captcha", 12).ok
    # 200 but zero cards -> blocked/stripped, NOT OK — even if marker-ish text is
    # present in the body (the old substring check would have falsely passed).
    r = health.classify("WG", "wg_gesucht", 200, "aditem offer_list_item", 0)
    assert not r.ok and r.state == health.PORTAL_BLOCKED and "no listings" in r.detail


def test_classify_block_rate_limit_challenge_and_error():
    assert health.classify("X", "x", 403, "", 0).detail == "BLOCKED (403)"
    assert health.classify("X", "x", 403, "", 0).state == health.PORTAL_BLOCKED
    assert health.classify("X", "x", 429, "", 0).detail == "rate-limited (429)"
    r = health.classify("X", "x", 200, "Please verify — datadome captcha", 0)
    assert not r.ok and "challenge" in r.detail
    err = health.classify("X", "x", 500, "", 0)
    assert err.state == health.ERROR and err.detail == "HTTP 500"


def test_format_report_header_and_icons():
    assert health.format_report([PortalHealth("A", "a", health.OK, "OK")]).startswith("🟢")
    mixed = [
        PortalHealth("A", "a", health.OK, "OK"),
        PortalHealth("B", "b", health.PORTAL_BLOCKED, "BLOCKED (403)"),
    ]
    report = health.format_report(mixed)
    assert report.startswith("🔴")
    assert "⛔ B: BLOCKED (403)" in report
    assert "✅ A: OK" in report


class _Resp:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


class _Client:
    def __init__(self, resp=None, raises=None):
        self._resp, self._raises = resp, raises

    async def get(self, url, headers=None):
        if self._raises:
            raise self._raises
        return self._resp


# For wiring tests, replace the canaries with trivial counters ("CARD" tokens) so
# we exercise check_all's plumbing without crafting parser-valid HTML per portal.
def _patch_canaries(monkeypatch):
    canaries = [
        health.Canary(s, n, "http://x", lambda b: b.count("CARD"))
        for s, n in [("wg_gesucht", "WG-Gesucht"),
                     ("kleinanzeigen", "Kleinanzeigen"),
                     ("immowelt", "Immowelt")]
    ]
    monkeypatch.setattr(health, "CANARIES", canaries)


def test_check_all_downgrades_when_detail_circuit_open(monkeypatch):
    from flatsniper.sources import http_session

    _patch_canaries(monkeypatch)
    ok_client = _Client(_Resp(200, "CARD CARD CARD"))
    # Force the WG detail breaker open via an injected clock.
    br = http_session.CircuitBreaker("wg_gesucht", threshold=1, cooldown=600, clock=lambda: 0.0)
    br.record_failure()
    monkeypatch.setitem(http_session._BREAKERS, "wg_gesucht", br)

    results = asyncio.run(health.check_all(get_client=lambda s: ok_client))
    wg = next(r for r in results if r.name == "WG-Gesucht")
    assert wg.ok is False and "gedrosselt" in wg.detail
    # Other portals (no open breaker) stay OK.
    assert next(r for r in results if r.name == "Immowelt").ok is True


def test_check_all_uses_provided_clients_and_handles_errors(monkeypatch):
    from flatsniper.sources import http_session

    monkeypatch.setattr(http_session, "_BREAKERS", {})  # no stale open breakers
    _patch_canaries(monkeypatch)
    ok_client = _Client(_Resp(200, "CARD CARD"))
    err_client = _Client(raises=ConnectionError("boom"))

    def get_client(source):
        return err_client if source == "kleinanzeigen" else ok_client

    results = asyncio.run(health.check_all(get_client=get_client))
    by_name = {r.name: r for r in results}
    assert by_name["WG-Gesucht"].ok is True
    assert by_name["Immowelt"].ok is True
    assert by_name["Kleinanzeigen"].ok is False
    assert by_name["Kleinanzeigen"].state == health.ERROR
    assert "error" in by_name["Kleinanzeigen"].detail


def test_check_all_soft_block_is_not_ok(monkeypatch):
    """The core fix: 200 OK with zero cards must read as blocked, not OK."""
    from flatsniper.sources import http_session

    monkeypatch.setattr(http_session, "_BREAKERS", {})
    _patch_canaries(monkeypatch)
    stripped = _Client(_Resp(200, "<html>no cards here</html>"))
    results = asyncio.run(health.check_all(get_client=lambda s: stripped))
    assert all(not r.ok and r.state == health.PORTAL_BLOCKED for r in results)


class _PostClient:
    """Mock whose POST returns a fixed response (the IS24 mobile canary is POST)."""

    def __init__(self, resp):
        self._resp = resp
        self.posted = []

    async def post(self, url, data=None, headers=None):
        self.posted.append((url, data))
        return self._resp


def test_immoscout_canary_is_a_post_app_probe():
    """The registered IS24 canary must POST (its list route reads the query, empty
    body) and carry the app identity so the standalone probe looks like the app."""
    from flatsniper.sources import http_session

    canary = health._BY_SOURCE["immoscout"]
    assert canary.method == "POST"
    assert canary.identity is http_session.IDENTITIES["immoscout"]
    assert canary.url.startswith("https://api.mobile.immobilienscout24.de/search/list?")


def test_check_one_posts_for_immoscout_and_keys_on_items(monkeypatch):
    from flatsniper.sources import http_session

    monkeypatch.setattr(http_session, "_BREAKERS", {})  # no stale breaker downgrade
    canary = health._BY_SOURCE["immoscout"]

    # 200 with real result items -> OK, and we POSTed an empty body.
    ok_client = _PostClient(_Resp(200, '{"resultListItems":[{"type":"EXPOSE_RESULT","item":{"id":"1"}}]}'))
    res = asyncio.run(health._check_one(ok_client, canary))
    assert res.ok and ok_client.posted and ok_client.posted[0][1] == ""

    # 200 with the API's error/catch-all body -> zero cards -> blocked.
    err_body = _PostClient(_Resp(200, '{"error":"what???"}'))
    res = asyncio.run(health._check_one(err_body, canary))
    assert not res.ok and res.state == health.PORTAL_BLOCKED and "no listings" in res.detail

    # 403 (HTML challenge) -> blocked.
    blocked = _PostClient(_Resp(403, "<html>forbidden</html>"))
    res = asyncio.run(health._check_one(blocked, canary))
    assert res.state == health.PORTAL_BLOCKED and "403" in res.detail


def test_session_aware_report_flags_session_block(monkeypatch):
    from flatsniper.sources import http_session

    monkeypatch.setattr(http_session, "_BREAKERS", {})
    _patch_canaries(monkeypatch)
    # Live session: Kleinanzeigen returns 200 but zero cards; others get cards.
    def live_get(source):
        return _Client(_Resp(200, "" if source == "kleinanzeigen" else "CARD CARD"))

    async def fake_probe(source, client=None):
        # A FRESH probe of Kleinanzeigen still gets cards -> it's our session.
        return PortalHealth("Kleinanzeigen", "kleinanzeigen", health.OK, "OK")

    monkeypatch.setattr(health, "probe", fake_probe)
    results = asyncio.run(health.session_aware_report(live_get))
    ka = next(r for r in results if r.source == "kleinanzeigen")
    assert ka.state == health.SESSION_BLOCKED and "Session" in ka.detail
    # The portals that returned cards stay OK.
    assert next(r for r in results if r.source == "immowelt").ok is True
