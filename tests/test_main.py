"""Tests for the scheduler loop (offline: fake adapters, in-memory store)."""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

from flatsniper import main
from flatsniper.config import Config, RentTier, SearchConfig, Secrets
from flatsniper.models import Listing
from flatsniper.store import Store


def _config(**kw) -> Config:
    search = SearchConfig(
        min_sqm=28,
        rent_tiers=[RentTier(28, 1000), RentTier(40, 1200)],
        min_rooms=1,
        earliest_move_in=date(2026, 8, 1),
        areas=["München"],
        postal_codes=[],
    )
    return Config(
        search=search,
        allow_empty_2person_wg=True,
        sources=kw.get("sources", {"fake": True}),
        poll_interval_seconds=60,
        about_me="",
        secrets=Secrets("t", "c", "k", "m"),
    )


def _flat(external_id="1", sqm=45, rent=1100, **kw) -> Listing:
    base = dict(source="fake", external_id=external_id, url=f"u/{external_id}",
                title="Wohnung München", sqm=sqm, rent_warm=rent, is_wg=False,
                raw={"city_name": "München"})
    base.update(kw)
    return Listing(**base)


def _adapter(name, listings, *, fail=False):
    async def fetch(config, is_new=None, *, limit=None):
        if fail:
            raise RuntimeError("boom")
        items = [l for l in listings if is_new is None or is_new(l.source, l.external_id)]
        return items[:limit] if limit is not None else items
    return SimpleNamespace(name=name, fetch=fetch)


def _collector():
    got = []

    async def notify(listing):
        got.append(listing)

    return got, notify


def _run_cycle(config, store, notify, **kw):
    return asyncio.run(main.run_cycle(config, store, notify, **kw))


def test_match_notified_once_and_dedup_on_second_cycle(monkeypatch):
    store = Store(":memory:")
    good = _flat("1", sqm=45, rent=1100)        # passes
    bad = _flat("2", sqm=30, rent=1050)         # rent too high for 28-tier
    monkeypatch.setattr(main, "ADAPTERS", {"fake": _adapter("fake", [good, bad])})

    got, notify = _collector()
    new1, notified1 = _run_cycle(_config(), store, notify)
    assert (new1, notified1) == (2, 1)
    assert [l.external_id for l in got] == ["1"]

    # Second cycle: both already seen -> nothing new, no re-notify.
    new2, notified2 = _run_cycle(_config(), store, notify)
    assert (new2, notified2) == (0, 0)
    assert len(got) == 1


def test_one_adapter_failing_does_not_stop_others(monkeypatch):
    store = Store(":memory:")
    monkeypatch.setattr(main, "ADAPTERS", {
        "boom": _adapter("boom", [], fail=True),
        "ok": _adapter("ok", [_flat("9")]),
    })
    config = _config(sources={"boom": True, "ok": True})
    got, notify = _collector()
    new, notified = _run_cycle(config, store, notify)
    assert (new, notified) == (1, 1)
    assert got[0].external_id == "9"


def test_disabled_adapter_skipped(monkeypatch):
    store = Store(":memory:")
    monkeypatch.setattr(main, "ADAPTERS", {"fake": _adapter("fake", [_flat("1")])})
    got, notify = _collector()
    new, notified = _run_cycle(_config(sources={"fake": False}), store, notify)
    assert (new, notified) == (0, 0)


def test_wg_classify_gate(monkeypatch):
    store = Store(":memory:")
    wg = _flat("w1", is_wg=True, wg_size=2, sqm=55, rent=950, title="2er WG München")
    monkeypatch.setattr(main, "ADAPTERS", {"fake": _adapter("fake", [wg])})

    # Classifier says NOT empty -> no notify.
    monkeypatch.setattr(main, "classify_empty_2p_wg", lambda l, s: False)
    got, notify = _collector()
    new, notified = _run_cycle(_config(), store, notify)
    assert (new, notified) == (1, 0)
    assert got == []


def test_wg_classified_empty_then_rent_applies(monkeypatch):
    store = Store(":memory:")
    cheap = _flat("w1", is_wg=True, wg_size=2, sqm=55, rent=950, title="2er WG München")
    pricey = _flat("w2", is_wg=True, wg_size=2, sqm=55, rent=1300, title="2er WG München")
    monkeypatch.setattr(main, "ADAPTERS", {"fake": _adapter("fake", [cheap, pricey])})
    monkeypatch.setattr(main, "classify_empty_2p_wg", lambda l, s: True)  # both empty

    got, notify = _collector()
    new, notified = _run_cycle(_config(), store, notify)
    # Both empty 2p WGs, but only the cheap one clears the rent tier.
    assert (new, notified) == (2, 1)
    assert [l.external_id for l in got] == ["w1"]


def test_max_notify_caps_and_marks_rest_seen(monkeypatch):
    store = Store(":memory:")
    listings = [_flat(str(i)) for i in range(5)]  # all match
    monkeypatch.setattr(main, "ADAPTERS", {"fake": _adapter("fake", listings)})
    got, notify = _collector()
    new, notified = _run_cycle(_config(), store, notify, max_notify=2)
    assert (new, notified) == (5, 2)
    # Next cycle: all 5 were marked seen, so nothing re-notified.
    new2, notified2 = _run_cycle(_config(), store, notify, max_notify=2)
    assert (new2, notified2) == (0, 0)
