"""Kleinanzeigen adapter (HTML + BeautifulSoup).

Kleinanzeigen has no public listing API, so we scrape the "Mietwohnungen"
category (`c203`, offers only, newest first). The list page is cheap and already
carries most fields — id, URL, title, **Kaltmiete** (cold rent), m², rooms, PLZ —
but warm rent and the description live only on each (heavy ~260 KB) detail page.

To stay polite (the dominant anti-ban lever — see `http_session`), we hydrate as
few detail pages as possible:

  1. Fetch the list page per location and parse every offer card.
  2. Drop cards that *provably* can't match from card data alone — too small, or
     a cold rent already above the highest tier ceiling (warm >= cold, so this
     never drops a real match). Drop already-seen offers via `is_new`.
  3. Hydrate only the survivors via their detail page into a normalized Listing.

So steady-state traffic is one list fetch per location plus a detail fetch only
for genuinely-new, plausibly-matching offers. Requests are sequential with a
jittered gap, and all HTTP goes through `http_session` for browser-like headers,
HTTP/2 and a persistent cookie session.

NB: Kleinanzeigen emits malformed numeric char refs (e.g. `&#8203Foo` with no
semicolon) that crash Python's stdlib `html.parser`; `_clean_entities` repairs
them before parsing so we need no third-party parser.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime

import httpx
from bs4 import BeautifulSoup

from ..config import Config
from ..models import Listing
from . import http_session
from .base import NewPredicate

name = "kleinanzeigen"
log = logging.getLogger(__name__)

BASE = "https://www.kleinanzeigen.de"
CATEGORY = "c203"  # "Mietwohnungen" (apartments to rent — offers, not Gesuche)
DETAIL_DELAY_SECONDS = 0.8  # base politeness gap between detail fetches (jittered)
DETAIL_DELAY_JITTER = 0.5  # ±50% so the gap isn't a fixed metronome
DETAIL_MAX_RETRIES = 2  # retries on HTTP 429
RATE_LIMIT_BACKOFF_SECONDS = 5.0  # base backoff when no Retry-After header

# Kleinanzeigen location ids + URL slugs. Garching b. München (TUM campus) is the
# priority area; München is the broader fallback. config.search.areas are mapped
# to these via alias substrings; query order follows config.areas (Garching is
# listed first there by design), mirroring the WG-Gesucht adapter.
LOCATIONS: dict[int, dict] = {
    6303: {"slug": "garching-b-muenchen", "aliases": ("garching",)},
    6411: {
        "slug": "muenchen",
        "aliases": (
            "münchen", "muenchen", "munchen", "munich",
            "freimann", "studentenstadt", "schwabing", "maxvorstadt",
        ),
    },
}
DEFAULT_LOCATION_ORDER = [6303, 6411]  # Garching first

# Card opening tag carries the offer id: <article class="aditem" data-adid="...">
_M2_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m²")
_ROOMS_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*Zi")
_PLZ_RE = re.compile(r"\b(\d{5})\b")
_DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")

_GERMAN_MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5,
    "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10,
    "november": 11, "dezember": 12,
}


# ---------------------------------------------------------------------------
# Parsing helpers (pure, no I/O)
# ---------------------------------------------------------------------------


def _clean_entities(html: str) -> str:
    """Repair *decimal* numeric char refs missing the trailing ';' (Kleinanzeigen
    emits these, e.g. `&#8203Foo`), which otherwise crash stdlib html.parser.

    Decimal-only on purpose: a hex ref's terminator is ambiguous (the letters
    after `&#x200b` could be more hex digits or following text, e.g. "Bar"), so
    auto-repairing hex could corrupt valid markup. Kleinanzeigen only emits the
    decimal form in practice, and well-formed refs (already `;`-terminated) are
    left untouched by the negative lookahead.
    """
    return re.sub(r"&#(\d+)(?![\d;])", r"&#\1;", html)


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(_clean_entities(html), "html.parser")


def _money(text: str | None) -> int | None:
    """`'2.100 €'` -> 2100. Drops '.' thousands and ',dd' cents. 'VB'/'' -> None."""
    if not text:
        return None
    m = re.search(r"\d[\d.]*", text)
    if not m:
        return None
    try:
        return int(m.group(0).replace(".", ""))
    except ValueError:
        return None


def _num(text: str | None) -> float | None:
    """First number in `text` as float ('100 m²' -> 100.0, '2,5' -> 2.5)."""
    if not text:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group(0).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _parse_avail(value: str | None) -> date | None:
    """`'Juli 2026'` -> 2026-07-01; also accepts dd.mm.yyyy. Else None."""
    if not value:
        return None
    m = _DATE_RE.search(value)
    if m:
        try:
            return datetime.strptime(m.group(1), "%d.%m.%Y").date()
        except ValueError:
            pass
    mm = re.search(r"([A-Za-zÄÖÜäöü]+)\s+(\d{4})", value)
    if mm:
        month = _GERMAN_MONTHS.get(mm.group(1).lower())
        if month:
            return date(int(mm.group(2)), month, 1)
    return None


@dataclass(frozen=True)
class CardStub:
    external_id: str
    url: str
    title: str
    cold_rent: int | None
    sqm: float | None
    rooms: float | None
    plz: str | None
    location: str | None  # full card location text, e.g. "80538 München/Lehel"


def _parse_cards(html: str) -> list[CardStub]:
    """Offer cards from a list page, de-duplicated, order preserved (newest first)."""
    soup = _soup(html)
    out: list[CardStub] = []
    seen: set[str] = set()
    for art in soup.select("article.aditem"):
        oid = art.get("data-adid")
        href = art.get("data-href")
        if not oid or oid in seen:
            continue
        seen.add(oid)
        title_el = art.select_one(".aditem-main--middle--description")
        price_el = art.select_one(".aditem-main--middle--price-shipping--price")
        tags_el = art.select_one(".aditem-main--middle--tags")
        loc_el = art.select_one(".aditem-main--top--left")
        tags = tags_el.get_text(" ", strip=True) if tags_el else ""
        loc = loc_el.get_text(" ", strip=True) if loc_el else ""
        m2 = _M2_RE.search(tags)
        zi = _ROOMS_RE.search(tags)
        plz = _PLZ_RE.search(loc)
        out.append(
            CardStub(
                external_id=oid,
                url=BASE + href if href else f"{BASE}/s-anzeige/{oid}",
                title=re.sub(r"\s+", " ", title_el.get_text(strip=True)) if title_el else "",
                cold_rent=_money(price_el.get_text(strip=True) if price_el else None),
                sqm=_num(m2.group(0)) if m2 else None,
                rooms=_num(zi.group(0)) if zi else None,
                plz=plz.group(1) if plz else None,
                location=re.sub(r"\s+", " ", loc) or None,
            )
        )
    return out


def count_list_cards(html: str) -> int:
    """Number of real offer cards on a list page. The health watchdog uses this
    to tell "served real listings" from "200 but the listings were stripped"
    (a soft block) — a substring marker can't: e.g. "aditem" appears hundreds of
    times in a healthy page's CSS/JS, so its mere presence proves nothing."""
    return len(_parse_cards(html))


