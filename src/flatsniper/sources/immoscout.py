"""ImmoScout24 adapter (mobile-app API, list-only).

ImmoScout24 is the biggest single-flat portal but the most aggressively
bot-walled: its **website** returns a captcha/robot challenge (HTTP 401) to a
datacenter IP, so the Chrome-impersonation path the other HTML adapters use is a
dead end here. The **mobile-app API** (`api.mobile.immobilienscout24.de`),
however, answers from the same IP with no auth — it only expects to look like the
app. So this adapter talks to that API wearing an app identity (app User-Agent +
JSON Accept over a mobile TLS fingerprint; see `http_session.IDENTITIES`), which
is both the only working surface and the most ban-resistant one.

The list endpoint is an oddity worth recording: it's a **POST** to
`/search/list?<params>` with an **empty body** — the handler reads the query
string (a JSON/form body 500s, and `GET /search/list` is 404). The response JSON
carries everything we need per result card — id, title, address/PLZ, and price /
size / rooms as display strings in `attributes` — so this adapter is **list-only**:
one POST per region per cycle, no detail ("expose") fetch (the gentlest possible
footprint, like the Immowelt adapter).

What the card gives vs. doesn't: the price is always **Kaltmiete** (cold rent;
`pricetype` doesn't change the displayed value), so warm rent is *estimated* as
`cold + WARM_ESTIMATE_PER_SQM * m²` and flagged as estimated, exactly like
Immowelt. There's no precise move-in date on the card (the move-in filter keeps
unknown-date listings) and only a relative "published" string, so we sort
newest-first server-side (`sorting=-firstactivation`) and dedup by id rather than
by date. WGs are out of scope here (whole apartments only) — they come via
WG-Gesucht.

Optional escalation (not needed today — the mobile API works from the datacenter
IP): set `IMMOSCOUT_PROXY` in `.env` to route this source through a residential
proxy if the IP ever gets blocked (`http_session.proxy_for`). A documented web
fallback (parse the website's embedded JSON) is deliberately left unbuilt while
the mobile API is healthy.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import quote

from ..config import Config
from ..models import Listing
from . import http_session
from .base import NewPredicate

name = "immoscout"
log = logging.getLogger(__name__)

BASE = "https://api.mobile.immobilienscout24.de"
PAGE_SIZE = 50  # one page is plenty for a newest-first poll
MAX_RETRIES = 2
RATE_LIMIT_BACKOFF_SECONDS = 5.0

# Region geocodes (path form) the API expects. Garching bei München (TUM campus)
# sits in the Landkreis (muenchen-kreis), distinct from the city; München is the
# broad fallback. config.search.areas map to these via alias substrings, mirroring
# the other adapters. Resolve more via GET /geoautocomplete/v3/locations.json.
REGIONS: dict[str, dict] = {
    "/de/bayern/muenchen-kreis/garching-bei-muenchen": {"aliases": ("garching",)},
    "/de/bayern/muenchen": {
        "aliases": (
            "münchen", "muenchen", "munchen", "munich",
            "freimann", "studentenstadt", "schwabing", "maxvorstadt",
        ),
    },
}
DEFAULT_REGION_ORDER = [
    "/de/bayern/muenchen-kreis/garching-bei-muenchen",
    "/de/bayern/muenchen",
]

# Estimated warm-rent uplift over Kaltmiete (Nebenkosten + heating), €/m². Mirrors
# the Immowelt adapter (Munich operating costs run high; a moderate approximation).
WARM_ESTIMATE_PER_SQM = 3.5

_M2_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m²")
_ROOMS_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:Zi|Zimmer|rms?|rooms?)", re.IGNORECASE)


# --- parsing helpers (pure, no I/O) ----------------------------------------


def _norm(text: str | None) -> str:
    """Collapse whitespace incl. the NBSP the API uses between number and unit."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _money(text: str | None) -> int | None:
    """`'1.900 €'` -> 1900. Drops '.' thousands and ',dd' cents (de-DE format)."""
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
    """First number in `text` as float ('87 m²' -> 87.0, '2,5' -> 2.5)."""
    if not text:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group(0).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _estimated_warm(cold: int | None, sqm: float | None) -> int | None:
    """Approximate Warmmiete from Kaltmiete (+ per-m² operating costs)."""
    if cold is None:
        return None
    if sqm is None:
        return cold  # no size to scale by — best we can do
    return cold + round(WARM_ESTIMATE_PER_SQM * sqm)


@dataclass(frozen=True)
class Card:
    external_id: str
    url: str
    title: str
    cold_rent: int | None
    sqm: float | None
    rooms: float | None
    postcode: str | None
    address: str | None


