"""Immowelt adapter (HTML list page, card-only).

Immowelt fronts everything with DataDome: the detail ("Exposé") pages, the JSON
search API and the location autocomplete all return a captcha (HTTP 403). Only
the **server-rendered list page** is reachable with our browser-like client. So
this adapter is card-only — it normalizes entirely from the result cards, makes
exactly one request per cycle, and never fetches a detail page (which is also the
gentlest possible footprint).

What the list card gives us: the Exposé id + URL, rooms, m², the move-in date
(on ~half the cards), the address/PLZ, a description snippet, and the price —
but the price is always **Kaltmiete** (cold rent). Since we can't reach the
detail page for the real Warmmiete, we *estimate* warm rent as
`cold + WARM_ESTIMATE_PER_SQM * m²` (a typical Nebenkosten+heating rate) and flag
it as estimated so the notifier can label it. The normal pipeline (dedup,
move-in/rent filter) then applies as usual.

Limitations (documented, not bugs): München only — Garching's geo-id sits behind
the captcha'd autocomplete and the slug URL is gone (410); and the list can't be
sorted newest-first (the sort control is client-side via the captcha'd API), so
this widens coverage rather than guaranteeing first-mover speed.
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

name = "immowelt"
log = logging.getLogger(__name__)

BASE = "https://www.immowelt.de"
# /liste/... 301-redirects to the canonical /suche/.../{geoId} page; letting the
# client follow it keeps us robust to geo-id changes.
MUENCHEN_PATH = "/liste/muenchen/wohnungen/mieten"
MAX_RETRIES = 2
RATE_LIMIT_BACKOFF_SECONDS = 5.0

# Estimated warm-rent uplift over Kaltmiete (Nebenkosten + heating), €/m². Munich
# operating costs run high; this is a deliberately moderate approximation.
WARM_ESTIMATE_PER_SQM = 3.5

# Card selectors (Immowelt "serp" cards use stable data-testid attributes).
_CARD_SEL = '[data-testid="serp-core-classified-card-testid"]'
_PRICE_SEL = '[data-testid="cardmfe-price-testid"]'
_KEYFACTS_SEL = '[data-testid="cardmfe-keyfacts-testid"]'
_ADDRESS_SEL = '[data-testid="cardmfe-description-box-address"]'
_DESC_SEL = '[data-testid="cardmfe-description-text-test-id"]'

_M2_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m²")
_ROOMS_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*Zimmer")
_PLZ_RE = re.compile(r"\b(\d{5})\b")
_DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")
_EXPOSE_RE = re.compile(r"/expose/([0-9a-fA-F-]{16,})")


# --- parsing helpers (pure) -----------------------------------------------


def _clean_entities(html: str) -> str:
    """Repair decimal numeric char refs missing the trailing ';' (defensive —
    some portals emit them and they crash stdlib html.parser)."""
    return re.sub(r"&#(\d+)(?![\d;])", r"&#\1;", html)


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(_clean_entities(html), "html.parser")


def _money(text: str | None) -> int | None:
    """`'2.360 € Kaltmiete'` -> 2360. '.' thousands dropped, ',dd' cents ignored."""
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
    if not text:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group(0).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _parse_date(text: str | None) -> date | None:
    if not text:
        return None
    m = _DATE_RE.search(text)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%d.%m.%Y").date()
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
    cold_rent: int | None
    sqm: float | None
    rooms: float | None
    available_from: date | None
    plz: str | None
    address: str | None
    description: str


def _txt(node, sel: str) -> str | None:
    el = node.select_one(sel)
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else None


def _parse_cards(html: str) -> list[Card]:
    """Normalize the result cards on a list page, de-duplicated by Exposé id."""
    soup = _soup(html)
    out: list[Card] = []
    seen: set[str] = set()
    for node in soup.select(_CARD_SEL):
        link = node.select_one('a[href*="/expose/"]')
        href = link.get("href") if link else None
        m = _EXPOSE_RE.search(href or "")
        if not m:
            continue
        oid = m.group(1)
        if oid in seen:
            continue
        seen.add(oid)
        keyfacts = _txt(node, _KEYFACTS_SEL) or ""
        address = _txt(node, _ADDRESS_SEL)
        m2 = _M2_RE.search(keyfacts)
        zi = _ROOMS_RE.search(keyfacts)
        plz = _PLZ_RE.search(address or "")
        out.append(
            Card(
                external_id=oid,
                url=href if href.startswith("http") else BASE + href,
                cold_rent=_money(_txt(node, _PRICE_SEL)),
                sqm=_num(m2.group(0)) if m2 else None,
                rooms=_num(zi.group(0)) if zi else None,
                available_from=_parse_date(keyfacts),
                plz=plz.group(1) if plz else None,
                address=address,
                description=_txt(node, _DESC_SEL) or "",
            )
        )
    return out


def count_list_cards(html: str) -> int:
    """Number of real offer cards on a list page. The health watchdog uses this
    to tell "served real listings" from "200 but the listings were stripped"
    (a soft block) — a substring marker can't, since class names linger in CSS."""
    return len(_parse_cards(html))


