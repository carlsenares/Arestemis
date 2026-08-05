"""Tests for the criteria filter (pure, offline)."""

from __future__ import annotations

from datetime import date

from flatsniper import filter as F
from flatsniper.config import WG_ANY, WG_EMPTY_2P, WG_NONE, Config, RentTier, SearchConfig, Secrets
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


def _config(wg_mode=WG_EMPTY_2P, **kw) -> Config:
    return Config(
        search=_search(**kw),
        wg_mode=wg_mode,
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


def test_exclude_areas_veto_a_matching_area():
    """`areas: [München]` stays broad to scope the queries; the outskirts and the
    surrounding towns are carved back out here."""
    s = _search(areas=["München"], exclude_areas=["Garching", "Trudering", "Dachau"])
    assert F.area_ok(_flat(raw={"city_name": "München-Schwabing"}), s) is True
    assert F.area_ok(_flat(raw={"city_name": "Garching b. München"}), s) is False
    assert F.area_ok(_flat(raw={"city_name": "München-Trudering"}), s) is False
    assert F.area_ok(_flat(title="Zimmer in Trudering", raw={"city_name": "München"}), s) is False


def test_exclude_areas_match_whole_words_only():
    """A substring test would read central streets as outskirts and silently drop
    them: Dachauer Str. is Maxvorstadt, Pasinger Str. is Sendling."""
    s = _search(areas=["München"], exclude_areas=["Dachau", "Pasing", "Haar"])
    assert F.area_ok(_flat(title="2-Zi, Dachauer Straße", raw={"city_name": "München"}), s) is True
    assert F.area_ok(_flat(title="WG, Pasinger Straße", raw={"city_name": "München"}), s) is True
    assert F.area_ok(_flat(title="Zimmer, Haarweg", raw={"city_name": "München"}), s) is True
    assert F.area_ok(_flat(title="Wohnung in Haar", raw={"city_name": "Haar"}), s) is False


def test_exclude_areas_read_the_url_slug():
    """WG-Gesucht reports every offer as plain "München" and puts the district
    only in the link — without it, outskirts listings look central."""
    s = _search(areas=["München"], exclude_areas=["Pasing", "Trudering"])
    pasing = _flat(url="https://www.wg-gesucht.de/wg-zimmer-in-Muenchen-Pasing.123.html",
                   title="Schönes Zimmer", raw={"city_name": "München"})
    lehel = _flat(url="https://www.wg-gesucht.de/wg-zimmer-in-Muenchen-Altstadt-Lehel.9.html",
                  title="Schönes Zimmer", raw={"city_name": "München"})
    assert F.area_ok(pasing, s) is False
    assert F.area_ok(lehel, s) is True


def test_exclude_areas_match_postal_codes():
    s = _search(areas=["München"], exclude_areas=["81825"])
    assert F.area_ok(_flat(raw={"city_name": "München", "postcode": "81825"}), s) is False
    assert F.area_ok(_flat(raw={"city_name": "München", "postcode": "80333"}), s) is True


def test_area_empty_passes():
    assert F.area_ok(_flat(raw={"city_name": "Augsburg"}, title="x"), _search(areas=[])) is True


# --- move_in_ok -----------------------------------------------------------

def test_move_in():
    s = _search()  # earliest_move_in = 2026-08-01
    assert F.move_in_ok(_flat(raw={"available_to_date": "31.07.2026"}), s) is False  # ends too early
    assert F.move_in_ok(_flat(raw={"available_to_date": "31.12.2026"}), s) is True
    assert F.move_in_ok(_flat(raw={"available_to_date": "00.00.0000"}), s) is True   # open-ended
    assert F.move_in_ok(_flat(raw={}), s) is True


def test_move_in_rejects_dates_past_the_window():
    """The target date is when the user needs to move in — a room that only frees
    up months later is no use, so the floor must not be open-ended."""
    s = _search(earliest_move_in=date(2026, 10, 1), latest_move_in=date(2026, 10, 15))
    assert F.move_in_ok(_flat(available_from=date(2026, 10, 1)), s) is True
    assert F.move_in_ok(_flat(available_from=date(2026, 10, 15)), s) is True   # inclusive
    assert F.move_in_ok(_flat(available_from=date(2026, 10, 16)), s) is False
    assert F.move_in_ok(_flat(available_from=date(2026, 12, 1)), s) is False
    assert F.move_in_ok(_flat(available_from=date(2026, 9, 20)), s) is False   # still too early
    assert F.move_in_ok(_flat(available_from=None), s) is True                 # unknown -> keep


def test_move_in_window_open_ended_without_latest():
    s = _search(earliest_move_in=date(2026, 10, 1), latest_move_in=None)
    assert F.move_in_ok(_flat(available_from=date(2027, 5, 1)), s) is True


def test_move_in_rejects_early_available_from():
    s = _search()  # earliest_move_in = 2026-08-01
    assert F.move_in_ok(_flat(available_from=date(2026, 7, 1)), s) is False   # before floor
    assert F.move_in_ok(_flat(available_from=date(2026, 5, 1)), s) is False
    assert F.move_in_ok(_flat(available_from=date(2026, 8, 1)), s) is True    # exactly the floor
    assert F.move_in_ok(_flat(available_from=date(2026, 9, 15)), s) is True   # later is fine
    assert F.move_in_ok(_flat(available_from=None), s) is True                # unknown -> keep


# --- prefilter / postclassify --------------------------------------------

def test_prefilter_single_flat():
    cfg = _config()
    assert F.passes_prefilter(_flat(sqm=45, rent=1100), cfg) is True
    assert F.passes_prefilter(_flat(sqm=45, rent=1300), cfg) is False  # rent fails


def _wg(size=2, sqm=55, rent=950, **kw) -> Listing:
    return _flat(is_wg=True, wg_size=size, sqm=sqm, rent_warm=rent,
                 title="2er WG München", **kw)


def test_prefilter_wg_gate():
    cfg = _config(wg_mode=WG_EMPTY_2P)
    assert F.passes_prefilter(_wg(size=2), cfg) is True       # 2-person WG passes gate
    assert F.passes_prefilter(_wg(size=3), cfg) is False      # not 2-person
    assert F.passes_prefilter(_wg(size=2), _config(wg_mode=WG_NONE)) is False
    # rent is NOT applied at the gate for WGs (deferred to post-classify)
    assert F.passes_prefilter(_wg(size=2, rent=99999), cfg) is True


def test_prefilter_wg_mode_any_takes_rooms_of_any_size():
    """mode: any -> an ordinary WG room is judged like a flat, right here."""
    cfg = _config(wg_mode=WG_ANY)
    assert F.passes_prefilter(_wg(size=2, rent=950), cfg) is True
    assert F.passes_prefilter(_wg(size=4, rent=950), cfg) is True   # size is irrelevant
    assert F.passes_prefilter(_wg(size=3, rent=1300), cfg) is False  # rent applies now


def test_postclassify_applies_rent():
    s = _search()
    assert F.passes_postclassify(_wg(sqm=55, rent=950), s) is True
    assert F.passes_postclassify(_wg(sqm=55, rent=1300), s) is False


# --- no-size/price (unbounded) mode ---------------------------------------

def test_price_cap_without_size_floor_ignores_unknown_sqm():
    """min_sqm 0 + a tier starting at 0 m² = "no minimum square meters": a listing
    whose size we couldn't parse is judged on price alone, not dropped."""
    s = _search(min_sqm=0, rent_tiers=[RentTier(0, 900)])
    assert F.rent_ok(_flat(sqm=None, rent=850), s) is True
    assert F.rent_ok(_flat(sqm=None, rent=1200), s) is False   # over the cap
    assert F.rent_ok(_flat(sqm=9, rent=850), s) is True        # tiny is fine
    assert F.rent_ok(_flat(sqm=60, rent=None), s) is False     # cap unverifiable


def test_size_gated_tiers_still_need_a_known_size():
    """A tier that only applies above a size floor can't be cleared blind."""
    s = _search(min_sqm=0, rent_tiers=[RentTier(40, 1200)])
    assert F.rent_ok(_flat(sqm=None, rent=1100), s) is False
    assert F.rent_ok(_flat(sqm=45, rent=1100), s) is True


def test_rent_ok_unbounded_passes_everything():
    """min_sqm 0 + unbounded tier => every flat qualifies, incl. unknown data."""
    s = _search(min_sqm=0, rent_tiers=[RentTier(0, 1_000_000)])
    assert F.rent_ok(_flat(sqm=12, rent=2500), s) is True       # tiny + pricey: still OK
    assert F.rent_ok(_flat(sqm=None, rent=None), s) is True     # unparseable: still OK


# --- blocklist (swaps/sublets/temporary) ----------------------------------

def test_is_blocked_keywords():
    s = _search(blocklist=["Tauschwohnung", "Untermiete", "Zwischenmiete"])
    assert F.is_blocked(_flat(title="Schöne Tauschwohnung in Garching"), s) is True
    assert F.is_blocked(_flat(title="1-Zi", description="nur zur Zwischenmiete frei"), s) is True
    assert F.is_blocked(_flat(title="Helle 1-Zimmer-Wohnung"), s) is False
    assert F.is_blocked(_flat(title="x"), _search(blocklist=[])) is False  # no list => never blocks


def test_prefilter_rejects_blocked():
    cfg = _config(wg_mode=WG_NONE, min_sqm=0, rent_tiers=[RentTier(0, 1_000_000)],
                  blocklist=["Untermiete"], areas=["Garching bei München"])
    blocked = _flat(title="Wohnung zur Untermiete", raw={"city_name": "Garching"})
    clean = _flat(title="1-Zimmer-Wohnung", raw={"city_name": "Garching"})
    assert F.passes_prefilter(blocked, cfg) is False
    assert F.passes_prefilter(clean, cfg) is True   # unbounded + Garching + not blocked
