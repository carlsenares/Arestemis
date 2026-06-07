"""Tests for portal health classification / ban detection (offline)."""

from __future__ import annotations

import asyncio

from flatsniper import health
from flatsniper.health import PortalHealth


def test_classify_ok_requires_marker():
    # 200 + listing marker -> OK (even though WG pages embed a 'captcha' widget).
    assert health.classify("WG", 200, "<div class=offer_list_item> captcha", "offer_list_item").ok
    # 200 without the marker -> not OK.
    r = health.classify("WG", 200, "<html>nothing</html>", "offer_list_item")
    assert not r.ok and "no listings" in r.detail


def test_classify_block_and_rate_limit():
    M = "listing_card"  # marker absent from the bodies below
    assert health.classify("X", 403, "", M).detail == "BLOCKED (403)"
    assert health.classify("X", 429, "", M).detail == "rate-limited (429)"
    r = health.classify("X", 200, "Please verify — datadome captcha", M)
    assert not r.ok and "challenge" in r.detail
    assert health.classify("X", 500, "", M).detail == "HTTP 500"


def test_transitions_only_on_change():
    ok = [PortalHealth("WG", True, "OK")]
    bad = [PortalHealth("WG", False, "BLOCKED (403)")]
    # First check, all good -> silent.
    assert health.transitions({}, ok) == []
    # First check, already broken -> alert.
    assert health.transitions({}, bad) and "WG" in health.transitions({}, bad)[0]
    # Good -> bad transition alerts; bad -> good recovers; steady states silent.
    assert health.transitions({"WG": True}, bad)[0].startswith("⚠️")
    assert health.transitions({"WG": False}, ok)[0].startswith("✅")
    assert health.transitions({"WG": True}, ok) == []
    assert health.transitions({"WG": False}, bad) == []


def test_format_report_header():
    assert health.format_report([PortalHealth("A", True, "OK")]).startswith("🟢")
    mixed = [PortalHealth("A", True, "OK"), PortalHealth("B", False, "BLOCKED (403)")]
    assert health.format_report(mixed).startswith("🔴")
    assert "⛔ B: BLOCKED (403)" in health.format_report(mixed)


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


def test_check_all_uses_provided_clients_and_handles_errors():
    ok_client = _Client(_Resp(200, "x offer_list_item x aditem serp-core-classified-card-testid"))
    err_client = _Client(raises=ConnectionError("boom"))

    def get_client(source):
        return err_client if source == "kleinanzeigen" else ok_client

    results = asyncio.run(health.check_all(get_client=get_client))
    by_name = {r.name: r for r in results}
    assert by_name["WG-Gesucht"].ok is True
    assert by_name["Immowelt"].ok is True
    assert by_name["Kleinanzeigen"].ok is False
    assert "error" in by_name["Kleinanzeigen"].detail