def _attr_values(item: dict) -> list[str]:
    """The display strings in a result item's `attributes` (price/size/rooms).

    Labels are always empty, so we read by content (€ / m² / Zi.) rather than by
    position — robust to the API reordering them."""
    return [_norm(a.get("value")) for a in (item.get("attributes") or []) if a.get("value")]


def _pick(values: list[str], pattern: re.Pattern[str]) -> str | None:
    for v in values:
        if pattern.search(v):
            return v
    return None


def _parse_item(item: dict) -> Card | None:
    """Normalize one `resultListItems[].item` into a Card, or None if it has no id."""
    oid = item.get("id")
    if not oid:
        return None
    values = _attr_values(item)
    price_txt = _pick(values, re.compile("€")) or _pick(values, re.compile(r"\bEUR\b"))
    sqm_txt = _pick(values, _M2_RE)
    rooms_txt = _pick(values, _ROOMS_RE)
    address = item.get("address") or {}
    return Card(
        external_id=str(oid),
        url=f"https://www.immobilienscout24.de/expose/{oid}",
        title=_norm(item.get("title")) or "Wohnung",
        cold_rent=_money(price_txt),
        sqm=_num(_M2_RE.search(sqm_txt).group(0)) if sqm_txt and _M2_RE.search(sqm_txt) else None,
        rooms=_num(rooms_txt),
        postcode=address.get("postcode"),
        address=_norm(address.get("line")) or None,
    )


def _result_items(payload) -> list[dict]:
    """The EXPOSE result items from a parsed /search/list payload (defensive)."""
    if not isinstance(payload, dict):
        return []
    out = []
    for entry in payload.get("resultListItems") or []:
        if isinstance(entry, dict) and entry.get("type") == "EXPOSE_RESULT":
            item = entry.get("item")
            if isinstance(item, dict):
                out.append(item)
    return out


def parse_cards(payload) -> list[Card]:
    """All offer cards from a /search/list payload, de-duplicated by expose id."""
    out: list[Card] = []
    seen: set[str] = set()
    for item in _result_items(payload):
        card = _parse_item(item)
        if card and card.external_id not in seen:
            seen.add(card.external_id)
            out.append(card)
    return out


def count_cards(body: str) -> int:
    """Number of real offer cards in a /search/list response body. The health
    watchdog uses this to tell "served listings" from "200 but empty / error
    JSON" (a block) — keyed on real cards, not a marker substring."""
    try:
        return len(parse_cards(json.loads(body)))
    except Exception:
        return 0


def parse_listing(card: Card) -> Listing:
    """Card -> normalized Listing (warm rent estimated from cold, like Immowelt)."""
    warm = _estimated_warm(card.cold_rent, card.sqm)
    return Listing(
        source=name,
        external_id=card.external_id,
        url=card.url,
        title=card.title,
        rent_warm=warm,
        rooms=card.rooms,
        sqm=card.sqm,
        is_wg=False,  # whole apartments only; WGs come via WG-Gesucht
        wg_size=None,
        description="",  # not on the list card; no detail fetch
        posted_at=None,  # only a relative "published" string is exposed
        available_from=None,  # not on the card (move-in filter keeps unknown dates)
        raw={
            "postcode": card.postcode,
            "city_name": card.address,  # filter.area_ok matches "münchen"/"garching" here
            "rent_cold": card.cold_rent,
            "rent_estimated": warm is not None and warm != card.cold_rent,
        },
    )


# --- region resolution + URL ----------------------------------------------


def _match_region(area: str) -> str | None:
    a = area.lower()
    for geocode, meta in REGIONS.items():
        if any(tok in a for tok in meta["aliases"]):
            return geocode
    return None


def _resolve_regions(config: Config) -> list[str]:
    """Map config.search.areas -> ordered, de-duplicated geocodes (Garching first)."""
    out: list[str] = []
    seen: set[str] = set()
    for area in config.search.areas:
        geocode = _match_region(area)
        if geocode is not None and geocode not in seen:
            seen.add(geocode)
            out.append(geocode)
    return out or list(DEFAULT_REGION_ORDER)


def _list_url(geocode: str) -> str:
    return (
        f"{BASE}/search/list?searchType=region&geocodes={quote(geocode)}"
        f"&realestatetype=apartmentrent&sorting=-firstactivation&pagesize={PAGE_SIZE}"
    )


def _enabled(config: Config) -> bool:
    """Only query if a configured area maps to a known region (else nothing to do)."""
    return bool(_resolve_regions(config)) and (
        any(
            tok in area.lower()
            for area in config.search.areas
            for tok in ("münchen", "muenchen", "munchen", "munich", "garching")
        )
        or not config.search.areas
    )


