"""Coverage for the data-hygiene + FTS5 surface added in Round 2.

These exercise the real SQLite path through the tmp_db fixture so the
schema migration + triggers + retention logic stay honest.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import models
from models import Event, upsert_event


# ---------- FTS5 search ----------


def _ev(summary: str, *, location="Nabatieh", source_name="src",
        theater="LEBANON", event_type="AIRSTRIKE",
        minutes_ago: int = 0) -> Event:
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return Event(
        timestamp=ts,
        summary=summary,
        event_type=event_type,
        theater=theater,
        location=location,
        sources=[{"name": source_name, "url": f"https://example/{source_name}"}],
        confidence="REPORTED",
    )


def test_search_events_finds_token(tmp_db):
    upsert_event(tmp_db, _ev("Israeli airstrike on Nabatieh kills three"))
    upsert_event(tmp_db, _ev("OPEC announces production cut",
                              location=None, theater="ENERGY",
                              event_type="MARKET_MOVE",
                              source_name="opec"))
    rows = models.search_events(tmp_db, "airstrike")
    assert len(rows) == 1
    assert "airstrike" in rows[0]["summary"].lower()


def test_search_events_filters_by_theater(tmp_db):
    upsert_event(tmp_db, _ev("oil tanker attack near Hormuz",
                              location="Hormuz", theater="ENERGY",
                              event_type="SUPPLY_DISRUPTION",
                              source_name="lloyds"))
    upsert_event(tmp_db, _ev("oil convoy reported in southern Lebanon",
                              source_name="reuters"))
    rows = models.search_events(tmp_db, "oil", theater="ENERGY")
    assert len(rows) == 1
    assert rows[0]["theater"] == "ENERGY"


def test_search_events_handles_quotes_and_empty(tmp_db):
    upsert_event(tmp_db, _ev("ceasefire holds in south litani"))
    # Quoted multi-word query must not blow up the FTS5 parser.
    # NB: search_events strips the user's quotes and re-quotes each token,
    # so '"south litani"' becomes 'south' AND 'litani' — both must hit.
    rows = models.search_events(tmp_db, '"south litani"')
    assert rows, f"quoted query returned no rows: {rows!r}"
    # Empty / whitespace queries return no rows, never crash.
    assert models.search_events(tmp_db, "") == []
    assert models.search_events(tmp_db, "   ") == []


def test_fts_index_updates_on_event_change(tmp_db):
    upsert_event(tmp_db, _ev("airstrike on Tyre"))
    # Direct UPDATE to a new summary — exercises the AFTER UPDATE
    # trigger that should refresh the FTS row.
    tmp_db.execute(
        "UPDATE events SET summary = ? WHERE summary = ?",
        ("airstrike on Tyre with multiple casualties reported",
         "airstrike on Tyre"),
    )
    tmp_db.commit()
    rows = models.search_events(tmp_db, "casualties")
    assert rows, f"FTS update trigger did not propagate (search_events returned {rows})"


# ---------- retention ----------


def _stale_log_row(conn, source: str, days_ago: int) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    conn.execute(
        "INSERT INTO scrape_log (source,timestamp,status,events_found) "
        "VALUES (?,?,?,?)",
        (source, ts, "ok", 0),
    )
    conn.commit()


def test_prune_scrape_log_drops_stale_rows(tmp_db):
    _stale_log_row(tmp_db, "old_src", days_ago=30)
    _stale_log_row(tmp_db, "fresh_src", days_ago=1)
    n = models.prune_scrape_log(tmp_db, retention_days=14)
    assert n == 1
    remaining = tmp_db.execute("SELECT source FROM scrape_log").fetchall()
    assert {r["source"] for r in remaining} == {"fresh_src"}


def test_prune_scrape_log_disabled_when_zero(tmp_db):
    _stale_log_row(tmp_db, "old_src", days_ago=999)
    assert models.prune_scrape_log(tmp_db, retention_days=0) == 0
    assert tmp_db.execute("SELECT COUNT(*) FROM scrape_log").fetchone()[0] == 1


def test_prune_alerts_fired_drops_stale(tmp_db):
    old = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
    new = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    tmp_db.execute(
        "INSERT INTO alerts_fired (event_id, fired_at, event_type, confidence)"
        " VALUES ('e_old', ?, 'AIRSTRIKE', 'CONFIRMED')", (old,))
    tmp_db.execute(
        "INSERT INTO alerts_fired (event_id, fired_at, event_type, confidence)"
        " VALUES ('e_new', ?, 'AIRSTRIKE', 'CONFIRMED')", (new,))
    tmp_db.commit()
    n = models.prune_alerts_fired(tmp_db, retention_days=60)
    assert n == 1
    rows = tmp_db.execute("SELECT event_id FROM alerts_fired").fetchall()
    assert {r["event_id"] for r in rows} == {"e_new"}


# ---------- backfill ----------


def test_backfill_first_seen_at_fills_nulls(tmp_db):
    upsert_event(tmp_db, _ev("test event"))
    # Force the column to NULL to simulate a legacy row.
    tmp_db.execute("UPDATE events SET first_seen_at = NULL")
    tmp_db.commit()
    assert tmp_db.execute(
        "SELECT first_seen_at FROM events"
    ).fetchone()[0] is None

    n = models.backfill_first_seen_at(tmp_db)
    assert n == 1
    val = tmp_db.execute("SELECT first_seen_at FROM events").fetchone()[0]
    assert val is not None and val != ""


def test_backfill_is_idempotent(tmp_db):
    upsert_event(tmp_db, _ev("test event"))
    # All rows already have first_seen_at — second call should be a no-op.
    assert models.backfill_first_seen_at(tmp_db) == 0


# ---------- audit ----------


def test_data_audit_reports_counts(tmp_db):
    upsert_event(tmp_db, _ev("airstrike alpha", location="Tyre"))
    upsert_event(tmp_db, _ev("oil tanker incident", source_name="lloyds",
                              location="Hormuz", theater="ENERGY",
                              event_type="SUPPLY_DISRUPTION"))
    audit = models.data_audit(tmp_db)
    assert audit["events_total"] == 2
    assert audit["missing_first_seen"] == 0
    assert audit["other_theater"] == 0
    assert audit["empty_summary"] == 0


def test_data_audit_flags_unconfirmed_with_two_sources(tmp_db):
    # Insert a hand-crafted bad row: two sources but UNVERIFIED confidence
    # — this is the merge-path bug data_audit watches for.
    tmp_db.execute(
        """INSERT INTO events
            (id, timestamp, location, event_type, summary, sources,
             confidence, theater)
           VALUES ('bad1', ?, 'Beirut', 'AIRSTRIKE', 'something hit',
                   '[{"name":"a"},{"name":"b"}]', 'UNVERIFIED', 'LEBANON')""",
        (datetime.now(timezone.utc).isoformat(),),
    )
    tmp_db.commit()
    audit = models.data_audit(tmp_db)
    assert audit["unconfirmed_with_2plus_sources"] == 1


# ---------- theater_counts_since (the consolidated 6-into-1 query) ----------


def test_theater_counts_since_returns_per_theater_buckets(tmp_db):
    upsert_event(tmp_db, _ev("strike A"))
    upsert_event(tmp_db, _ev("strike B", source_name="other"))
    upsert_event(tmp_db, _ev("oil tanker attack", theater="ENERGY",
                              event_type="SUPPLY_DISRUPTION",
                              location="Hormuz", source_name="lloyds"))
    counts = models.theater_counts_since(tmp_db, hours=24)
    assert counts.get("LEBANON", 0) >= 1
    assert counts.get("ENERGY", 0) == 1


# ---------- VACUUM ----------


def test_vacuum_analyze_runs_clean(tmp_db):
    # Smoke test: VACUUM cannot run inside a transaction, so the helper's
    # commit() must come first. Just call it and assert no exception.
    upsert_event(tmp_db, _ev("test event"))
    models.vacuum_analyze(tmp_db)
    # Connection still usable afterwards.
    assert tmp_db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_search_events_supports_prefix(tmp_db):
    """User-typed `hez*` should match hezbollah etc."""
    upsert_event(tmp_db, _ev("Hezbollah claims responsibility"))
    upsert_event(tmp_db, _ev("Hellish weather conditions reported",
                              source_name="other"))
    rows = models.search_events(tmp_db, "hez*")
    assert any("hezbollah" in (r["summary"] or "").lower() for r in rows)