def _card_plausible(stub: CardStub, config: Config) -> bool:
    """Cheap card-level pre-screen: keep unless card data *proves* it can't match.

    Only rejects on positive evidence (too small, or cold rent already over every
    tier). Since warm rent >= cold rent, a card that fails here can never pass the
    real warm-rent filter, so no true match is dropped. Unknown size/price ->
    keep (hydrate to verify). Intentionally self-contained (no filter.py import).
    """
    search = config.search
    if stub.sqm is not None and stub.sqm < search.min_sqm:
        return False
    if stub.cold_rent is not None:
        # Cold rent must clear at least one tier on size+ceiling to be worth
        # hydrating (warm only goes up from here).
        if not any(
            stub.sqm is not None
            and stub.sqm >= tier.min_sqm
            and stub.cold_rent <= tier.max_rent_warm
            for tier in search.rent_tiers
        ):
            # If size is unknown we can't apply the size leg; fall back to the
            # loosest test (cold over the highest ceiling can never match).
            if stub.sqm is None:
                return stub.cold_rent <= max(t.max_rent_warm for t in search.rent_tiers)
            return False
    return True


def _detail_pairs(soup: BeautifulSoup) -> dict[str, str]:
    """Label -> value from the detail page's attribute list."""
    pairs: dict[str, str] = {}
    for li in soup.select(".addetailslist--detail"):
        value_el = li.select_one(".addetailslist--detail--value")
        if not value_el:
            continue
        label = "".join(
            t for t in li.find_all(string=True, recursive=False)
        ).strip()
        if label:
            pairs[label] = value_el.get_text(strip=True)
    return pairs