def parse_listing(card: Card) -> Listing:
    """Card -> normalized Listing (warm rent estimated from cold)."""
    warm = _estimated_warm(card.cold_rent, card.sqm)
    title = card.address or "Wohnung"
    return Listing(
        source=name,
        external_id=card.external_id,
        url=card.url,
        title=title,
        rent_warm=warm,
        rooms=card.rooms,
        sqm=card.sqm,
        is_wg=False,  # apartment listings; WGs come via WG-Gesucht
        wg_size=None,
        description=card.description,
        posted_at=None,  # not exposed on the card
        available_from=card.available_from,
        raw={
            "postcode": card.plz,
            "city_name": card.address,  # filter.area_ok matches "münchen" here
            "rent_cold": card.cold_rent,
            "rent_estimated": warm is not None and warm != card.cold_rent,
        },
    )


# --- HTTP ------------------------------------------------------------------


async def _get(client: httpx.AsyncClient, url: str, headers: dict) -> httpx.Response:
    """GET with jittered HTTP 429 backoff+retry, then raise_for_status."""
    for attempt in range(MAX_RETRIES + 1):
        resp = await client.get(url, headers=headers)
        if resp.status_code == 429 and attempt < MAX_RETRIES:
            retry_after = resp.headers.get("Retry-After")
            wait = (
                float(retry_after)
                if retry_after and retry_after.isdigit()
                else http_session.jitter(RATE_LIMIT_BACKOFF_SECONDS * (attempt + 1))
            )
            log.warning("Immowelt: 429 on %s, backing off %.1fs", url, wait)
            await asyncio.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


def _enabled(config: Config) -> bool:
    """Only query if a München-area alias is configured (Garching unsupported)."""
    return any(
        tok in area.lower()
        for area in config.search.areas
        for tok in ("münchen", "muenchen", "munchen", "munich")
    ) or not config.search.areas


async def fetch(
    config: Config,
    is_new: NewPredicate | None = None,
    *,
    limit: int | None = None,
    client: httpx.AsyncClient | None = None,
    stats: dict | None = None,
) -> list[Listing]:
    """Fetch + normalize current Immowelt München offers (card-only).

    When `stats` is given, the raw card count is recorded as `stats["enumerated"]`
    so the scheduler's scrape watchdog can see whether we got any cards at all
    (independent of how many survive de-dup/filtering)."""
    if not _enabled(config):
        return []
    own_client = client is None
    if own_client:
        client = http_session.new_client()
    try:
        resp = await _get(
            client,
            BASE + MUENCHEN_PATH,
            headers=http_session.navigation_headers(referer=f"{BASE}/"),
        )
        cards = _parse_cards(resp.text)
        if stats is not None:
            stats["enumerated"] = len(cards)
        # Card-only: no detail fetch. Bound per-cycle NEW returns with is_new +
        # the cold-start limit so a fresh DB doesn't notify the whole page at once.
        if is_new is not None:
            cards = [c for c in cards if is_new(name, c.external_id)]
        if limit is not None:
            cards = cards[:limit]
        listings = [parse_listing(c) for c in cards]
        log.info("Immowelt: %d new offers normalized", len(listings))
        return listings
    finally:
        if own_client:
            await http_session.aclose(client)


def _main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="flatsniper.sources.immowelt")
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
            f"{listing.rooms or '?'}Zi · ab {listing.available_from or '?'}\n"
            f"   {listing.title}\n   {listing.url}"
        )
    print(f"\n{len(listings)} normalized listing(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
