"""Criteria matching over a Listing + the search config.

Pure functions, no I/O. Two phases (see docs/ARCHITECTURE.md):

  - Single flats (is_wg == False): must clear the size-tiered rent rule, plus
    area and move-in. Fully decided here, no LLM.
  - WGs (is_wg == True): `passes_prefilter` only checks the cheap structural
    gate (2-person + allowed) plus area/move-in. Whether it's *actually an
    empty* 2-person WG is deferred to the LLM classifier; once classified
    empty, `rent_ok` (the same tiered rule) is applied via `passes_postclassify`.

Move-in semantics: `earliest_move_in` is a floor. A listing is rejected if its
advertised move-in date (`available_from`) is *before* that floor — the user
won't take a place that needs to be occupied earlier. A listing with no known
move-in date is kept (can't verify → don't false-reject). We also still reject
short-term sublets that *end* before the floor (available-until precedes it).
"""

from __future__ import annotations

from datetime import date, datetime

from .config import Config, SearchConfig
from .models import Listing


def rent_ok(listing: Listing, search: SearchConfig) -> bool:
    """Size-tiered rent rule. Qualifies if any tier matches; under min_sqm or
    with unknown size/rent it fails (can't be verified)."""
    if listing.sqm is None or listing.rent_warm is None:
        return False
    if listing.sqm < search.min_sqm:
        return False
    return any(
        listing.sqm >= tier.min_sqm and listing.rent_warm <= tier.max_rent_warm
        for tier in search.rent_tiers
    )


def _location_text(listing: Listing) -> str:
    raw = listing.raw or {}
    fields = [raw.get("city_name"), raw.get("district_custom"), raw.get("postcode"), listing.title]
    return " ".join(str(f) for f in fields if f).lower()


def area_ok(listing: Listing, search: SearchConfig) -> bool:
    """True if the listing's location intersects any configured area. Lenient by
    design: a configured broad area like 'München' matches all of Munich."""
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
    """Reject listings whose move-in is before the floor, or that end before it.

    `available_from` before `earliest_move_in` -> reject (too early to take).
    An unknown move-in date is kept. A sublet ending before the floor is rejected.
    """
    if listing.available_from is not None and listing.available_from < search.earliest_move_in:
        return False
    available_to = _parse_any_date((listing.raw or {}).get("available_to_date"))
    if available_to is not None and available_to < search.earliest_move_in:
        return False
    return True


def passes_prefilter(listing: Listing, config: Config) -> bool:
    """Cheap, LLM-free gate run on every new listing.

    Single flats are fully decided here. WGs pass only the structural gate
    (2-person + allowed) + area/move-in; emptiness + rent are decided later.
    """
    search = config.search
    if not area_ok(listing, search) or not move_in_ok(listing, search):
        return False
    if listing.is_wg:
        return config.allow_empty_2person_wg and listing.wg_size == 2
    return rent_ok(listing, search)


def passes_postclassify(listing: Listing, search: SearchConfig) -> bool:
    """Applied to a WG once the LLM has confirmed it's an empty 2-person WG:
    the same size-tiered rent rule as for single flats."""
    return rent_ok(listing, search)