def _warm_rent(pairs: dict[str, str], card_cold: int | None) -> int | None:
    """Prefer Warmmiete; else Kaltmiete + Nebenkosten; else the card's cold rent."""
    warm = _money(pairs.get("Warmmiete"))
    if warm:
        return warm
    kalt = _money(pairs.get("Kaltmiete")) or card_cold
    neben = _money(pairs.get("Nebenkosten"))
    if kalt and neben:
        return kalt + neben
    return kalt or None


def _parse_detail(html: str) -> dict:
    """Normalize a detail page into a flat dict of the fields we use."""
    soup = _soup(html)
    pairs = _detail_pairs(soup)
    desc_el = soup.select_one("#viewad-description-text")
    extra_el = soup.select_one("#viewad-extra-info")
    posted = _DATE_RE.search(extra_el.get_text(" ", strip=True)) if extra_el else None
    return {
        "pairs": pairs,
        "description": desc_el.get_text("\n", strip=True) if desc_el else "",
        "posted_at": (
            datetime.strptime(posted.group(1), "%d.%m.%Y") if posted else None
        ),
    }


def parse_listing(stub: CardStub, detail: dict) -> Listing:
    """Combine a card stub + parsed detail page into a normalized Listing."""
    pairs = detail.get("pairs", {})
    sqm = _num(pairs.get("Wohnfläche")) or stub.sqm
    rooms = _num(pairs.get("Zimmer")) or stub.rooms
    return Listing(
        source=name,
        external_id=stub.external_id,
        url=stub.url,
        title=stub.title,
        rent_warm=_warm_rent(pairs, stub.cold_rent),
        rooms=rooms,
        sqm=sqm,
        is_wg=False,  # this category is whole apartments; WGs come via WG-Gesucht
        wg_size=None,
        description=detail.get("description", ""),
        posted_at=detail.get("posted_at"),
        available_from=_parse_avail(pairs.get("Verfügbar ab")),
        # Keys filter.area_ok looks at (city_name carries the town name so
        # "münchen"/"garching" match), plus the raw payload for debugging.
        raw={"postcode": stub.plz, "city_name": stub.location, "detail": pairs},
    )


# ---------------------------------------------------------------------------
# Location resolution + URLs
# ---------------------------------------------------------------------------


def _match_location(area: str) -> int | None:
    a = area.lower()
    for loc_id, meta in LOCATIONS.items():
        if any(tok in a for tok in meta["aliases"]):
            return loc_id
    return None


def _resolve_locations(config: Config) -> list[tuple[int, str]]:
    """Map config.search.areas -> ordered, de-duplicated (loc_id, slug) pairs.
    Order follows config.areas, so Garching (listed first) is queried first."""
    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    for area in config.search.areas:
        loc_id = _match_location(area)
        if loc_id is not None and loc_id not in seen:
            seen.add(loc_id)
            out.append((loc_id, LOCATIONS[loc_id]["slug"]))
    if not out:
        out = [(lid, LOCATIONS[lid]["slug"]) for lid in DEFAULT_LOCATION_ORDER]
    return out


