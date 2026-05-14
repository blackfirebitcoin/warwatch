"""WARWATCH SITREP generator."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from models import get_conn, events_since

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())

THEATER_ORDER = ["LEBANON", "IRAN", "GAZA", "SYRIA", "YEMEN", "ENERGY"]

# Sitrep bullets are sorted so CONFIRMED items rise to the top within
# each category — readers scan the first 1–3 bullets per section, so
# burying a multi-source strike under a single-source unverified claim
# defeats the purpose of the confidence tier.
_CONF_RANK = {"CONFIRMED": 0, "REPORTED": 1, "UNVERIFIED": 2}


def _conf_rank(e) -> int:
    return _CONF_RANK.get(e["confidence"], 3)


def _local_hm(iso_ts: str) -> str:
    if not iso_ts:
        return "--:--"
    try:
        s = iso_ts[:-1] + "+00:00" if iso_ts.endswith("Z") else iso_ts
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%H:%M")
    except Exception:
        return iso_ts[11:16] if len(iso_ts) >= 16 else "--:--"


def theater_status(events, theater: str | None = None) -> str:
    types = {e["event_type"] for e in events}
    # ENERGY theater has its own status vocabulary — "ACTIVE COMBAT" is
    # meaningless for commodity feeds; supply-side disruptions and market
    # policy moves are what matter there.
    if theater == "ENERGY":
        if not events:
            return "QUIET"
        n_supply = sum(1 for e in events if e["event_type"] == "SUPPLY_DISRUPTION")
        n_market = sum(1 for e in events if e["event_type"] == "MARKET_MOVE")
        if n_supply >= 2:
            return "SUPPLY DISRUPTED"
        if n_supply >= 1:
            return "SUPPLY INCIDENT"
        if n_market >= 2:
            return "MARKETS ACTIVE"
        if n_market >= 1:
            return "POLICY MOVE"
        return "LOW"
    if "CLASH" in types or "GROUND_OP" in types:
        return "ACTIVE COMBAT"
    if "AIRSTRIKE" in types:
        return "HEAVY STRIKES"
    if "ROCKET_FIRE" in types and len(events) > 2:
        return "ACTIVE"
    if "CEASEFIRE_UPDATE" in types or (types and types.issubset({"DIPLOMATIC", "HUMANITARIAN", "DEPLOYMENT"})):
        return "CEASEFIRE / TENSE"
    if not events:
        return "QUIET"
    return "LOW"


def theater_badge(status: str) -> str:
    return {
        "ACTIVE COMBAT": "[🔴]",
        "HEAVY STRIKES": "[🟠]",
        "ACTIVE": "[🔴]",
        "SUPPLY DISRUPTED": "[🔴]",
        "SUPPLY INCIDENT": "[🟠]",
        "MARKETS ACTIVE": "[🟠]",
        "POLICY MOVE": "[🟡]",
        "CEASEFIRE / TENSE": "[🟡]",
        "SPORADIC": "[🟡]",
        "QUIET": "[🟢]",
        "LOW": "[⚪]",
    }.get(status, "[⚪]")


def _fmt_count(events) -> dict:
    c = Counter(e["event_type"] for e in events)
    return dict(c)


def generate_sitrep(window_hours: int = 24) -> str:
    conn = get_conn()
    try:
        now_local = datetime.now().astimezone()
        tz = now_local.strftime("%Z") or "LOCAL"
        lines: list[str] = []
        lines.append("═" * 60)
        lines.append(f"WARWATCH SITREP — {now_local.strftime('%Y-%m-%d %H:%M')} {tz}")
        if window_hours >= 24 and window_hours % 24 == 0:
            win_label = f"last {window_hours // 24}d"
        else:
            win_label = f"last {window_hours}h"
        lines.append(f"Window: {win_label}")
        lines.append("═" * 60)
        lines.append("")
        lines.append(f"CEASEFIRE CONTEXT: {CONFIG.get('ceasefire_context', '')}")
        lines.append("")

        for theater in THEATER_ORDER:
            evs = events_since(conn, window_hours, theater=theater)
            status = theater_status(evs, theater=theater)
            badge = theater_badge(status)
            lines.append(f"── {theater} {badge} {status} ".ljust(60, "─"))
            if not evs:
                lines.append("  No events in window.")
                lines.append("")
                continue

            counts = _fmt_count(evs)
            breakdown = " / ".join(f"{k}:{v}" for k, v in sorted(counts.items()))
            lines.append(f"  Events: {len(evs)}  ({breakdown})")

            # categorize
            ground = [e for e in evs if e["event_type"] in ("CLASH", "GROUND_OP")]
            air = [e for e in evs if e["event_type"] == "AIRSTRIKE"]
            rockets = [e for e in evs if e["event_type"] == "ROCKET_FIRE"]
            casualties = [e for e in evs if e["event_type"] == "CASUALTY"]
            diplo = [e for e in evs if e["event_type"] in ("DIPLOMATIC", "CEASEFIRE_UPDATE")]
            hum = [e for e in evs if e["event_type"] == "HUMANITARIAN"]
            supply = [e for e in evs if e["event_type"] == "SUPPLY_DISRUPTION"]
            market = [e for e in evs if e["event_type"] == "MARKET_MOVE"]

            def bullet(label, items, limit=3):
                if not items:
                    return
                lines.append(f"  {label}:")
                # Stable two-stage sort: newest first, then CONFIRMED
                # first. Python sort is stable, so ordering within the
                # same confidence tier stays newest-first.
                items = sorted(items, key=lambda e: e["timestamp"] or "", reverse=True)
                items = sorted(items, key=_conf_rank)
                for e in items[:limit]:
                    ts = _local_hm(e["timestamp"] or "")
                    loc = e["location"] or ""
                    summary = (e["summary"] or "")[:140]
                    tag = ""
                    if e["confidence"] == "UNVERIFIED":
                        tag = " (unverified)"
                    elif e["confidence"] == "CONFIRMED":
                        tag = " ✓ CONFIRMED"
                    lines.append(f"    • {ts} {loc} — {summary}{tag}".rstrip())

            bullet("Ground", ground)
            bullet("Air", air)
            bullet("Rockets/Drones", rockets)
            bullet("Supply disruption", supply)
            bullet("Casualties", casualties)
            bullet("Market / policy", market)
            bullet("Diplomatic", diplo)
            bullet("Humanitarian", hum)

            if theater == "LEBANON":
                unifil = [e for e in evs if "unifil" in (e["summary"] or "").lower() or "peacekeep" in (e["summary"] or "").lower()]
                if unifil:
                    bullet("UNIFIL", unifil, limit=5)
                lines.append("  Ceasefire applicability: DISPUTED (per config).")

            # attribution discrepancies
            attrs = Counter()
            for e in evs:
                try:
                    for s in json.loads(e["sources"] or "[]"):
                        if isinstance(s, dict) and s.get("attribution"):
                            attrs[s["attribution"]] += 1
                except Exception:
                    pass
            if len(attrs) >= 2:
                lines.append(f"  Source perspectives: {', '.join(f'{a}:{n}' for a, n in attrs.most_common())}")

            lines.append("")

        # global tail
        lines.append("─" * 60)
        lines.append("NOTES:")
        lines.append("  • All claims reported with attribution where available.")
        lines.append("  • CONFIRMED = 2+ independent sources. REPORTED = single credible.")
        lines.append("  • This is a monitoring feed — not an intelligence assessment.")
        lines.append("═" * 60)
        return "\n".join(lines)
    finally:
        conn.close()


if __name__ == "__main__":
    print(generate_sitrep())
