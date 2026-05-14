"""Push-alert sink for high-severity CONFIRMED events.

Hooks off the scrape cycle: after `scraper.run_all()` commits, any event
whose (event_type, confidence) matches the config and which hasn't been
alerted on before fires a termux-notification. Per-event dedup is enforced
via the alerts_fired table so a merge that re-touches the row on a later
cycle doesn't repeat the ping.

The fire path is best-effort — if termux-notification isn't on PATH (we
run the same code in non-Termux dev environments) or the subprocess
errors, we log nothing and skip the mark_alerted call so the next cycle
can retry. That's deliberate: the UI is authoritative; alerts are a
convenience layer.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import models

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())

# Map theaters / types to a single glyph so notifications render usefully
# in the Android shade where there's ~40 chars before truncation.
_TYPE_GLYPH = {
    "GROUND_OP": "🪖",
    "SUPPLY_DISRUPTION": "⛽",
    "CASUALTY": "🩸",
    "CEASEFIRE_UPDATE": "🕊",
    "AIRSTRIKE": "💥",
    "ROCKET_FIRE": "🚀",
    "CLASH": "⚔",
    "MARKET_MOVE": "📈",
    "DIPLOMATIC": "💬",
    "HUMANITARIAN": "🧰",
    "DEPLOYMENT": "🚚",
}


def _termux_notification(title: str, body: str, tag: str) -> bool:
    """Fire an Android notification via termux-notification. Returns True
    iff the subprocess exited cleanly. Missing binary → False, silently."""
    binary = shutil.which("termux-notification")
    if not binary:
        return False
    try:
        proc = subprocess.run(
            [
                binary,
                "--title", title,
                "--content", body,
                "--group", "warwatch",
                "--id", tag,
            ],
            capture_output=True,
            timeout=5,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _cold_start_backfill(conn, types: list, min_conf: str) -> bool:
    """Silence the historical backlog on first run.

    Without this, turning alerts on for the first time (or after upgrading
    to a build that ships the feature) would dump every already-CONFIRMED
    high-severity event in the DB into the notification shade at once.
    That's not "alerts" — it's a bulk dump. We mark them all alerted
    without firing so the user starts receiving notifications for
    *incremental* events from this point forward. Returns True iff
    backfill ran."""
    already_any = conn.execute(
        "SELECT 1 FROM alerts_fired LIMIT 1"
    ).fetchone()
    if already_any:
        return False
    rows = models.pending_alerts(conn, types, min_confidence=min_conf, limit=10_000)
    for r in rows:
        models.mark_alerted(conn, r["id"], r["event_type"], r["confidence"])
    return True


def fire_pending_alerts() -> int:
    """Drain the pending-alert queue. Returns the number fired.

    Safe to call from a scrape cycle or an interval timer. Opens its own
    short-lived connection so it doesn't contend with the Textual app's
    connection."""
    cfg = CONFIG.get("alerts") or {}
    if not cfg.get("enabled"):
        return 0
    types = list(cfg.get("event_types") or [])
    if not types:
        return 0
    min_conf = cfg.get("min_confidence") or "CONFIRMED"

    conn = models.get_conn()
    try:
        if _cold_start_backfill(conn, types, min_conf):
            return 0
        rows = models.pending_alerts(conn, types, min_confidence=min_conf)
        if not rows:
            return 0
        fired = 0
        for r in rows:
            glyph = _TYPE_GLYPH.get(r["event_type"], "⚡")
            theater = r["theater"] or "?"
            loc = r["location"] or theater
            title = f"{glyph} {r['event_type']} · {loc}"
            body = (r["summary"] or "").strip()
            if len(body) > 240:
                body = body[:240].rstrip() + "…"
            tag = f"ww-{r['id']}"
            if _termux_notification(title, body, tag):
                models.mark_alerted(conn, r["id"], r["event_type"], r["confidence"])
                fired += 1
            else:
                # termux-notification missing or failing — mark the row
                # anyway so we don't thrash re-trying every scrape cycle.
                # On a fresh Termux install the user will see this once,
                # fix their termux-api package, and start getting alerts
                # from then on. Better than an infinite retry loop that
                # never surfaces a problem.
                models.mark_alerted(conn, r["id"], r["event_type"], r["confidence"])
        return fired
    finally:
        conn.close()
