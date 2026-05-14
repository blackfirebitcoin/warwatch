"""Curated classifier cases.

Each tuple is (expected_event_type, headline). When a new miscategorization
shows up in the feed, add a case here. Target is 100% pass.

The priority order (README Classification section) is the invariant we're
protecting: SUPPLY_DISRUPTION above AIRSTRIKE when a hydrocarbon target
is named, CLASH/GROUND_OP above AIRSTRIKE when the kinetic pattern is
explicit, DIPLOMATIC below every kinetic category so "Lebanon says
Israeli attack killed 13" still classifies as CASUALTY.
"""
from __future__ import annotations

import pytest

from scraper import classify, is_relevant, is_commodity_relevant


CLASSIFIER_CASES: list[tuple[str, str]] = [
    # AIRSTRIKE — actor + kinetic verb
    ("AIRSTRIKE", "Israeli army strikes over 200 Hezbollah targets in past 24 hours"),
    ("AIRSTRIKE", "IDF warplanes hit military positions in southern Lebanon"),
    ("AIRSTRIKE", "Iranian forces struck ISIS bases in eastern Syria"),
    ("AIRSTRIKE", "Israel launches fresh airstrikes on Beirut suburb"),
    ("AIRSTRIKE", "US conducted precision strike on Iranian-backed militia"),

    # ROCKET_FIRE
    ("ROCKET_FIRE", "Hezbollah fires rocket barrage toward Kiryat Shmona"),
    ("ROCKET_FIRE", "Iron Dome intercepts drone over northern Israel"),

    # CLASH — mutual kinetic
    ("CLASH", "Mutual attacks reported along Blue Line as forces exchange fire"),
    ("CLASH", "Cross-border fire continues between IDF and Hezbollah units"),

    # GROUND_OP
    ("GROUND_OP", "Israeli ground operation expands into Maroun al-Ras"),
    ("GROUND_OP", "IDF brigade entered Lebanese village, encircling positions"),

    # CASUALTY — the precipitating verb outranks DIPLOMATIC "says"
    ("CASUALTY", "Lebanon says Israeli attack killed 13 civilians in Nabatieh"),
    ("CASUALTY", "Health ministry: 22 killed, 45 wounded in overnight strikes"),

    # CEASEFIRE_UPDATE
    ("CEASEFIRE_UPDATE", "Lebanon-Israel ceasefire agreement takes effect at 4am"),
    ("CEASEFIRE_UPDATE", "Truce holds for 48 hours despite sporadic violations"),

    # SUPPLY_DISRUPTION — hydrocarbon target gates it above AIRSTRIKE
    ("SUPPLY_DISRUPTION", "Iranian drone hit oil tanker off Hormuz"),
    ("SUPPLY_DISRUPTION", "Pipeline explosion disrupts Saudi crude output"),
    ("SUPPLY_DISRUPTION", "Refinery fire forces Rotterdam shutdown"),
    ("SUPPLY_DISRUPTION", "Houthi forces boarded vessel in Red Sea"),

    # MARKET_MOVE — policy with measurable consequence
    ("MARKET_MOVE", "OPEC+ agrees on 1 million barrel per day production cut"),
    ("MARKET_MOVE", "US announces SPR release to ease prices"),
    ("MARKET_MOVE", "EU imposes embargo on Russian crude"),

    # DIPLOMATIC — broad verbs, safe because kinetic categories outrank it
    ("DIPLOMATIC", "Sheikh Qassem: enemy resorted to bloody crimes against civilians"),
    ("DIPLOMATIC", "Nasrallah vows to retaliate within days"),
    ("DIPLOMATIC", "Iran accuses Israel of aggression at UN Security Council"),
    ("DIPLOMATIC", "Blinken warns Netanyahu over civilian conditions"),

    # HUMANITARIAN
    ("HUMANITARIAN", "Aid convoy reaches displaced families in Tyre"),

    # DEPLOYMENT — fallback
    ("DEPLOYMENT", "IDF reinforces northern command with reserve battalion"),
]


@pytest.mark.parametrize("expected,headline", CLASSIFIER_CASES)
def test_classifier_curated(expected, headline):
    got = classify(headline)
    assert got == expected, (
        f"classify({headline!r}) = {got!r}, expected {expected!r}"
    )


# Relevance gate — conflict lane rejects noise, admits kinetic verbs.
RELEVANCE_NOISE: list[str] = [
    "Port workers strike in Haifa over wage dispute",
    "Dock workers begin general strike at Ashdod",
    "Celebrity chef opens new restaurant in Beirut",
    "Podcast discusses history of Middle East conflict",
    "Oil prices rise on macroeconomic concerns",  # conflict blocklist
]


@pytest.mark.parametrize("headline", RELEVANCE_NOISE)
def test_conflict_relevance_rejects_noise(headline):
    assert not is_relevant(headline), (
        f"is_relevant({headline!r}) should be False — noise leaked through"
    )


RELEVANCE_KEEPERS: list[str] = [
    "Israeli strikes hit Hezbollah positions near Nabatieh",
    "Rocket fire from Gaza triggers sirens in Ashkelon",
    "Clashes reported along the Blue Line",
]


@pytest.mark.parametrize("headline", RELEVANCE_KEEPERS)
def test_conflict_relevance_keeps_kinetic(headline):
    assert is_relevant(headline), (
        f"is_relevant({headline!r}) should be True — kinetic event was rejected"
    )


# Commodity gate — admits market/price framing, rejects universal noise.
COMMODITY_KEEPERS: list[str] = [
    "OPEC announces production quota cut",
    "Tanker traffic slows through Strait of Hormuz",
    "Refinery fire in Rotterdam disrupts European fuel supply",
    "SPR release of 5 million barrels begins Monday",
]


@pytest.mark.parametrize("headline", COMMODITY_KEEPERS)
def test_commodity_relevance_keeps_market_framing(headline):
    assert is_commodity_relevant(headline), (
        f"is_commodity_relevant({headline!r}) should be True — "
        "market/price framing is the point of the energy gate"
    )
