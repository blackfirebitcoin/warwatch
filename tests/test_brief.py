"""Tests for the brief subsystem's resume-time snapshot refresh."""
from __future__ import annotations

import brief


def _seed_market(monkeypatch, payloads: list[dict]) -> list[dict | None]:
    """Stub `models.latest_context` to return successive payloads.

    Returns the same list (callers can inspect/append). When the list is
    exhausted, returns None.
    """
    pulls = list(payloads)

    def fake_latest_context(_conn, kind):
        assert kind == "market"
        return pulls.pop(0) if pulls else None

    monkeypatch.setattr(brief.models, "latest_context", fake_latest_context)
    return pulls


def _stub_get_conn(monkeypatch):
    class _Conn:
        def close(self):
            pass
    monkeypatch.setattr(brief.models, "get_conn", lambda: _Conn())


def _snap(ts: str, brent: float = 84.0) -> dict:
    return {
        "fetched_at": ts,
        "spot": {"BZ=F": {"label": "Brent front month", "price": brent, "change_pct": 0.5}},
        "brent_curve": [],
        "errors": [],
    }


def test_resume_injects_refresh_when_snapshot_advances(monkeypatch):
    _stub_get_conn(monkeypatch)
    _seed_market(monkeypatch, [_snap("2026-05-01T12:30:00+00:00", brent=85.10)])

    thread = brief.BriefThread()
    thread._last_snapshot_fetched_at = "2026-05-01T12:00:00+00:00"

    out = thread._build_resume_prompt("what changed in the curve?")

    assert "Market snapshot refreshed since last turn" in out
    assert "2026-05-01T12:30:00" in out
    assert "85.10" in out
    assert out.endswith("what changed in the curve?")
    assert thread._last_snapshot_fetched_at == "2026-05-01T12:30:00+00:00"


def test_resume_passes_question_through_when_snapshot_unchanged(monkeypatch):
    _stub_get_conn(monkeypatch)
    _seed_market(monkeypatch, [_snap("2026-05-01T12:00:00+00:00")])

    thread = brief.BriefThread()
    thread._last_snapshot_fetched_at = "2026-05-01T12:00:00+00:00"

    out = thread._build_resume_prompt("any tanker news?")

    assert out == "any tanker news?"
    assert thread._last_snapshot_fetched_at == "2026-05-01T12:00:00+00:00"


def test_resume_passes_through_when_no_snapshot_available(monkeypatch):
    _stub_get_conn(monkeypatch)
    _seed_market(monkeypatch, [])

    thread = brief.BriefThread()
    thread._last_snapshot_fetched_at = "2026-05-01T12:00:00+00:00"

    out = thread._build_resume_prompt("status?")
    assert out == "status?"


def test_resume_skips_refresh_for_older_snapshot(monkeypatch):
    """Defensive: if latest_context somehow returns an older row (clock
    skew, race), don't inject a confusing 'refreshed' block."""
    _stub_get_conn(monkeypatch)
    _seed_market(monkeypatch, [_snap("2026-05-01T11:00:00+00:00")])

    thread = brief.BriefThread()
    thread._last_snapshot_fetched_at = "2026-05-01T12:00:00+00:00"

    out = thread._build_resume_prompt("update?")
    assert out == "update?"
    assert thread._last_snapshot_fetched_at == "2026-05-01T12:00:00+00:00"
