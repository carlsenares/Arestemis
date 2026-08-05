"""Offline tests for the ImmoScout24 adapter (mobile API, list-only, no network).

The JSON mirrors the real `/search/list` payload shape (resultListItems with an
EXPOSE_RESULT item carrying address + display-string attributes). The live query
is the manual acceptance: `python -m flatsniper.sources.immoscout --once`.
"""

from __future__ import annotations

import json
from datetime import date

from flatsniper import filter as filt
from flatsniper.config import WG_NONE, Config, RentTier, SearchConfig, Secrets
from flatsniper.sources import immoscout as is24


def _item(oid="167145710", title="Schöne 2-Zimmer-Wohnung",
          price="1.100\xa0€", area="62\xa0m²", rooms="2\xa0Zi.",
          line="85748 Garching bei München (unvollständige Adresse)", postcode="85748"):
    return {
        "type": "EXPOSE_RESULT",
        "item": {
            "id": oid,
            "title": title,
            "address": {"line": line, "postcode": postcode},
            "attributes": [
                {"label": "", "value": price},
                {"label": "", "value": area},
                {"label": "", "value": rooms},
            ],
            "published": "vor einer Stunde",
        },
    }


def _payload(*items, total=3511):
    return json.dumps({"totalResults": total, "resultListItems": list(items)})


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


def test_money_num_and_estimate_handle_de_format():
    assert is24._money("1.900\xa0€") == 1900       # dot thousands dropped
    assert is24._money("820 €") == 820
    assert is24._money("auf Anfrage") is None
    assert is24._num("87\xa0m²") == 87.0
    assert is24._num("2,5") == 2.5
    assert is24._estimated_warm(1100, 62) == 1100 + round(3.5 * 62)  # 1317
    assert is24._estimated_warm(1000, None) == 1000  # no size -> cold as-is
    assert is24._estimated_warm(None, 40) is None


def test_attributes_parsed_by_content_not_position():
    """Labels are empty, so price/size/rooms are matched by their unit, robust to
    the API reordering them."""
    reordered = _item(price="3\xa0Zi.", area="62\xa0m²", rooms="1.100\xa0€")
    # values list now is [rooms-looking, area, price-looking] but content drives it
    card = is24._parse_item(reordered["item"])
    assert card.cold_rent == 1100 and card.sqm == 62.0 and card.rooms == 3.0


def test_parse_cards_fields_and_dedup():
    payload = json.loads(_payload(
        _item("111", area="45\xa0m²", rooms="2\xa0Zi.", price="980\xa0€"),
        _item("222", area="79\xa0m²", rooms="3\xa0Zi.", price="1.500\xa0€"),
        _item("111"),  # duplicate id
    ))
    cards = is24.parse_cards(payload)
    assert [c.external_id for c in cards] == ["111", "222"]  # dup dropped, order kept
    a = cards[0]
    assert a.url == "https://www.immobilienscout24.de/expose/111"
    assert a.cold_rent == 980 and a.sqm == 45.0 and a.rooms == 2.0
    assert a.postcode == "85748"


def test_parse_cards_skips_non_expose_and_idless():
    payload = {"resultListItems": [
        {"type": "ADVERTISEMENT", "item": {"id": "ad1"}},  # not an expose
        {"type": "EXPOSE_RESULT", "item": {"title": "no id"}},  # no id
        _item("999"),
    ]}
    cards = is24.parse_cards(payload)
    assert [c.external_id for c in cards] == ["999"]


def test_count_cards_zero_on_blocked_or_garbage():
    assert is24.count_cards(_payload(_item("1"), _item("2"))) == 2
    assert is24.count_cards('{"error":"what???"}') == 0   # 404 catch-all body
    assert is24.count_cards('{"totalResults":0,"resultListItems":[]}') == 0
    assert is24.count_cards("<html>captcha</html>") == 0  # non-JSON
    assert is24.count_cards("") == 0


def test_parse_listing_estimates_warm_and_sets_raw():
    card = is24.parse_cards(json.loads(_payload(_item("167145710"))))[0]
    listing = is24.parse_listing(card)
    assert listing.source == "immoscout"
    assert listing.is_wg is False and listing.wg_size is None
    assert listing.url == "https://www.immobilienscout24.de/expose/167145710"
    assert listing.rent_warm == 1100 + round(3.5 * 62)  # estimated from cold
    assert listing.raw["rent_estimated"] is True
    assert listing.raw["rent_cold"] == 1100
    assert listing.raw["postcode"] == "85748"
    assert "garching" in listing.raw["city_name"].lower()
    assert listing.available_from is None  # not on the card; move-in filter keeps it


# --- pipeline integration --------------------------------------------------


def test_parsed_listing_passes_filter():
    card = is24.parse_cards(json.loads(_payload(
        _item(area="42\xa0m²", price="900\xa0€", rooms="2\xa0Zi.",
              line="80799 München, Maxvorstadt", postcode="80799"))))[0]
    listing = is24.parse_listing(card)  # warm ~ 900 + 147 = 1047, 42m² -> 40-tier (<=1200)
    assert filt.passes_prefilter(listing, _config()) is True


def test_parsed_listing_rejected_when_too_small():
    card = is24.parse_cards(json.loads(_payload(
        _item(area="20\xa0m²", price="700\xa0€"))))[0]
    listing = is24.parse_listing(card)
    assert filt.passes_prefilter(listing, _config()) is False  # 20 < min_sqm 28


# --- region resolution + enablement ----------------------------------------


def test_resolve_regions_follow_config_order_and_dedup():
    # Order follows config.areas (the real config lists Garching first), like the
    # other adapters; duplicates (München + a Munich district) collapse to one.
    cfg = _config(areas=["Garching bei München", "Schwabing", "München"])
    regions = is24._resolve_regions(cfg)
    assert regions[0] == "/de/bayern/muenchen-kreis/garching-bei-muenchen"  # Garching first
    assert "/de/bayern/muenchen" in regions
    assert len(regions) == len(set(regions)) == 2

    # No configured area -> the default order (Garching first) rather than nothing.
    assert is24._resolve_regions(_config(areas=[])) == is24.DEFAULT_REGION_ORDER


def test_list_url_shape():
    url = is24._list_url("/de/bayern/muenchen")
    assert url.startswith("https://api.mobile.immobilienscout24.de/search/list?")
    assert "searchType=region" in url
    assert "realestatetype=apartmentrent" in url
    assert "sorting=-firstactivation" in url  # newest first
    # The API accepts the geocode in raw path form (confirmed live); quote() leaves
    # the slashes (safe="/") so it round-trips unchanged.
    assert "geocodes=/de/bayern/muenchen" in url


def test_enabled_requires_known_region():
    assert is24._enabled(_config(areas=["München"])) is True
    assert is24._enabled(_config(areas=["Garching bei München"])) is True
    assert is24._enabled(_config(areas=["Berlin"])) is False
    assert is24._enabled(_config(areas=[])) is True  # no areas -> don't suppress