def _list_url(loc_id: int, slug: str) -> str:
    return (
        f"{BASE}/s-wohnung-mieten/anzeige:angebote/sortierung:neueste/"
        f"{slug}/{CATEGORY}l{loc_id}"
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


async def _get(
    client: httpx.AsyncClient, url: str, headers: dict
) -> httpx.Response:
    """GET with jittered HTTP 429 backoff+retry, then raise_for_status."""
    for attempt in range(DETAIL_MAX_RETRIES + 1):
        resp = await client.get(url, headers=headers)
        if resp.status_code == 429 and attempt < DETAIL_MAX_RETRIES:
            retry_after = resp.headers.get("Retry-After")
            wait = (
                float(retry_after)
                if retry_after and retry_after.isdigit()
                else http_session.jitter(RATE_LIMIT_BACKOFF_SECONDS * (attempt + 1))
            )
            log.warning("Kleinanzeigen: 429 on %s, backing off %.1fs", url, wait)
            await asyncio.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


async def fetch(
    config: Config,
    is_new: NewPredicate | None = None,
    *,
    locations: list[tuple[int, str]] | None = None,
    limit: int | None = None,
    client: httpx.AsyncClient | None = None,
    stats: dict | None = None,
) -> list[Listing]:
    """Fetch + normalize current Kleinanzeigen offers for the configured search.

    When `stats` is given, the raw enumerated-card count is recorded as
    `stats["enumerated"]` so the scheduler's scrape watchdog can detect a soft
    block (200 OK but zero cards) regardless of how many survive filtering."""
    own_client = client is None
    if own_client:
        # No injected session (e.g. the --once CLI / tests): build a one-shot
        # polite client. The scheduler instead passes a long-lived client so
        # cookies + profile persist across cycles (see http_session.SessionPool).
        client = http_session.new_client()
    locations = locations or _resolve_locations(config)

    try:
        # 1) Enumerate offer cards across locations (Garching first), dedup by id.
        stubs: list[CardStub] = []
        seen_ids: set[str] = set()
        for loc_id, slug in locations:
            try:
                resp = await _get(
                    client,
                    _list_url(loc_id, slug),
                    headers=http_session.navigation_headers(referer=f"{BASE}/"),
                )
                for stub in _parse_cards(resp.text):
                    if stub.external_id not in seen_ids:
                        seen_ids.add(stub.external_id)
                        stubs.append(stub)
            except Exception:
                log.exception("Kleinanzeigen: list failed loc=%s", loc_id)

        # 2) Skip already-seen, then card-level pre-screen (avoid hydrating
        #    offers that provably can't match), then bound per-cycle hydration.
        if is_new is not None:
            stubs = [s for s in stubs if is_new(name, s.external_id)]
        stubs = [s for s in stubs if _card_plausible(s, config)]
        if limit is not None:
            stubs = stubs[:limit]

        # 3) Hydrate each survivor via its detail page, sequentially.
        listings: list[Listing] = []
        for index, stub in enumerate(stubs):
            if index:
                await asyncio.sleep(
                    http_session.jitter(DETAIL_DELAY_SECONDS, DETAIL_DELAY_JITTER)
                )
            try:
                resp = await _get(
                    client,
                    stub.url,
                    headers=http_session.navigation_headers(referer=_list_url(*locations[0])),
                )
                listings.append(parse_listing(stub, _parse_detail(resp.text)))
            except Exception:
                log.exception("Kleinanzeigen: failed to hydrate offer %s", stub.external_id)

        if stats is not None:
            stats["enumerated"] = len(seen_ids)
        log.info(
            "Kleinanzeigen: %d offers normalized (%d enumerated)",
            len(listings), len(seen_ids),
        )
        return listings
    finally:
        if own_client:
            await http_session.aclose(client)


def _main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="flatsniper.sources.kleinanzeigen")
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
        sqm = f"{listing.sqm:g}" if listing.sqm else "?"
        print(
            f"[Flat] {listing.rent_warm}€ · {sqm}m² · {listing.rooms or '?'}Zi · "
            f"{listing.raw.get('postcode') or ''}\n"
            f"   {listing.title[:70]}\n   {listing.url}"
        )
    print(f"\n{len(listings)} normalized listing(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
