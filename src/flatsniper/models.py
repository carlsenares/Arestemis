"""Domain models. Implemented in Milestone 1."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class Listing:
    """A normalized listing from any source adapter (see docs/ARCHITECTURE.md)."""

    source: str  # "wg_gesucht" | "kleinanzeigen"
    external_id: str  # unique per source
    url: str
    title: str
    rent_warm: int | None = None
    rooms: float | None = None
    sqm: float | None = None
    is_wg: bool = False
    wg_size: int | None = None
    description: str = ""
    posted_at: datetime | None = None
    # Earliest move-in date the listing advertises. Used by the move-in filter
    # (Milestone 4). Not in the original ARCHITECTURE field list, but required to
    # honor `search.earliest_move_in`.
    available_from: date | None = None
    raw: dict = field(default_factory=dict)
