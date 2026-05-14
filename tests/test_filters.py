"""Filter composition tests.

WarWatchApp()._apply_filters is pure: given a list of row-like mappings
and the app's four filter reactives, it returns the subset. The tests
exercise each filter in isolation and in combination.

No Textual screen is mounted — we just build the app object and poke
the reactives directly, per the 'Testing patterns' section of the README.
"""
from __future__ import annotations

import pytest


def _row(id, confidence, event_type, summary, location=None):
    return {
        "id": id,
        "confidence": confidence,
        "event_type": event_type,
        "summary": summary,
        "location": location,
    }


def _rows() -> list:
    return [
        _row("1", "CONFIRMED", "AIRSTRIKE", "IDF strikes targets near Nabatieh", "Nabatieh"),
        _row("2", "REPORTED", "DIPLOMATIC", "Blinken warns over situation"),
        _row("3", "UNVERIFIED", "CASUALTY", "Claimed death toll in Gaza rises", "Gaza City"),
        _row("4", "CONFIRMED", "MARKET_MOVE", "OPEC+ agrees production cut"),
        _row("5", "REPORTED", "ROCKET_FIRE", "Rockets fired toward Kiryat Shmona", "Kiryat Shmona"),
    ]


def _app():
    """Construct the app without mounting — sufficient for _apply_filters."""
    from app import WarWatchApp
    return WarWatchApp()


def _ids(rows) -> set[str]:
    return {r["id"] for r in rows}


def test_no_filters_returns_all():
    a = _app()
    assert _ids(a._apply_filters(_rows())) == {"1", "2", "3", "4", "5"}


def test_confirmed_only():
    a = _app()
    a.filter_confidence = "CONFIRMED"
    assert _ids(a._apply_filters(_rows())) == {"1", "4"}


def test_hide_unverified():
    a = _app()
    a.filter_confidence = "NO_UNVERIFIED"
    # Row 3 is UNVERIFIED → dropped; everything else stays.
    assert _ids(a._apply_filters(_rows())) == {"1", "2", "4", "5"}


def test_kinetic_group():
    a = _app()
    a.filter_etype = "KINETIC"
    # KINETIC = {CLASH, GROUND_OP, AIRSTRIKE, ROCKET_FIRE, CASUALTY}
    assert _ids(a._apply_filters(_rows())) == {"1", "3", "5"}


def test_energy_group():
    a = _app()
    a.filter_etype = "ENERGY"
    # ENERGY = {SUPPLY_DISRUPTION, MARKET_MOVE}
    assert _ids(a._apply_filters(_rows())) == {"4"}


def test_search_substring():
    a = _app()
    a.filter_search = "gaza"
    # Row 3 has "Gaza" in summary + "Gaza City" in location — case-insensitive.
    assert _ids(a._apply_filters(_rows())) == {"3"}


def test_combined_confirmed_and_kinetic():
    a = _app()
    a.filter_confidence = "CONFIRMED"
    a.filter_etype = "KINETIC"
    # Only row 1 is both CONFIRMED and kinetic.
    assert _ids(a._apply_filters(_rows())) == {"1"}


def test_search_matches_location_when_summary_clean():
    a = _app()
    a.filter_search = "kiryat"
    assert _ids(a._apply_filters(_rows())) == {"5"}
