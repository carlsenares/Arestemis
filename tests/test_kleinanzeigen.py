"""Offline tests for the Kleinanzeigen adapter (no network).

HTML snippets mirror the real list-card / detail-page structure captured during
development. The live query is the manual acceptance:
`python -m flatsniper.sources.kleinanzeigen --once`.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from flatsniper import filter as filt
from flatsniper.config import WG_NONE, Config, RentTier, SearchConfig, Secrets
from flatsniper.sources import kleinanzeigen as ka


# --- helpers ---------------------------------------------------------------


def _card(adid, *, price="1.200 €", tags="45 m² · 2 Zi.", loc="80538 München/Lehel",
          desc="Schöne Wohnung", href=None):
    href = href or f"/s-anzeige/x/{adid}-203-1"
    return f"""
    <article class="aditem" data-adid="{adid}" data-href="{href}">
      <div class="aditem-main--top">
        <div class="aditem-main--top--left">{loc}</div>
        <div class="aditem-main--top--right">31.05.2026</div>
      </div>
      <div class="aditem-main--middle">
        <h2 class="text-module-begin"><a href="{href}">link</a></h2>
        <p class="aditem-main--middle--description">{desc}</p>
        <p class="aditem-main--middle--tags">{tags}</p>
        <p class="aditem-main--middle--price-shipping">
          <span class="aditem-main--middle--price-shipping--price">{price}</span>
        </p>
      </div>
    </article>"""


def _detail(pairs, *, desc="Tolle Wohnung &#8203Lehel", posted="31.05.2026"):
    lis = "".join(
        f'<li class="addetailslist--detail">{k}'
        f'<span class="addetailslist--detail--value">{v}</span></li>'
        for k, v in pairs.items()
    )
    return f"""
    <html><body>
      <ul class="addetailslist">{lis}</ul>
      <p id="viewad-description-text">{desc}</p>
      <div id="viewad-extra-info"><span>{posted}</span></div>
    </body></html>"""


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


def test_clean_entities_repairs_missing_semicolons():
    # Decimal refs missing ';' get one; well-formed refs are left untouched.
    assert ka._clean_entities("a&#8203Foo") == "a&#8203;Foo"
    assert ka._clean_entities("a&#8203;ok &amp; b") == "a&#8203;ok &amp; b"
    assert ka._clean_entities("x &amp; &#0; y") == "x &amp; &#0; y"


def test_money():
    assert ka._money("2.100 €") == 2100
    assert ka._money("1.234,56 €") == 1234  # cents dropped
    assert ka._money("950 €") == 950
    assert ka._money("VB") is None
    assert ka._money("") is None
    assert ka._money(None) is None


def test_num():
    assert ka._num("100 m²") == 100.0
    assert ka._num("2,5") == 2.5
    assert ka._num("70,45 m²") == 70.45
    assert ka._num(None) is None


def test_parse_avail():
    assert ka._parse_avail("Juli 2026") == date(2026, 7, 1)
    assert ka._parse_avail("01.07.2026") == date(2026, 7, 1)
    assert ka._parse_avail("ab sofort") is None
    assert ka._parse_avail(None) is None


def test_parse_cards_extracts_fields_and_dedups():
    html = _card("111", price="1.200 €", tags="45 m² · 2 Zi.") + \
        _card("222", price="VB", tags="33 m²", loc="85748 Garching b. München") + \
        _card("111")  # duplicate id -> dropped
    cards = ka._parse_cards(html)
    assert [c.external_id for c in cards] == ["111", "222"]
    a, b = cards
    assert a.url == "https://www.kleinanzeigen.de/s-anzeige/x/111-203-1"
    assert a.cold_rent == 1200 and a.sqm == 45.0 and a.rooms == 2.0
    assert a.plz == "80538" and "München" in a.location
    assert b.cold_rent is None and b.sqm == 33.0 and b.rooms is None  # no "Zi" -> None
    assert b.plz == "85748"


def test_parse_cards_survives_malformed_entities():
    # Regression: a stray &#8203 (no ';') in the markup must not crash html.parser.
    html = _card("9", loc="80331 München/&#8203Altstadt")
    cards = ka._parse_cards(html)
    assert cards and cards[0].external_id == "9"


# --- pre-screen ------------------------------------------------------------


def _stub(**kw):
    base = dict(external_id="1", url="u", title="t", cold_rent=900, sqm=45,
                rooms=2.0, plz="80538", location="80538 München")
    base.update(kw)
    return ka.CardStub(**base)


def test_card_plausible_rejects_too_small_and_over_budget():
    cfg = _config()
    assert ka._card_plausible(_stub(sqm=45, cold_rent=900), cfg) is True
    # too small (< min_sqm 28)
    assert ka._card_plausible(_stub(sqm=20, cold_rent=500), cfg) is False
    # 45 m² -> 40-tier ceiling 1200; cold 1300 already over -> reject
    assert ka._card_plausible(_stub(sqm=45, cold_rent=1300), cfg) is False
    # 33 m² -> only 28-tier (1000) applies; cold 1100 over that tier -> reject
    assert ka._card_plausible(_stub(sqm=33, cold_rent=1100), cfg) is False


def test_card_plausible_keeps_when_data_unknown():
    cfg = _config()
    # Unknown price -> hydrate to verify.
    assert ka._card_plausible(_stub(sqm=45, cold_rent=None), cfg) is True
    # Unknown size: keep if cold within the highest ceiling, else reject.
    assert ka._card_plausible(_stub(sqm=None, cold_rent=1150), cfg) is True
    assert ka._card_plausible(_stub(sqm=None, cold_rent=1300), cfg) is False


# --- detail + normalization ------------------------------------------------


def test_parse_listing_full():
    stub = _stub(external_id="500", cold_rent=1100, sqm=45, rooms=2.0)
    detail = ka._parse_detail(_detail({
        "Wohnfläche": "46 m²", "Zimmer": "2", "Warmmiete": "1.150 €",
        "Verfügbar ab": "Juli 2026",
    }))
    listing = ka.parse_listing(stub, detail)
    assert listing.source == "kleinanzeigen"
    assert listing.external_id == "500"
    assert listing.is_wg is False and listing.wg_size is None
    assert listing.rent_warm == 1150          # Warmmiete wins over card cold rent
    assert listing.sqm == 46.0                # detail wins over card
    assert listing.available_from == date(2026, 7, 1)
    assert listing.posted_at == datetime(2026, 5, 31)
    assert "Lehel" in listing.description     # malformed entity cleaned, text intact
    assert listing.raw["postcode"] == "80538"
    assert "München" in listing.raw["city_name"]


def test_warm_rent_fallbacks():
    assert ka._warm_rent({"Warmmiete": "1.150 €"}, None) == 1150
    # No Warmmiete: Kaltmiete + Nebenkosten.
    assert ka._warm_rent({"Kaltmiete": "900 €", "Nebenkosten": "200 €"}, None) == 1100
    # Neither warm nor components: fall back to the card's cold rent.
    assert ka._warm_rent({}, 950) == 950
    assert ka._warm_rent({}, None) is None


def test_parsed_listing_passes_filter():
    """A normalized match should clear the real prefilter end-to-end."""
    stub = _stub(cold_rent=1000, sqm=45, location="80538 München/Lehel")
    detail = ka._parse_detail(_detail({"Wohnfläche": "45 m²", "Warmmiete": "1.100 €"}))
    listing = ka.parse_listing(stub, detail)
    assert filt.passes_prefilter(listing, _config()) is True


# --- locations + URLs ------------------------------------------------------


def test_match_location_and_resolve_order():
    assert ka._match_location("Garching bei München") == 6303  # garching wins
    assert ka._match_location("München") == 6411
    assert ka._match_location("Berlin") is None
    cfg = _config(areas=["Garching bei München", "Schwabing", "München"])
    assert ka._resolve_locations(cfg) == [(6303, "garching-b-muenchen"), (6411, "muenchen")]


def test_resolve_locations_fallback():
    cfg = _config(areas=["Nowhere"])
    assert ka._resolve_locations(cfg) == [(6303, "garching-b-muenchen"), (6411, "muenchen")]


def test_list_url():
    assert ka._list_url(6411, "muenchen") == (
        "https://www.kleinanzeigen.de/s-wohnung-mieten/anzeige:angebote/"
        "sortierung:neueste/muenchen/c203l6411"
    )


# --- HTTP retry ------------------------------------------------------------


class _Resp:
    def __init__(self, status, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


class _Client:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def get(self, url, headers=None):
        resp = self._responses[self.calls]
        self.calls += 1
        return resp


def test_get_retries_on_429(monkeypatch):
    monkeypatch.setattr(ka.asyncio, "sleep", AsyncMock())  # no real backoff wait
    client = _Client([_Resp(429), _Resp(200, "<html>ok</html>")])
    resp = asyncio.run(ka._get(client, "u", headers={}))
    assert resp.text == "<html>ok</html>"
    assert client.calls == 2


def test_get_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr(ka.asyncio, "sleep", AsyncMock())
    client = _Client([_Resp(429)] * (ka.DETAIL_MAX_RETRIES + 1))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(ka._get(client, "u", headers={}))
    assert client.calls == ka.DETAIL_MAX_RETRIES + 1
