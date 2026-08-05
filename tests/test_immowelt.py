"""Offline tests for the Immowelt adapter (card-only, no network).

HTML snippets mirror the real result-card structure (data-testid attributes).
The live query is the manual acceptance: `python -m flatsniper.sources.immowelt --once`.
"""

from __future__ import annotations

from datetime import date

from flatsniper import filter as filt
from flatsniper.config import WG_NONE, Config, RentTier, SearchConfig, Secrets
from flatsniper.sources import immowelt as iw

UUID = "8f259bc1-1892-4622-844a-6a599b464f64"
UUID2 = "75e7c57f-5b17-498c-941e-2769d20a1755"


def _card_html(uuid=UUID, price="1.050 € Kaltmiete", keyfacts="2 Zimmer · 45 m² · frei ab 01.09.2026",
               address="Schwabing, München (80799)", desc="Schöne, helle Wohnung"):
    return f"""
    <div data-testid="serp-core-classified-card-testid">
      <a href="https://www.immowelt.de/expose/{uuid}" data-testid="card-mfe-covering-link-testid">x</a>
      <div data-testid="cardmfe-price-testid">{price}</div>
      <div data-testid="cardmfe-keyfacts-testid">{keyfacts}</div>
      <div data-testid="cardmfe-description-box-address">{address}</div>
      <div data-testid="cardmfe-description-text-test-id">{desc}</div>
    </div>"""


def _config(**kw):
    search = SearchConfig(
        min_sqm=kw.get("min_sqm", 28),
        rent_tiers=kw.get("rent_tiers", [RentTier(28, 1000), RentTier(40, 1200)]),
        min_rooms=1,
        earliest_move_in=date(2026, 8, 1),
        areas=kw.get("areas", ["München", "Garching bei München"]),
        postal_codes=[],
    )
    return Config(search=search, wg_mode=WG_NONE, sources={},
                  poll_interval_seconds=60, about_me="", secrets=Secrets("t", "c", "k", "m"))


# --- pure parsers ----------------------------------------------------------


def test_money_and_num_and_date():
    assert iw._money("2.360 € Kaltmiete") == 2360
    assert iw._money("1.050 €") == 1050
    assert iw._money("auf Anfrage") is None
    assert iw._num("45 m²") == 45.0
    assert iw._num("2,5") == 2.5
    assert iw._parse_date("frei ab 01.09.2026") == date(2026, 9, 1)
    assert iw._parse_date("ab sofort") is None


def test_estimated_warm():
    assert iw._estimated_warm(900, 40) == 900 + round(3.5 * 40)  # 1040
    assert iw._estimated_warm(1000, None) == 1000  # no size -> cold as-is
    assert iw._estimated_warm(None, 40) is None


def test_parse_cards_fields_and_dedup():
    html = _card_html(UUID) + _card_html(UUID2, price="1.920 € Kaltmiete",
        keyfacts="2 Zimmer · 79 m²", address="Au-Haidhausen, München (81675)") + _card_html(UUID)
    cards = iw._parse_cards(html)
    assert [c.external_id for c in cards] == [UUID, UUID2]  # dup dropped, order kept
    a, b = cards
    assert a.url == f"https://www.immowelt.de/expose/{UUID}"
    assert a.cold_rent == 1050 and a.sqm == 45.0 and a.rooms == 2.0
    assert a.available_from == date(2026, 9, 1) and a.plz == "80799"
    # Second card has no move-in date in keyfacts -> None (kept downstream).
    assert b.available_from is None and b.sqm == 79.0


def test_parse_cards_skips_cards_without_expose_link():
    html = '<div data-testid="serp-core-classified-card-testid"><a href="/agent/foo">x</a></div>'
    assert iw._parse_cards(html) == []


def test_parse_listing_estimates_warm_and_sets_raw():
    card = iw._parse_cards(_card_html())[0]
    listing = iw.parse_listing(card)
    assert listing.source == "immowelt"
    assert listing.is_wg is False and listing.wg_size is None
    assert listing.rent_warm == 1050 + round(3.5 * 45)  # estimated
    assert listing.raw["rent_estimated"] is True
    assert listing.raw["rent_cold"] == 1050
    assert listing.raw["postcode"] == "80799"
    assert "München" in listing.raw["city_name"]
    assert listing.available_from == date(2026, 9, 1)


def test_parsed_listing_passes_filter():
    card = iw._parse_cards(_card_html(price="900 € Kaltmiete",
        keyfacts="2 Zimmer · 42 m² · frei ab 01.09.2026"))[0]
    listing = iw.parse_listing(card)  # warm ~ 900 + 147 = 1047, sqm 42 -> 40-tier (<=1200)
    assert filt.passes_prefilter(listing, _config()) is True


def test_parsed_listing_rejected_when_move_in_too_early():
    card = iw._parse_cards(_card_html(keyfacts="2 Zimmer · 45 m² · frei ab 01.07.2026"))[0]
    listing = iw.parse_listing(card)
    assert filt.passes_prefilter(listing, _config()) is False  # July < Aug floor


# --- enablement ------------------------------------------------------------


def test_enabled_requires_munich_area():
    assert iw._enabled(_config(areas=["München"])) is True
    assert iw._enabled(_config(areas=["Garching bei München"])) is True  # contains 'münchen'
    assert iw._enabled(_config(areas=["Berlin"])) is False
    assert iw._enabled(_config(areas=[])) is True  # no areas -> don't suppress
