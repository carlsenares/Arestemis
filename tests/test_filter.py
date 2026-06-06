"""Tests for the criteria filter (pure, offline)."""

from __future__ import annotations

from datetime import date

from flatsniper import filter as F
from flatsniper.config import Config, RentTier, SearchConfig, Secrets
from flatsniper.models import Listing


def _search(**kw) -> SearchConfig:
    base = dict(
        min_sqm=28,
        rent_tiers=[RentTier(28, 1000), RentTier(40, 1200)],
        min_rooms=1,
        earliest_move_in=date(2026, 8, 1),
        areas=["München", "Garching bei München"],
        postal_codes=[],
    )
    base.update(kw)
    return SearchConfig(**base)


def _config(allow_wg=True, **kw) -> Config:
    return Config(
        search=_search(**kw),
        allow_empty_2person_wg=allow_wg,
        sources={},
        poll_interval_seconds=60,
        about_me="",
        secrets=Secrets("t", "c", "k", "m"),
    )


def _flat(sqm=45, rent=1100, **kw) -> Listing:
    base = dict(
        source="wg_gesucht", external_id="1", url="u", title="Helle Wohnung",
        sqm=sqm, rent_warm=rent, is_wg=False, raw={"city_name": "München"},
    )
    base.update(kw)
    return Listing(**base)


# --- rent_ok --------------------------------------------------------------

def test_rent_tiers():
    s = _search()
    assert F.rent_ok(_flat(sqm=45, rent=1100), s) is True   # 40+ tier, <=1200
    assert F.rent_ok(_flat(sqm=30, rent=950), s) is True    # 28 tier, <=1000
    assert F.rent_ok(_flat(sqm=30, rent=1050), s) is False  # too pricey for 28 tier
    assert F.rent_ok(_flat(sqm=50, rent=1250), s) is False  # over every tier
    assert F.rent_ok(_flat(sqm=25, rent=600), s) is False   # below min_sqm


def test_rent_unknown_data_fails():
    s = _search()
    assert F.rent_ok(_flat(sqm=None, rent=900), s) is False
    assert F.rent_ok(_flat(sqm=45, rent=None), s) is False


# --- area_ok --------------------------------------------------------------

def test_area_match():
    s = _search()
    assert F.area_ok(_flat(raw={"city_name": "München"}), s) is True
    assert F.area_ok(_flat(raw={"city_name": "Garching b. München"}), s) is True
    assert F.area_ok(_flat(title="Wohnung", raw={"city_name": "Augsburg"}), s) is False


def test_area_empty_passes():
    assert F.area_ok(_flat(raw={"city_name": "Augsburg"}, title="x"), _search(areas=[])) is True


# --- move_in_ok -----------------------------------------------------------

def test_move_in():
    s = _search()
    assert F.move_in_ok(_flat(raw={"available_to_date": "31.07.2026"}), s) is False  # ends too early
    assert F.move_in_ok(_flat(raw={"available_to_date": "31.12.2026"}), s) is True
    assert F.move_in_ok(_flat(raw={"available_to_date": "00.00.0000"}), s) is True   # open-ended
    assert F.move_in_ok(_flat(raw={}), s) is True


# --- prefilter / postclassify --------------------------------------------

def test_prefilter_single_flat():
    cfg = _config()
    assert F.passes_prefilter(_flat(sqm=45, rent=1100), cfg) is True
    assert F.passes_prefilter(_flat(sqm=45, rent=1300), cfg) is False  # rent fails


def _wg(size=2, sqm=55, rent=950, **kw) -> Listing:
    return _flat(is_wg=True, wg_size=size, sqm=sqm, rent_warm=rent,
                 title="2er WG München", **kw)


def test_prefilter_wg_gate():
    cfg = _config(allow_wg=True)
    assert F.passes_prefilter(_wg(size=2), cfg) is True       # 2-person WG passes gate
    assert F.passes_prefilter(_wg(size=3), cfg) is False      # not 2-person
    assert F.passes_prefilter(_wg(size=2), _config(allow_wg=False)) is False
    # rent is NOT applied at the gate for WGs (deferred to post-classify)
    assert F.passes_prefilter(_wg(size=2, rent=99999), cfg) is True


def test_postclassify_applies_rent():
    s = _search()
    assert F.passes_postclassify(_wg(sqm=55, rent=950), s) is True
    assert F.passes_postclassify(_wg(sqm=55, rent=1300), s) is False
