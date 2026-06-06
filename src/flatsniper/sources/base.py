"""Source adapter contract.

An adapter is a *module* exposing:
  - `name: str`               — e.g. "wg_gesucht"
  - `async def fetch(config, is_new=None) -> list[Listing]`

`fetch` returns normalized Listing objects for the configured search. Adding a
portal = adding one such module. The `SourceAdapter` Protocol below documents
the shape (and lets call sites type-hint a module structurally).

The optional `is_new` predicate is a *politeness lever*: when an adapter has to
make a follow-up request per listing to fully normalize it (WG-Gesucht hydrates
each offer via its detail API), passing `store.is_new` lets the adapter skip
listings it has already seen, so steady-state traffic is one cheap list fetch
plus a detail fetch only for genuinely new offers. Adapters that can normalize
without per-listing requests may ignore it. Returned listings still flow through
the normal dedup/filter pipeline in main.py.
"""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

from ..config import Config
from ..models import Listing

# (source, external_id) -> True if not seen before.
NewPredicate = Callable[[str, str], bool]


@runtime_checkable
class SourceAdapter(Protocol):
    name: str

    async def fetch(
        self, config: Config, is_new: NewPredicate | None = None
    ) -> list[Listing]:
        """Return current listings for the configured search (first page is enough)."""
        ...
