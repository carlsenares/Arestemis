"""Tests for the SQLite seen-listings store (Milestone 1 acceptance + extras)."""

from __future__ import annotations

from flatsniper.models import Listing
from flatsniper.store import Store


def _listing(source: str = "wg_gesucht", external_id: str = "abc123") -> Listing:
    return Listing(
        source=source,
        external_id=external_id,
        url="https://example.com/" + external_id,
        title="Schöne 2-Zimmer-Wohnung",
        rent_warm=950,
        rooms=2,
        sqm=45,
    )


def test_insert_then_not_new(tmp_path):
    """Acceptance: insert a listing, and the second is_new returns False."""
    store = Store(tmp_path / "t.db")
    listing = _listing()

    assert store.is_new(listing.source, listing.external_id) is True
    store.mark_seen(listing)
    assert store.is_new(listing.source, listing.external_id) is False


def test_keyed_by_source_and_id(tmp_path):
    """Same external_id on a different source is still new."""
    store = Store(tmp_path / "t.db")
    store.mark_seen(_listing(source="wg_gesucht", external_id="X"))

    assert store.is_new("wg_gesucht", "X") is False
    assert store.is_new("kleinanzeigen", "X") is True


def test_mark_seen_is_idempotent(tmp_path):
    store = Store(tmp_path / "t.db")
    listing = _listing()
    store.mark_seen(listing)
    store.mark_notified(listing)
    store.mark_seen(listing)  # re-seeing must not reset notified

    assert store.was_notified(listing.source, listing.external_id) is True


def test_mark_notified(tmp_path):
    store = Store(tmp_path / "t.db")
    listing = _listing()

    assert store.was_notified(listing.source, listing.external_id) is False
    store.mark_notified(listing)
    assert store.was_notified(listing.source, listing.external_id) is True
    # mark_notified without a prior mark_seen still records the row.
    assert store.is_new(listing.source, listing.external_id) is False


def test_persists_across_reopen(tmp_path):
    """A restart must not re-notify old listings."""
    db = tmp_path / "t.db"
    listing = _listing()

    with Store(db) as store:
        store.mark_seen(listing)
        store.mark_notified(listing)

    with Store(db) as store2:
        assert store2.is_new(listing.source, listing.external_id) is False
        assert store2.was_notified(listing.source, listing.external_id) is True


def test_meta_get_set_and_persists(tmp_path):
    """Meta key/value (e.g. the paused flag) round-trips and survives a restart."""
    db = tmp_path / "t.db"
    with Store(db) as store:
        assert store.get_meta("scanning_paused", "0") == "0"  # default
        store.set_meta("scanning_paused", "1")
        store.set_meta("scanning_paused", "1")  # upsert, no error
        assert store.get_meta("scanning_paused") == "1"

    with Store(db) as store2:
        assert store2.get_meta("scanning_paused") == "1"