# --- HTTP ------------------------------------------------------------------


async def _post_list(client, geocode: str) -> str:
    """POST the list endpoint (empty body) with jittered 429 backoff; return text.

    The list route reads the query string, so the body is intentionally empty.
    Trips the shared circuit breaker on repeated 429 so we stop hammering a
    rate-limited API (which is what escalates a limit into a ban)."""
    breaker = http_session.get_breaker(name)
    url = _list_url(geocode)
    for attempt in range(MAX_RETRIES + 1):
        resp = await client.post(url, data="")
        status = getattr(resp, "status_code", None)
        if status == 429:
            if attempt < MAX_RETRIES:
                retry_after = resp.headers.get("Retry-After")
                wait = (
                    float(retry_after)
                    if retry_after and str(retry_after).isdigit()
                    else http_session.jitter(RATE_LIMIT_BACKOFF_SECONDS * (attempt + 1))
                )
                log.warning("ImmoScout: 429 on %s, backing off %.1fs", geocode, wait)
                await asyncio.sleep(wait)
                continue
            if breaker.record_failure():
                log.warning("ImmoScout: repeated 429 — pausing for %.0fs", breaker.cooldown)
            raise http_session.RateLimited(f"ImmoScout list {geocode} 429")
        resp.raise_for_status()
        breaker.record_success()
        return resp.text
    resp.raise_for_status()
    return resp.text


async def fetch(
    config: Config,
    is_new: NewPredicate | None = None,
    *,
    regions: list[str] | None = None,
    limit: int | None = None,
    client=None,
    stats: dict | None = None,
) -> list[Listing]:
    """Fetch + normalize current ImmoScout24 München/Garching offers (list-only).

    One POST per region per cycle, no detail fetch. When `stats` is given, the raw
    enumerated-card count is recorded as `stats["enumerated"]` so the scheduler's
    scrape watchdog can detect a block (e.g. 200 with zero cards) independent of
    how many survive de-dup/filtering."""
    if not _enabled(config):
        return []
    own_client = client is None
    if own_client:
        client = http_session.new_client(
            identity=http_session.IDENTITIES.get(name), proxy=http_session.proxy_for(name)
        )
    regions = regions or _resolve_regions(config)
    breaker = http_session.get_breaker(name)

    try:
        if not breaker.allow():
            log.warning(
                "ImmoScout: API in cooldown (%.0fs left) — skipping this cycle",
                breaker.seconds_left(),
            )
            if stats is not None:
                stats["enumerated"] = None
            return []

        # 1) Enumerate cards across regions (Garching first), dedup by expose id.
        cards: list[Card] = []
        seen_ids: set[str] = set()
        for geocode in regions:
            try:
                body = await _post_list(client, geocode)
                for card in parse_cards(json.loads(body)):
                    if card.external_id not in seen_ids:
                        seen_ids.add(card.external_id)
                        cards.append(card)
            except http_session.RateLimited:
                break  # don't keep hitting a rate-limited API this cycle
            except Exception:
                log.exception("ImmoScout: list failed region=%s", geocode)

        # 2) Skip already-seen, then bound per-cycle NEW returns (cold-start ramp).
        if is_new is not None:
            cards = [c for c in cards if is_new(name, c.external_id)]
        if limit is not None:
            cards = cards[:limit]

        listings = [parse_listing(c) for c in cards]
        if stats is not None:
            stats["enumerated"] = len(seen_ids)
        log.info(
            "ImmoScout: %d new offers normalized (%d enumerated)", len(listings), len(seen_ids)
        )
        return listings
    finally:
        if own_client:
            await http_session.aclose(client)


def _main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="flatsniper.sources.immoscout")
    parser.add_argument("--once", action="store_true", help="Run one live query and print results.")
    parser.add_argument("--limit", type=int, default=10, help="Max listings to print.")
    args = parser.parse_args(argv)
    if not args.once:
        parser.print_help()
        return 0
    from ..config import load_config

    config = load_config()
    listings = asyncio.run(fetch(config, limit=args.limit))
    for listing in listings:
        sqm = f"{listing.sqm:g}" if listing.sqm else "?"
        cold = (listing.raw or {}).get("rent_cold")
        print(
            f"[Flat] ~{listing.rent_warm}€ warm (kalt {cold}€) · {sqm}m² · "
            f"{listing.rooms or '?'}Zi · {(listing.raw or {}).get('city_name') or ''}\n"
            f"   {listing.title[:70]}\n   {listing.url}"
        )
    print(f"\n{len(listings)} normalized listing(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
