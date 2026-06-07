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
        self,
        config: Config,
        is_new: NewPredicate | None = None,
        *,
        limit: int | None = None,
        client=None,
    ) -> list[Listing]:
        """Return current listings for the configured search (first page is enough).

        `limit` caps how many NEW listings are hydrated this call — the scheduler
        passes it to bound per-cycle request rate. Listings beyond the cap are
        simply not returned (and so not marked seen), to be picked up next cycle.

        `client` is an optional pre-built `httpx.AsyncClient` (the scheduler passes
        a long-lived, cookie-keeping one from `http_session.SessionPool` so polling
        looks like one returning browser session — the anti-ban layer). When None,
        the adapter should build its own via `http_session.new_client()` and close
        it. Adapters that do their own HTTP should route it through `http_session`
        so they inherit the shared browser-like headers / jitter.
        """
        ...
