"""Offline tests for WG-Gesucht parsing/normalization (no network).

The live query is the manual acceptance: `python -m flatsniper.sources.wg_gesucht --once`.
"""

from __future__ import annotations

from datetime import date, datetime

from flatsniper.sources import wg_gesucht as wg

# Trimmed shapes mirroring the real /api/public/offers/{id} payload.
FLAT_DETAIL = {
    "offer_id": "13534698",
    "category": "2",
    "offer_title": "Modern Apartment",
    "total_costs": "1950",
    "rent_costs": "1500",
    "utility_costs": None,
    "property_size": "54",
    "number_of_rooms": "2.5",
    "flatshare_inhabitants_total": None,
    "available_from_date": "10.06.2026",
    "date_created": "27.05.2026, 11:04:49",
    "freetext_property_description": "  Helle Wohnung  ",
    "freetext_flatshare": "",
    "district_custom": "Feldmoching-Hasenbergl",
}

WG_DETAIL = {
    "offer_id": "13354121",
    "category": "0",
    "offer_title": "Zimmer in WG",
    "total_costs": "895",
    "property_size": "13",
    "flatshare_property_size": "120",
    "number_of_rooms": "0",
    "flatshare_inhabitants_total": "5",
    "available_from_date": "00.00.0000",
    "freetext_property_description": "Zimmer frei",
    "freetext_flatshare": "5er WG",
}


def test_parse_flat():
    listing = wg.parse_listing(FLAT_DETAIL, "https://www.wg-gesucht.de/x.13534698.html")
    assert listing.source == "wg_gesucht"
    assert listing.external_id == "13534698"
    assert listing.is_wg is False
    assert listing.wg_size is None
    assert listing.rent_warm == 1950  # total_costs wins
    assert listing.sqm == 54.0
    assert listing.rooms == 2.5
    assert listing.available_from == date(2026, 6, 10)
    assert listing.posted_at == datetime(2026, 5, 27, 11, 4, 49)
    assert listing.description == "Helle Wohnung"  # trimmed, empty block dropped


def test_parse_wg():
    listing = wg.parse_listing(WG_DETAIL, "https://www.wg-gesucht.de/x.13354121.html")
    assert listing.is_wg is True
    assert listing.wg_size == 5
    assert listing.rent_warm == 895
    assert listing.available_from is None  # 00.00.0000 -> None
    assert "5er WG" in listing.description


def test_warm_rent_falls_back_to_components():
    detail = {"offer_id": "1", "category": "2", "total_costs": "0",
              "rent_costs": "600", "utility_costs": "150", "other_costs": None}
    assert wg.parse_listing(detail, "u").rent_warm == 750


def test_enabled_categories_includes_wg_when_allowed():
    class C:
        allow_empty_2person_wg = True
    assert wg._enabled_categories(C()) == [0, 1, 2]

    class D:
        allow_empty_2person_wg = False
    assert wg._enabled_categories(D()) == [1, 2]


def test_scrape_offer_ids_dedups_and_orders():
    html = (
        '<div class="wgg_card offer_list_item " data-id="111"></div>'
        '<div class="wgg_card offer_list_item premium" data-id="222"></div>'
        '<span id="back_to_ad_111"></span>'  # must not be picked up
        '<div class="wgg_card offer_list_item " data-id="111"></div>'  # dup
    )
    assert wg._scrape_offer_ids(html) == ["111", "222"]


def test_offer_url_from_list():
    html = '<a href="/wohnungen-in-Muenchen-Schwabing.333.html">x</a>'
    assert wg._offer_url(html, "333") == "https://www.wg-gesucht.de/wohnungen-in-Muenchen-Schwabing.333.html"
    # fallback when not present
    assert wg._offer_url("<html></html>", "444") == "https://www.wg-gesucht.de/444.html"


def test_date_helpers():
    assert wg._parse_de_date("01.07.2026") == date(2026, 7, 1)
    assert wg._parse_de_date("00.00.0000") is None
    assert wg._parse_de_date(None) is None
    assert wg._to_int("54") == 54 and wg._to_int("2.5") == 2 and wg._to_int(None) is None
    assert wg._to_float("2.5") == 2.5 and wg._to_float("") is None
