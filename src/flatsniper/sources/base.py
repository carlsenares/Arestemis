"""Source adapter interface.

Each portal adapter implements `fetch()` and returns normalized Listing objects.
Adding a portal = adding one file implementing this Protocol.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..config import Config
from ..models import Listing


@runtime_checkable
class SourceAdapter(Protocol):
    name: str  # e.g. "wg_gesucht"

    async def fetch(self, config: Config) -> list[Listing]:
        """Return current listings for the configured search (first page is enough)."""
        ...
