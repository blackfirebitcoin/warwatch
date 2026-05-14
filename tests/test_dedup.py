"""Dedup integration tests using the three-pass fuzzy matcher.

These exercise the real SQLite code path via `upsert_event` so the test
tracks the production dedup logic verbatim — not a reimplementation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from models import Event, upsert_event


def _ev(
    *,
    minutes_ago: int = 0,
    summary: str,
    event_type: str = "AIRSTRIKE",
    theater: str = "LEBANON",
    location: str | None = "Nabatieh",
    source_name: str,
) -> Event:
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return Event(
        timestamp=ts,
        summary=summary,
        event_type=event_type,
        theater=theater,
        location=location,
        sources=[{"name": source_name, "attribution": "test"}],
    )


def test_same_incident_different_outlets_merges_to_confirmed(tmp_db):
    """Two outlets reporting the same strike in the same town should merge
    and the merged row should flip to CONFIRMED (≥2 distinct sources)."""
    conn = tmp_db
    assert upsert_event(conn, _ev(
        minutes_ago=5,
        summary="Israeli airstrike hit Hezbollah positions in Nabatieh",
        source_name="Reuters",
    )) == "new"
    assert upsert_event(conn, _ev(
        minutes_ago=8,
        summary="IDF struck Hezbollah sites in Nabatieh",
        source_name="Al Jazeera",
    )) == "merged"

    rows = conn.execute("SELECT confidence, sources FROM events").fetchall()
    assert len(rows) == 1
    assert rows[0]["confidence"] == "CONFIRMED"


def test_distant_events_dont_merge(tmp_db):
    """Different town + different time window → two separate rows."""
    conn = tmp_db
    upsert_event(conn, _ev(
        minutes_ago=5,
        summary="Israeli airstrike hit positions in Nabatieh",
        location="Nabatieh",
        source_name="Reuters",
    ))
    upsert_event(conn, _ev(
        minutes_ago=5,
        summary="Israeli airstrike hit positions in Khiam",
        location="Khiam",
        source_name="Al Jazeera",
    ))
    rows = conn.execute("SELECT COUNT(*) c FROM events").fetchone()
    assert rows["c"] == 2


def test_different_theaters_dont_merge(tmp_db):
    """Pass 2 and 3 gate on theater — similar summaries across theaters
    must stay separate."""
    conn = tmp_db
    upsert_event(conn, _ev(
        summary="Drone strike hit target in Nabatieh",
        theater="LEBANON",
        source_name="A",
    ))
    upsert_event(conn, _ev(
        summary="Drone strike hit target in Gaza",
        theater="GAZA",
        location="Gaza City",
        source_name="B",
    ))
    rows = conn.execute("SELECT COUNT(*) c FROM events").fetchone()
    assert rows["c"] == 2


def test_pass3_merges_locationless_cluster_duplicates(tmp_db):
    """The Pass 3 motivator: two outlets, no location on either row, same
    theater, same type cluster, heavy token overlap → should still merge."""
    conn = tmp_db
    assert upsert_event(conn, _ev(
        minutes_ago=10,
        summary="Israeli army carried out 200 airstrikes on Hezbollah targets in Lebanon",
        location=None,
        source_name="Times of Israel",
    )) == "new"
    assert upsert_event(conn, _ev(
        minutes_ago=120,
        summary="Israeli army strikes over 200 Hezbollah targets in past 24 hours",
        location=None,
        source_name="Reuters",
    )) == "merged"

    rows = conn.execute("SELECT confidence FROM events").fetchall()
    assert len(rows) == 1
    assert rows[0]["confidence"] == "CONFIRMED"


def test_pass3_energy_lower_threshold_catches_outlet_paraphrase(tmp_db):
    """ENERGY-lane headlines from different outlets reframe the same
    event with analyst-attribution / financial-angle wording, landing
    at token-Jaccard ~0.22-0.29. The lowered ENERGY threshold (0.22 / 3
    shared) catches them; the conflict-lane default (0.30 / 4) would
    not."""
    conn = tmp_db
    assert upsert_event(conn, _ev(
        minutes_ago=10,
        summary="Goldman says UAE's exit from OPEC raises medium-term oil supply upside risk",
        event_type="DIPLOMATIC",
        theater="ENERGY",
        location=None,
        source_name="Reuters Energy",
    )) == "new"
    assert upsert_event(conn, _ev(
        minutes_ago=300,
        summary="OPEC Will Survive UAE Exit, But Medium-Term Supply Threat Is Real",
        event_type="DIPLOMATIC",
        theater="ENERGY",
        location=None,
        source_name="Oilprice.com",
    )) == "merged"
    rows = conn.execute("SELECT confidence FROM events").fetchall()
    assert len(rows) == 1
    assert rows[0]["confidence"] == "CONFIRMED"


def test_pass3_energy_does_not_merge_unrelated_opec_stories(tmp_db):
    """False-positive guard: two distinct ENERGY MARKET_MOVE headlines
    sharing only generic OPEC member tokens (opec, algeria, iraq) but
    describing different events — quota compliance vs. summit agenda —
    must stay separate. With 3 shared tokens / Jaccard ~0.23 they would
    merge under the previous ENERGY thresholds (0.22 / 3) but Pass 3 now
    requires ≥4 shared tokens / Jaccard ≥0.28."""
    conn = tmp_db
    upsert_event(conn, _ev(
        minutes_ago=10,
        summary="OPEC quota compliance falling Algeria Iraq overproducing reports",
        event_type="MARKET_MOVE",
        theater="ENERGY",
        location=None,
        source_name="Reuters Energy",
    ))
    upsert_event(conn, _ev(
        minutes_ago=300,
        summary="OPEC summit Vienna agenda Algeria Iraq position discussed",
        event_type="MARKET_MOVE",
        theater="ENERGY",
        location=None,
        source_name="Bloomberg Energy",
    ))
    rows = conn.execute("SELECT COUNT(*) c FROM events").fetchone()
    assert rows["c"] == 2


def test_pass3_energy_does_not_merge_unrelated_tanker_stories(tmp_db):
    """False-positive guard: two distinct ENERGY SUPPLY_DISRUPTION
    headlines sharing only {tanker, sanctions, europe} — one about
    Russian export disruption to western Europe, the other about
    Russian production rerouting to Asia — must stay separate. Jaccard
    ~0.23, only one shared bigram (oil, tanker), so Pass 4 doesn't
    catch them either."""
    conn = tmp_db
    upsert_event(conn, _ev(
        minutes_ago=10,
        summary="Moscow oil tanker disruption sanctions western Europe shipments declining",
        event_type="SUPPLY_DISRUPTION",
        theater="ENERGY",
        location=None,
        source_name="Lloyd's List",
    ))
    upsert_event(conn, _ev(
        minutes_ago=300,
        summary="Russia oil tanker production sanctions Asian Europe markets rising",
        event_type="SUPPLY_DISRUPTION",
        theater="ENERGY",
        location=None,
        source_name="Reuters Energy",
    ))
    rows = conn.execute("SELECT COUNT(*) c FROM events").fetchone()
    assert rows["c"] == 2


def test_pass4_bigram_merges_when_token_jaccard_below_floor(tmp_db):
    """Pass 4 fires when 3 contiguous content tokens are shared (≥ 2
    shared adjacent bigrams) but total token-Jaccard is too low and
    shared-token count too small for Pass 3 to merge.

    Both headlines name a 'Hamas senior commander' incident but spell
    out the rest of the sentence with non-overlapping vocabulary, so
    shared tokens = 3 (floor for Pass 3 conflict-lane is 4) and
    Jaccard = 3/14 ≈ 0.21 (Pass 3 conflict-lane wants ≥ 0.30). The
    shared bigrams (hamas,senior) and (senior,commander) carry the
    real signal."""
    conn = tmp_db
    assert upsert_event(conn, _ev(
        minutes_ago=10,
        summary="Hamas senior commander killed by special forces airstrike Gaza",
        event_type="AIRSTRIKE",
        theater="GAZA",
        location=None,
        source_name="Times of Israel",
    )) == "new"
    assert upsert_event(conn, _ev(
        minutes_ago=200,
        summary="Hamas senior commander died after Israeli army covert raid mission",
        event_type="AIRSTRIKE",
        theater="GAZA",
        location=None,
        source_name="Reuters Middle East",
    )) == "merged"
    rows = conn.execute("SELECT confidence FROM events").fetchall()
    assert len(rows) == 1
    assert rows[0]["confidence"] == "CONFIRMED"


def test_pass4_does_not_conflate_unrelated_lebanon_strikes(tmp_db):
    """False-positive guard: two distinct Lebanon airstrike headlines
    share generic tokens {israeli, lebanon, southern, strike} but
    target different towns and have no shared 2-word phrases — must
    stay separate. Without the bigram constraint a uniformly-lower
    Pass-3 threshold would merge them."""
    conn = tmp_db
    upsert_event(conn, _ev(
        minutes_ago=10,
        summary="Israeli drone strike targeted Al-Ramadiyah in southern Lebanon",
        event_type="AIRSTRIKE",
        theater="LEBANON",
        location=None,
        source_name="LiveUAMap Lebanon",
    ))
    upsert_event(conn, _ev(
        minutes_ago=240,
        summary="Israeli strike kills five family members in Lebanon",
        event_type="AIRSTRIKE",
        theater="LEBANON",
        location=None,
        source_name="Al Jazeera English",
    ))
    rows = conn.execute("SELECT COUNT(*) c FROM events").fetchone()
    assert rows["c"] == 2


def test_type_reconciliation_uses_priority(tmp_db):
    """When merging, the higher-priority event_type should win.
    AIRSTRIKE(75) should beat DEPLOYMENT(20) when the same incident
    is ingested from both angles."""
    conn = tmp_db
    upsert_event(conn, _ev(
        minutes_ago=5,
        summary="IDF reinforces positions near Nabatieh with reserves",
        event_type="DEPLOYMENT",
        source_name="Source1",
    ))
    upsert_event(conn, _ev(
        minutes_ago=8,
        summary="IDF reinforces positions near Nabatieh with reserves",
        event_type="AIRSTRIKE",
        source_name="Source2",
    ))
    rows = conn.execute("SELECT event_type FROM events").fetchall()
    assert len(rows) == 1
    assert rows[0]["event_type"] == "AIRSTRIKE"
