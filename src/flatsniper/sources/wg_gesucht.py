"""WG-Gesucht adapter.

The website's offer *search* JSON endpoint is locked (405) and the HTML list
cards hide price/size behind a login wall — but each offer's public detail JSON
(`/api/public/offers/{id}`) returns everything without auth. So we use a hybrid:

  1. Fetch the HTML list page per enabled category and scrape the offer IDs +
     human URLs (the only things visible without login).
  2. Hydrate each *new* offer via the detail JSON into a normalized Listing.

`is_new` lets the scheduler hydrate only unseen offers, keeping steady-state
traffic to one list fetch per category plus a detail fetch for new offers only.
Requests are sequential with a small delay — no aggressive parallelism.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
from datetime import date, datetime

import httpx

from ..config import Config
from ..models import Listing
from .base import NewPredicate

name = "wg_gesucht"
log = logging.getLogger(__name__)

BASE = "https://www.wg-gesucht.de"
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"
)
DEFAULT_CITY_ID = 90  # München
CITY_SLUG = "Muenchen"
DETAIL_DELAY_SECONDS = 0.3  # politeness gap between detail fetches

# WG-Gesucht category id -> (URL slug, is_wg)
CATEGORIES: dict[int, tuple[str, bool]] = {
    0: ("wg-zimmer", True),
    1: ("1-zimmer-wohnungen", False),
    2: ("wohnungen", False),
    3: ("haeuser", False),
}

# Card opening tag: class="wgg_card offer_list_item ..." data-id="12345"
_CARD_RE = re.compile(r'offer_list_item[^"]*"\s+data-id="(\d+)"')


def _enabled_categories(config: Config) -> list[int]:
    """Single flats (1-room + flat) always; WG rooms only if the rule allows it."""
    cats = [1, 2]
    if config.allow_empty_2person_wg:
        cats = [0, *cats]
    return cats


def _list_url(category: int, city_id: int) -> str:
    slug = CATEGORIES[category][0]
    # {slug}-in-{City}.{city_id}.{category}.{rent_type=1}.{page=0}.html
    return f"{BASE}/{slug}-in-{CITY_SLUG}.{city_id}.{category}.1.0.html"


def _detail_url(offer_id: str) -> str:
    return f"{BASE}/api/public/offers/{offer_id}"


def _to_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_de_date(s: str | None) -> date | None:
    if not s or s.startswith("00."):
        return None
    try:
        return datetime.strptime(s, "%d.%m.%Y").date()
    except ValueError:
        return None


def _parse_de_datetime(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%d.%m.%Y, %H:%M:%S")
    except ValueError:
        return None


def _warm_rent(detail: dict) -> int | None:
    """Prefer total_costs; otherwise sum the components we have."""
    total = _to_int(detail.get("total_costs"))
    if total:
        return total
    parts = [
        _to_int(detail.get("rent_costs")),
        _to_int(detail.get("utility_costs")),
        _to_int(detail.get("other_costs")),
    ]
    summed = sum(p for p in parts if p)
    return summed or None


def _description(detail: dict) -> str:
    blocks = [
        detail.get("freetext_property_description"),
        detail.get("freetext_flatshare"),
        detail.get("freetext_area_description"),
        detail.get("freetext_other"),
    ]
    return "\n\n".join(b.strip() for b in blocks if b and b.strip())


def parse_listing(detail: dict, url: str) -> Listing:
    """Normalize a WG-Gesucht detail JSON payload into a Listing."""
    category = _to_int(detail.get("category"))
    is_wg = category == 0
    return Listing(
        source=name,
        external_id=str(detail.get("offer_id")),
        url=url,
        title=(detail.get("offer_title") or "").strip(),
        rent_warm=_warm_rent(detail),
        rooms=_to_float(detail.get("number_of_rooms")) or None,
        sqm=_to_float(detail.get("property_size")),
        is_wg=is_wg,
        wg_size=_to_int(detail.get("flatshare_inhabitants_total")) if is_wg else None,
        description=_description(detail),
        posted_at=_parse_de_datetime(detail.get("date_created")),
        available_from=_parse_de_date(detail.get("available_from_date")),
        raw=detail,
    )


def _scrape_offer_ids(html: str) -> list[str]:
    """Offer IDs from a list page, de-duplicated, order preserved (newest first)."""
    seen: set[str] = set()
    out: list[str] = []
    for oid in _CARD_RE.findall(html):
        if oid not in seen:
            seen.add(oid)
            out.append(oid)
    return out


def _offer_url(html: str, offer_id: str) -> str:
    """The human-facing listing URL for an id, scraped from the list page."""
    m = re.search(rf'href="(/[^"]*\.{offer_id}\.html)"', html)
    return BASE + m.group(1) if m else f"{BASE}/{offer_id}.html"


async def _fetch_list(client: httpx.AsyncClient, category: int, city_id: int) -> list[tuple[str, str]]:
    resp = await client.get(_list_url(category, city_id))
    resp.raise_for_status()
    html = resp.text
    return [(oid, _offer_url(html, oid)) for oid in _scrape_offer_ids(html)]


async def _fetch_detail(client: httpx.AsyncClient, offer_id: str) -> dict:
    resp = await client.get(_detail_url(offer_id), headers={"X-Client-Id": "wg_desktop_website"})
    resp.raise_for_status()
    return resp.json()


async def fetch(
    config: Config,
    is_new: NewPredicate | None = None,
    *,
    city_id: int = DEFAULT_CITY_ID,
    limit: int | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[Listing]:
    """Fetch + normalize current WG-Gesucht offers for the configured search."""
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
        )

    try:
        # 1) Enumerate offer ids across enabled categories (one list fetch each).
        stubs: list[tuple[str, str]] = []
        seen_ids: set[str] = set()
        for category in _enabled_categories(config):
            try:
                for offer_id, url in await _fetch_list(client, category, city_id):
                    if offer_id not in seen_ids:
                        seen_ids.add(offer_id)
                        stubs.append((offer_id, url))
            except Exception:
                log.exception("WG-Gesucht: failed to list category %s", category)

        # 2) Skip already-seen offers (politeness) before hydrating.
        if is_new is not None:
            stubs = [s for s in stubs if is_new(name, s[0])]
        if limit is not None:
            stubs = stubs[:limit]

        # 3) Hydrate each via the detail JSON, sequentially.
        listings: list[Listing] = []
        for index, (offer_id, url) in enumerate(stubs):
            if index:
                await asyncio.sleep(DETAIL_DELAY_SECONDS)
            try:
                detail = await _fetch_detail(client, offer_id)
                listings.append(parse_listing(detail, url))
            except Exception:
                log.exception("WG-Gesucht: failed to hydrate offer %s", offer_id)

        log.info("WG-Gesucht: %d offers normalized (%d enumerated)", len(listings), len(seen_ids))
        return listings
    finally:
        if own_client:
            await client.aclose()


def _main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="flatsniper.sources.wg_gesucht")
    parser.add_argument("--once", action="store_true", help="Run one live query and print results.")
    parser.add_argument("--limit", type=int, default=5, help="Max offers to hydrate (politeness).")
    args = parser.parse_args(argv)

    if not args.once:
        parser.print_help()
        return 0

    from ..config import load_config

    config = load_config()
    listings = asyncio.run(fetch(config, limit=args.limit))
    for listing in listings:
        kind = f"WG/{listing.wg_size}p" if listing.is_wg else "Flat"
        print(
            f"[{kind}] {listing.rent_warm}€ · {listing.sqm:g}m² · {listing.rooms or '?'}Zi · "
            f"{(listing.raw.get('district_custom') or listing.raw.get('city_name') or '')}\n"
            f"   {listing.title[:70]}\n   {listing.url}"
        )
    print(f"\n{len(listings)} normalized listing(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
