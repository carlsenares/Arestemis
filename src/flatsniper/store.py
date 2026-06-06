"""SQLite seen-listings store.

One table, `seen_listings(source, external_id, first_seen, notified)`, keyed by
`(source, external_id)`. Its only job is to know what's new and avoid duplicate
pings — no ORM, no listing payload stored. Schema is created on first use.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Listing

# Repo root = two levels up from src/flatsniper/store.py. DB lives under data/
# (git-ignored). ":memory:" is honored for tests.
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "flatsniper.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_listings (
    source      TEXT    NOT NULL,
    external_id TEXT    NOT NULL,
    first_seen  TEXT    NOT NULL,
    notified    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, external_id)
);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """SQLite-backed record of listings we've already seen / notified about."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False so the async scheduler can share one Store.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def is_new(self, source: str, external_id: str) -> bool:
        """True if we have never recorded this (source, external_id)."""
        row = self._conn.execute(
            "SELECT 1 FROM seen_listings WHERE source = ? AND external_id = ?",
            (source, external_id),
        ).fetchone()
        return row is None

    def mark_seen(self, listing: Listing) -> None:
        """Record a listing as seen. Idempotent — re-seeing keeps the original
        first_seen and notified state."""
        self._conn.execute(
            "INSERT OR IGNORE INTO seen_listings (source, external_id, first_seen, notified) "
            "VALUES (?, ?, ?, 0)",
            (listing.source, listing.external_id, _utcnow_iso()),
        )
        self._conn.commit()

    def mark_notified(self, listing: Listing) -> None:
        """Flag that the user has been pinged about this listing. Inserts the
        row first if needed, so it's safe to call without a prior mark_seen."""
        self._conn.execute(
            "INSERT OR IGNORE INTO seen_listings (source, external_id, first_seen, notified) "
            "VALUES (?, ?, ?, 0)",
            (listing.source, listing.external_id, _utcnow_iso()),
        )
        self._conn.execute(
            "UPDATE seen_listings SET notified = 1 WHERE source = ? AND external_id = ?",
            (listing.source, listing.external_id),
        )
        self._conn.commit()

    def was_notified(self, source: str, external_id: str) -> bool:
        """True if this listing has already been notified about."""
        row = self._conn.execute(
            "SELECT notified FROM seen_listings WHERE source = ? AND external_id = ?",
            (source, external_id),
        ).fetchone()
        return bool(row["notified"]) if row else False

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
