"""Criteria matching over a Listing + the search config.

Pure functions, no I/O. Two phases (see docs/ARCHITECTURE.md):

  - Single flats (is_wg == False): must clear the size-tiered rent rule, plus
    area and move-in. Fully decided here, no LLM.
  - WGs (is_wg == True): what happens depends on `wg_rule.mode`. Under `any` a
    WG room is judged exactly like a flat — rent + area + move-in, no LLM. Under
    `empty_2person`, `passes_prefilter` only checks the cheap structural gate
    (2-person + allowed) plus area/move-in; whether it's *actually an empty*
    2-person WG is deferred to the LLM classifier, and once classified empty,
    `rent_ok` (the same tiered rule) is applied via `passes_postclassify`.
    Under `none`, WGs never match.

Move-in semantics: `earliest_move_in` and `latest_move_in` bracket a *window*,
not an open-ended floor. A listing is rejected if its advertised move-in date
(`available_from`) falls before the start — the user won't take a place that
needs to be occupied earlier — or after the end, since the start date is when
they actually need to move in, not just the soonest they'd accept. Leaving
`latest_move_in` unset restores the open-ended behaviour. A listing with no known
move-in date is kept (can't verify → don't false-reject). We also still reject
short-term sublets that *end* before the window (available-until precedes it).
"""

from __future__ import annotations

import re
from datetime import date, datetime

from .config import WG_ANY, WG_EMPTY_2P, Config, SearchConfig
from .models import Listing


def rent_ok(listing: Listing, search: SearchConfig) -> bool:
    """Size-tiered rent rule. Qualifies if any tier matches.

    Unknown data only rejects where it's actually load-bearing: an unknown size
    can't clear a size floor or a size-gated tier, and an unknown rent can't
    clear a price cap. With no size requirement at all (min_sqm 0 and a tier
    that starts at 0 m²), a listing whose m² we couldn't parse is still judged
    on price alone rather than dropped — otherwise "no minimum square meters"
    would quietly reject every listing with an unparseable size.
    """
    if search.min_sqm > 0 and (listing.sqm is None or listing.sqm < search.min_sqm):
        return False
    # "No size/price filter" mode: a zero floor with effectively unbounded rent
    # means every flat qualifies regardless of size/price — including ones whose
    # size or rent we couldn't parse. Short-circuit so missing data doesn't reject.
    if search.min_sqm <= 0 and all(t.max_rent_warm >= 1_000_000 for t in search.rent_tiers):
        return True
    if listing.rent_warm is None:
        return False
    # A tier with no size floor needs nothing but the rent.
    if any(
        tier.min_sqm <= 0 and listing.rent_warm <= tier.max_rent_warm
        for tier in search.rent_tiers
    ):
        return True
    if listing.sqm is None:
        return False
    return any(
        listing.sqm >= tier.min_sqm and listing.rent_warm <= tier.max_rent_warm
        for tier in search.rent_tiers
    )


def is_blocked(listing: Listing, search: SearchConfig) -> bool:
    """True if the listing matches a blocklist keyword (swaps/sublets/temporary).
    Deterministic backstop to the LLM send-gate (filter.passes_prefilter) so
    explicit Tausch-/Unter-/Zwischenmiete listings are rejected even when the LLM
    is unavailable. Matches case-insensitively over title + description."""
    if not search.blocklist:
        return False
    text = f"{listing.title or ''} {listing.description or ''}".lower()
    return any(kw.lower() in text for kw in search.blocklist)


def _location_text(listing: Listing) -> str:
    raw = listing.raw or {}
    fields = [raw.get("city_name"), raw.get("district_custom"), raw.get("postcode"), listing.title]
    return " ".join(str(f) for f in fields if f).lower()


def is_excluded_area(listing: Listing, search: SearchConfig) -> bool:
    """True if the location names an excluded district / town / PLZ.

    `areas` is deliberately broad (one entry, "München", is what scopes the
    portal queries), so the outskirts are carved out here instead. Matching is
    whole-word — a substring test would read "Dachauer Straße" (Maxvorstadt) as
    Dachau and "Pasinger Straße" as Pasing, silently dropping central listings.

    The URL is searched alongside the location fields because it is often the
    only place the district appears: WG-Gesucht reports `city_name` as a flat
    "München" for every offer, but slugs the district into the link
    (`/wg-zimmer-in-Muenchen-Pasing.123.html`). Exclusion-only — widening the
    *include* side with URL text would let a link slug vouch for an area.
    """
    if not search.exclude_areas:
        return False
    loc = f"{_location_text(listing)} {listing.url or ''}".lower()
    return any(
        re.search(rf"\b{re.escape(token.lower().strip())}\b", loc)
        for token in search.exclude_areas
        if token.strip()
    )


def area_ok(listing: Listing, search: SearchConfig) -> bool:
    """True if the listing's location intersects any configured area and isn't
    excluded. Lenient by design: a configured broad area like 'München' matches
    all of Munich, minus whatever `exclude_areas` carves back out."""
    if is_excluded_area(listing, search):
        return False
    if not search.areas:
        return True
    loc = _location_text(listing)
    for area in search.areas:
        a = area.lower().strip()
        # Try the full name and a couple of distinctive head tokens so
        # "Garching bei München" matches a "Garching b. München" location.
        candidates = {a, a.split(" bei ")[0].strip(), a.split("-")[0].strip()}
        if any(tok and tok in loc for tok in candidates):
            return True
    return False


def _parse_any_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s or s.startswith("00."):
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def move_in_ok(listing: Listing, search: SearchConfig) -> bool:
    """Keep listings whose move-in falls inside the configured window.

    `available_from` before `earliest_move_in` -> reject (too early to take).
    `available_from` after `latest_move_in` -> reject: the target date is when
    the user actually needs to move, not merely the earliest they'd accept, so a
    room that only frees up two months later is no use. An unknown move-in date
    is still kept (can't verify -> don't false-reject). A sublet ending before
    the window is rejected.
    """
    if listing.available_from is not None:
        if listing.available_from < search.earliest_move_in:
            return False
        if search.latest_move_in is not None and listing.available_from > search.latest_move_in:
            return False
    available_to = _parse_any_date((listing.raw or {}).get("available_to_date"))
    if available_to is not None and available_to < search.earliest_move_in:
        return False
    return True


def passes_prefilter(listing: Listing, config: Config) -> bool:
    """Cheap, LLM-free gate run on every new listing.

    Single flats are fully decided here. A WG is too under `wg_mode: any` — an
    ordinary room needs no classification, so the rent rule applies right away.
    Only `empty_2person` defers: there the structural gate (2-person + allowed)
    passes and emptiness + rent are settled after the LLM has read the text.
    """
    search = config.search
    if is_blocked(listing, search):
        return False
    if not area_ok(listing, search) or not move_in_ok(listing, search):
        return False
    if listing.is_wg:
        if config.wg_mode == WG_ANY:
            return rent_ok(listing, search)
        return config.wg_mode == WG_EMPTY_2P and listing.wg_size == 2
    return rent_ok(listing, search)


def passes_postclassify(listing: Listing, search: SearchConfig) -> bool:
    """Applied to a WG once the LLM has confirmed it's an empty 2-person WG:
    the same size-tiered rent rule as for single flats."""
    return rent_ok(listing, search)
