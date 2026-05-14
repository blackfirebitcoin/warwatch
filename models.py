"""WARWATCH data models and SQLite helpers."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "db" / "warwatch.db"

EVENT_TYPES = {
    "CLASH", "AIRSTRIKE", "ROCKET_FIRE", "GROUND_OP", "CASUALTY",
    "CEASEFIRE_UPDATE", "DIPLOMATIC", "HUMANITARIAN", "DEPLOYMENT",
    "SUPPLY_DISRUPTION", "MARKET_MOVE",
}
CONFIDENCES = {"CONFIRMED", "REPORTED", "UNVERIFIED"}
THEATERS = {"LEBANON", "IRAN", "GAZA", "SYRIA", "YEMEN", "ENERGY", "OTHER"}


# CHECK constraints were dropped from the events table in favor of
# Python-side validation via EVENT_TYPES / CONFIDENCES / THEATERS. Keeping
# the CHECKs meant every new event_type / theater required a table
# rebuild; validation already happens in Event.ensure_valid() before any
# insert reaches SQLite.
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    first_seen_at TEXT,
    location TEXT,
    lat REAL,
    lon REAL,
    event_type TEXT,
    summary TEXT,
    sources TEXT,
    confidence TEXT,
    theater TEXT,
    raw_data TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scrape_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT,
    timestamp TEXT,
    status TEXT,
    events_found INTEGER
);

CREATE TABLE IF NOT EXISTS alerts_fired (
    event_id TEXT PRIMARY KEY,
    fired_at TEXT NOT NULL,
    event_type TEXT,
    confidence TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_theater ON events(theater);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
-- Compound: Pass 2/3 of find_dup filter on (theater AND timestamp BETWEEN);
-- the planner picks this over either single-column index every time.
CREATE INDEX IF NOT EXISTS idx_events_theater_ts ON events(theater, timestamp);
CREATE INDEX IF NOT EXISTS idx_scrape_log_ts ON scrape_log(timestamp);

CREATE TABLE IF NOT EXISTS briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    session_id TEXT,
    turn_index INTEGER NOT NULL,
    asked_at TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT,
    model TEXT,
    cost_usd REAL,
    duration_ms INTEGER,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_briefs_thread ON briefs(thread_id, turn_index);

CREATE TABLE IF NOT EXISTS context_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_context_kind_ts ON context_snapshots(kind, fetched_at);
"""


@dataclass
class Event:
    timestamp: str  # ISO-8601 UTC
    summary: str
    event_type: str = "DEPLOYMENT"
    theater: str = "OTHER"
    location: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    sources: list = field(default_factory=list)  # list of {name,url,attribution}
    confidence: str = "REPORTED"
    raw_data: Optional[str] = None
    id: Optional[str] = None

    def compute_id(self) -> str:
        key = f"{self.timestamp[:16]}|{(self.location or '').lower()}|{self.event_type}|{self.summary[:80].lower()}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    def ensure_valid(self) -> "Event":
        if self.event_type not in EVENT_TYPES:
            self.event_type = "DEPLOYMENT"
        if self.confidence not in CONFIDENCES:
            self.confidence = "REPORTED"
        if self.theater not in THEATERS:
            self.theater = "OTHER"
        if not self.id:
            self.id = self.compute_id()
        return self


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL + relaxed durability is safe for a derived/recoverable feed:
    # an unclean shutdown can lose the last few seconds of a scrape, which
    # the next 5-minute cycle re-pulls anyway. NORMAL gives ~2-3x faster
    # commits than the default FULL.
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA cache_size=-20000;")  # 20 MB page cache
    conn.execute("PRAGMA mmap_size=67108864;")  # 64 MB mmap window
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Defensive migration for pre-existing DBs that don't have the column.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
        if "first_seen_at" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN first_seen_at TEXT")
        # Drop legacy CHECK constraints if this is a pre-energy-lane DB.
        # SQLite can't ALTER a CHECK constraint, so we rebuild the table
        # in place: rename, recreate without CHECKs, copy rows, drop old.
        # first_seen_at is preserved (see README gotcha on DB rebuild).
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()
        if row and "CHECK(" in (row["sql"] or ""):
            conn.executescript(
                """
                BEGIN;
                ALTER TABLE events RENAME TO events_old;
                CREATE TABLE events (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    first_seen_at TEXT,
                    location TEXT,
                    lat REAL,
                    lon REAL,
                    event_type TEXT,
                    summary TEXT,
                    sources TEXT,
                    confidence TEXT,
                    theater TEXT,
                    raw_data TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                );
                INSERT INTO events
                    (id,timestamp,first_seen_at,location,lat,lon,event_type,
                     summary,sources,confidence,theater,raw_data,
                     created_at,updated_at)
                    SELECT id,timestamp,first_seen_at,location,lat,lon,event_type,
                           summary,sources,confidence,theater,raw_data,
                           created_at,updated_at
                    FROM events_old;
                DROP TABLE events_old;
                CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
                CREATE INDEX IF NOT EXISTS idx_events_theater ON events(theater);
                CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
                CREATE INDEX IF NOT EXISTS idx_events_theater_ts ON events(theater, timestamp);
                COMMIT;
                """
            )


# ---------- fuzzy matching for dedup ----------

def _norm(s: Optional[str]) -> str:
    return (s or "").lower().strip()


def _location_similar(a: Optional[str], b: Optional[str]) -> bool:
    """True when two location strings plausibly refer to the same place.

    NOTE: two unknown (None/empty) locations return False, not True — we
    never want the absence of location data to count as a match. Callers
    that want to merge locationless events must also require summary
    similarity.
    """
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    ta = set(a.replace(",", " ").split())
    tb = set(b.replace(",", " ").split())
    if not ta or not tb:
        return False
    overlap = ta & tb
    return len(overlap) / max(len(ta | tb), 1) >= 0.5 or a in b or b in a


def _parse_ts(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        # handle trailing Z
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


_STOPWORDS = frozenset({
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "as",
    "by", "with", "from", "is", "was", "were", "be", "been", "it", "its",
    "that", "this", "into", "over", "after", "before", "said", "says",
})


@lru_cache(maxsize=8192)
def _summary_tokens(s: Optional[str]) -> frozenset:
    """Content tokens for fuzzy-dedup overlap math.

    Memoized: in a typical ingest cycle the same DB row is tokenized
    once for every incoming event whose time-window overlaps it
    (~38× per row in measurement). Caching collapses that to one
    tokenization per unique summary.
    """
    if not s:
        return frozenset()
    words = []
    for w in (s.lower().replace("'", " ").replace("-", " ").split()):
        w = "".join(ch for ch in w if ch.isalnum())
        if len(w) >= 4 and w not in _STOPWORDS:
            words.append(w)
    return frozenset(words)


@lru_cache(maxsize=8192)
def _summary_bigrams(s: Optional[str]) -> frozenset:
    """Adjacent bigrams over content tokens.

    Char floor lowered to 3 (vs 4 for `_summary_tokens`) so 3-letter
    proper-noun anchors that drive news headlines — UAE, OIL, IDF, IDF,
    UAV, EU — survive into the bigram. The token-Jaccard pass keeps
    the 4-char floor because there it would just inflate noise.
    """
    if not s:
        return set()
    words = []
    for w in (s.lower().replace("'", " ").replace("-", " ").split()):
        w = "".join(ch for ch in w if ch.isalnum())
        if len(w) >= 3 and w not in _STOPWORDS:
            words.append(w)
    return frozenset((words[i], words[i + 1]) for i in range(len(words) - 1))


def _summary_similar(a: Optional[str], b: Optional[str], threshold: float = 0.35) -> bool:
    ta, tb = _summary_tokens(a), _summary_tokens(b)
    if not ta or not tb:
        return False
    overlap = ta & tb
    return len(overlap) / max(len(ta | tb), 1) >= threshold


# Priority order when reconciling event_type across sources reporting the
# same incident: the most operationally significant classification wins.
TYPE_PRIORITY = {
    "GROUND_OP": 90,
    "CLASH": 85,
    "SUPPLY_DISRUPTION": 77,  # physical disruption of hydrocarbon flow
    "AIRSTRIKE": 75,
    "ROCKET_FIRE": 70,
    "CASUALTY": 60,  # a casualty report loses to the precipitating cause
    "CEASEFIRE_UPDATE": 55,
    "MARKET_MOVE": 50,  # policy event, narrower consequence than ceasefire
    "HUMANITARIAN": 40,
    "DIPLOMATIC": 35,
    "DEPLOYMENT": 20,
}

# Types that commonly describe the same incident from different angles.
# When both types fall inside the same cluster AND location+time match,
# we skip the summary-similarity gate since the type signal already tells
# us these are different facets of the same event.
_KINETIC_CLUSTER = frozenset({"CLASH", "GROUND_OP", "AIRSTRIKE", "ROCKET_FIRE", "CASUALTY"})
_DIPLO_CLUSTER = frozenset({"CEASEFIRE_UPDATE", "DIPLOMATIC"})
# Commodity cluster — a tanker strike in Hormuz reported by Reuters
# Energy and a "Houthis board VLCC in Red Sea" headline from Lloyd's
# List are two framings of the same incident. Market policy events
# (MARKET_MOVE) sit in the cluster because an OPEC+ cut can be reported
# by several outlets with different emphasis but same underlying act.
_COMMODITY_CLUSTER = frozenset({"SUPPLY_DISRUPTION", "MARKET_MOVE"})


def _same_cluster(a: str, b: str) -> bool:
    return (
        (a in _KINETIC_CLUSTER and b in _KINETIC_CLUSTER)
        or (a in _DIPLO_CLUSTER and b in _DIPLO_CLUSTER)
        or (a in _COMMODITY_CLUSTER and b in _COMMODITY_CLUSTER)
    )


def reconcile_type(existing: str, incoming: str) -> str:
    """Pick the higher-priority classification when merging."""
    if TYPE_PRIORITY.get(incoming, 0) > TYPE_PRIORITY.get(existing, 0):
        return incoming
    return existing


def find_dup(
    conn: sqlite3.Connection,
    ev: Event,
    tight_window_min: int = 15,
    wide_window_min: int = 240,
    cluster_window_min: int = 720,
) -> Optional[sqlite3.Row]:
    """Find an existing event that matches within a fuzzy window.

    Three-pass match:
      1. Tight (±15min): same event_type + similar location, OR
                         similar location + any summary overlap.
         Handles near-simultaneous reports with matching metadata.
      2. Wide (±4h, same theater only): strong summary overlap (≥50% tokens)
         + similar location. Handles different sources whose RSS publish
         times drift hours apart but describe the same incident.
      3. Cluster (±12h, same theater + type-cluster): strong summary
         overlap (≥60% tokens), location NOT required. Motivated by the
         fact that >70% of scraped events carry no parseable location —
         passes 1+2 cannot merge them at all, so same-incident reports
         from different outlets were living as duplicates and never
         earning CONFIRMED status.

    Conservative knobs: cluster pass still requires same theater, a
    same-or-cluster type match, and a tighter similarity bar (0.60) than
    Pass 2's locationless threshold.
    """
    ev_dt = _parse_ts(ev.timestamp)
    if ev_dt is None:
        return conn.execute(f"SELECT {_DUP_COLS} FROM events WHERE id=?", (ev.id,)).fetchone()

    ev_loc_known = bool(ev.location)

    def loc_ok(row_loc: Optional[str]) -> tuple[bool, bool]:
        """Return (match, strict) where strict=True when both sides had real locations."""
        if _location_similar(row_loc, ev.location):
            return True, True
        # Both unknown → allow, but caller must require stronger summary match.
        if not row_loc and not ev_loc_known:
            return True, False
        return False, False

    # Pass 1: tight window
    low = (ev_dt - timedelta(minutes=tight_window_min)).isoformat()
    high = (ev_dt + timedelta(minutes=tight_window_min)).isoformat()
    rows = conn.execute(
        f"SELECT {_DUP_COLS} FROM events WHERE timestamp BETWEEN ? AND ?",
        (low, high),
    ).fetchall()
    for r in rows:
        match, strict = loc_ok(r["location"])
        if not match:
            continue
        if strict and r["event_type"] == ev.event_type:
            return r
        # Same kinetic/diplomatic cluster + strict location + tight time is
        # sufficient evidence — skip summary gate.
        if strict and _same_cluster(r["event_type"], ev.event_type):
            return r
        # Otherwise require summary overlap.
        threshold = 0.30 if strict else 0.55
        if _summary_similar(r["summary"], ev.summary, threshold=threshold):
            return r

    # Pass 2: wide window, same theater, strong text overlap
    low2 = (ev_dt - timedelta(minutes=wide_window_min)).isoformat()
    high2 = (ev_dt + timedelta(minutes=wide_window_min)).isoformat()
    rows2 = conn.execute(
        f"SELECT {_DUP_COLS} FROM events WHERE theater=? AND timestamp BETWEEN ? AND ?",
        (ev.theater, low2, high2),
    ).fetchall()
    for r in rows2:
        match, strict = loc_ok(r["location"])
        if not match:
            continue
        # Same type OR same cluster, strict location, same theater, ≤4h
        # → same incident. Example: LiveUAMap reports an AIRSTRIKE and
        # Reuters publishes the CASUALTY toll 2h later from the same town.
        if strict and (r["event_type"] == ev.event_type or _same_cluster(r["event_type"], ev.event_type)):
            return r
        threshold = 0.50 if strict else 0.65
        if _summary_similar(r["summary"], ev.summary, threshold=threshold):
            return r

    # Pass 3 + Pass 4: cluster merge — same theater + type cluster, wide
    # time, location optional. These are the passes that catch the
    # majority of real cross-source duplicates, because most RSS
    # headlines never mention a town name.
    #
    # Pass 3 (token Jaccard) — per-theater knobs. Conflict lane uses the
    # validated 0.30/4 default. ENERGY uses 0.28/4: lower than conflict
    # because energy paraphrases vary more in vocabulary (analyst
    # attribution, deal-driven verbs), but not as low as the original
    # 0.22/3 — that admitted false positives from unrelated stories
    # sharing 3 generic OPEC/sanctions tokens. Real paraphrase pairs
    # below 0.28 still get rescued by the bigram pass below.
    #
    # Pass 4 (bigram fallback) — ≥ 2 shared adjacent bigrams over
    # content tokens. Phrase-level constraint defends against the
    # false positives a uniformly-lower Jaccard would produce: two
    # unrelated Lebanon airstrike headlines share generic tokens
    # {israeli, lebanon, southern, strike} but rarely share the same
    # 2-word phrases. Catches paraphrase pairs like "UAE to leave
    # OPEC" / "UAE Quits OPEC" that token-Jaccard misses because of
    # vocabulary substitution.
    if ev.theater and ev.theater != "OTHER":
        ev_tokens = _summary_tokens(ev.summary)
        ev_bigrams = _summary_bigrams(ev.summary)
        low3 = (ev_dt - timedelta(minutes=cluster_window_min)).isoformat()
        high3 = (ev_dt + timedelta(minutes=cluster_window_min)).isoformat()
        rows3 = conn.execute(
            f"SELECT {_DUP_COLS} FROM events WHERE theater=? AND timestamp BETWEEN ? AND ?",
            (ev.theater, low3, high3),
        ).fetchall()

        if ev.theater == "ENERGY":
            j_min, s_min = 0.28, 4
        else:
            j_min, s_min = 0.30, 4

        for r in rows3:
            if not (r["event_type"] == ev.event_type or _same_cluster(r["event_type"], ev.event_type)):
                continue
            # If BOTH sides have locations, they must be plausibly the
            # same. If either is missing, proceed to the content gates.
            if r["location"] and ev_loc_known and not _location_similar(r["location"], ev.location):
                continue
            r_tokens = _summary_tokens(r["summary"])
            if ev_tokens and r_tokens:
                shared = ev_tokens & r_tokens
                if len(shared) >= s_min and len(shared) / max(len(ev_tokens | r_tokens), 1) >= j_min:
                    return r
            r_bigrams = _summary_bigrams(r["summary"])
            if ev_bigrams and r_bigrams and len(ev_bigrams & r_bigrams) >= 2:
                return r

    return conn.execute(f"SELECT {_DUP_COLS} FROM events WHERE id=?", (ev.id,)).fetchone()


def upsert_event(conn: sqlite3.Connection, ev: Event) -> str:
    """Insert or merge. Returns 'new'|'merged'|'skipped'."""
    ev.ensure_valid()
    existing = find_dup(conn, ev)
    if existing is None:
        now_iso = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            """INSERT OR IGNORE INTO events
               (id,timestamp,first_seen_at,location,lat,lon,event_type,summary,sources,confidence,theater,raw_data,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
            (
                ev.id, ev.timestamp, now_iso, ev.location, ev.lat, ev.lon, ev.event_type,
                ev.summary, json.dumps(ev.sources), ev.confidence, ev.theater,
                ev.raw_data,
            ),
        )
        if cur.rowcount == 0:
            # Same id already in DB but fuzzy find_dup missed it. Do NOT
            # REPLACE — that would clobber first_seen_at and break feed
            # ordering (see README gotcha).
            return "skipped"
        return "new"

    # merge sources
    try:
        existing_sources = json.loads(existing["sources"] or "[]")
    except Exception:
        existing_sources = []
    merged = list(existing_sources)
    seen_names = {s.get("name") for s in merged if isinstance(s, dict)}
    added = False
    for s in ev.sources:
        if isinstance(s, dict) and s.get("name") not in seen_names:
            merged.append(s)
            seen_names.add(s.get("name"))
            added = True

    # confidence upgrade
    distinct_credible = len({s.get("name") for s in merged if isinstance(s, dict)})
    new_conf = existing["confidence"]
    if distinct_credible >= 2:
        new_conf = "CONFIRMED"
    elif new_conf == "UNVERIFIED" and ev.confidence in ("REPORTED", "CONFIRMED"):
        new_conf = ev.confidence

    # event_type reconciliation: take the higher-priority classification
    new_type = reconcile_type(existing["event_type"], ev.event_type)

    # discrepancy note if attribution differs
    summary = existing["summary"] or ""
    attrs = {s.get("attribution") for s in merged if isinstance(s, dict) and s.get("attribution")}
    if len(attrs) > 1 and "[multi-source]" not in summary:
        summary += f"  [multi-source: {', '.join(sorted(a for a in attrs if a))}]"

    type_changed = new_type != existing["event_type"]
    if added or new_conf != existing["confidence"] or summary != (existing["summary"] or "") or type_changed:
        conn.execute(
            """UPDATE events SET sources=?, confidence=?, summary=?, event_type=?, updated_at=datetime('now') WHERE id=?""",
            (json.dumps(merged), new_conf, summary, new_type, existing["id"]),
        )
        return "merged"
    return "skipped"


_MULTI_SOURCE_SUFFIX_RE = __import__("re").compile(r"\s*\[multi-source:[^\]]*\]\s*$")


def rebuild_confidences(
    conn: sqlite3.Connection,
    *,
    reclassify=None,
) -> dict:
    """Walk events in place and recompute derived fields from stored sources.

    For every row we re-derive `confidence` from the distinct-source count,
    strip and re-emit the `[multi-source: …]` summary suffix based on the
    current attribution set, and optionally re-run classification on the
    summary via the caller-supplied `reclassify` function (typically
    `scraper.classify`). Updates are UPDATE-only — we never DELETE + INSERT,
    which would reset `first_seen_at` and collapse `recent_events` ordering.
    See README Gotchas.

    Returns {'scanned': N, 'updated': M, 'reclassified': K}.
    """
    scanned = updated = reclassified = 0
    rows = conn.execute("SELECT * FROM events").fetchall()
    for r in rows:
        scanned += 1
        try:
            sources = json.loads(r["sources"] or "[]")
        except Exception:
            sources = []
        names = {s.get("name") for s in sources if isinstance(s, dict) and s.get("name")}
        attrs = {s.get("attribution") for s in sources if isinstance(s, dict) and s.get("attribution")}

        new_conf = r["confidence"] or "REPORTED"
        if len(names) >= 2:
            new_conf = "CONFIRMED"
        elif new_conf == "CONFIRMED" and len(names) < 2:
            # Source list shrank (or was mis-merged); drop back to REPORTED.
            new_conf = "REPORTED"

        base_summary = _MULTI_SOURCE_SUFFIX_RE.sub("", r["summary"] or "").rstrip()
        if len(attrs) > 1:
            tail = f"  [multi-source: {', '.join(sorted(a for a in attrs if a))}]"
            new_summary = base_summary + tail
        else:
            new_summary = base_summary

        new_type = r["event_type"]
        if reclassify is not None:
            try:
                candidate = reclassify(new_summary)
            except Exception:
                candidate = new_type
            # Prefer the higher-priority classification; never downgrade.
            if candidate and candidate != new_type:
                chosen = reconcile_type(new_type, candidate)
                if chosen != new_type:
                    new_type = chosen
                    reclassified += 1

        if (
            new_conf != r["confidence"]
            or new_summary != (r["summary"] or "")
            or new_type != r["event_type"]
        ):
            conn.execute(
                "UPDATE events SET confidence=?, summary=?, event_type=?, updated_at=datetime('now') WHERE id=?",
                (new_conf, new_summary, new_type, r["id"]),
            )
            updated += 1
    conn.commit()
    return {"scanned": scanned, "updated": updated, "reclassified": reclassified}


def prune_events(conn: sqlite3.Connection, retention_days: int) -> int:
    """Delete events whose `timestamp` is older than `retention_days` days.

    Returns the number of rows removed. Set `retention_days` to 0 or a
    negative number to disable (no-op). Pruning is by event time, not
    `first_seen_at` — we're expiring data by age of what happened, not by
    when we learned it.
    """
    if retention_days is None or retention_days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(retention_days))).isoformat()
    cur = conn.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,))
    conn.commit()
    return cur.rowcount or 0


def log_scrape(conn: sqlite3.Connection, source: str, status: str, events_found: int) -> None:
    conn.execute(
        "INSERT INTO scrape_log (source,timestamp,status,events_found) VALUES (?,?,?,?)",
        (source, datetime.now(timezone.utc).isoformat(), status, events_found),
    )


# Columns needed for feed display, detail view, filters, and sitrep.
# Excludes raw_data (always NULL), created_at, updated_at — never read
# in the display path. SELECT * on 400 rows costs ~66ms; this is ~5ms.
_FEED_COLS = (
    "id, timestamp, first_seen_at, location, lat, lon, event_type, "
    "summary, sources, confidence, theater"
)

# Columns find_dup() actually reads off candidate rows. Drops raw_data
# (always NULL), lat/lon, first_seen_at, timestamp, created_at, updated_at
# — none are read in the dedup or merge path. SELECT * here was paying
# row-hydration cost on every candidate in the time window.
_DUP_COLS = "id, location, event_type, summary, sources, confidence, theater"


def recent_events(conn: sqlite3.Connection, limit: int = 200, theater: Optional[str] = None):
    """Fetch events in event-time order (the [HH:MM] shown on each row).

    Tiebreak by first_seen_at so items sharing a minute stay stable.
    """
    order = "timestamp DESC, first_seen_at DESC"
    if theater and theater != "ALL":
        rows = conn.execute(
            f"SELECT {_FEED_COLS} FROM events WHERE theater=? ORDER BY {order} LIMIT ?",
            (theater, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_FEED_COLS} FROM events ORDER BY {order} LIMIT ?", (limit,),
        ).fetchall()
    return rows


def events_since(conn: sqlite3.Connection, hours: int, theater: Optional[str] = None):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    if theater:
        return conn.execute(
            f"SELECT {_FEED_COLS} FROM events WHERE timestamp>=? AND theater=? ORDER BY timestamp DESC",
            (cutoff, theater),
        ).fetchall()
    return conn.execute(
        f"SELECT {_FEED_COLS} FROM events WHERE timestamp>=? ORDER BY timestamp DESC", (cutoff,),
    ).fetchall()


def related_events(conn: sqlite3.Connection, ev_row) -> list:
    """Same location ± 1hr OR same theater ± 30min."""
    base_dt = _parse_ts(ev_row["timestamp"])
    if base_dt is None:
        return []
    loc_low = (base_dt - timedelta(hours=1)).isoformat()
    loc_high = (base_dt + timedelta(hours=1)).isoformat()
    th_low = (base_dt - timedelta(minutes=30)).isoformat()
    th_high = (base_dt + timedelta(minutes=30)).isoformat()
    rows = conn.execute(
        """SELECT * FROM events
           WHERE id<>? AND (
             (location IS NOT NULL AND location=? AND timestamp BETWEEN ? AND ?)
             OR (theater=? AND timestamp BETWEEN ? AND ?)
           )
           ORDER BY timestamp DESC LIMIT 20""",
        (ev_row["id"], ev_row["location"], loc_low, loc_high,
         ev_row["theater"], th_low, th_high),
    ).fetchall()
    return rows


def source_health(conn: sqlite3.Connection, window_hours: int = 24) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    rows = conn.execute(
        """SELECT source, status, MAX(timestamp) last_ts, SUM(events_found) total
           FROM scrape_log WHERE timestamp>=? GROUP BY source, status""",
        (cutoff,),
    ).fetchall()
    h: dict = {}
    for r in rows:
        d = h.setdefault(r["source"], {"ok": 0, "error": 0, "last": None, "events": 0})
        if r["status"] == "ok":
            d["ok"] = 1
        else:
            d["error"] = 1
        d["events"] += r["total"] or 0
        if not d["last"] or (r["last_ts"] and r["last_ts"] > d["last"]):
            d["last"] = r["last_ts"]
    return h


def source_health_rolling(
    conn: sqlite3.Connection, window_hours: int = 168
) -> dict:
    """Per-source rolling success rate over `window_hours`.

    Returns {source: {ok: int, error: int, total: int, rate: float,
    events: int, last: iso_ts}}. 168h default = 7d. Used by the source
    health modal to spot sources that are intermittently failing — a
    flapping Reuters endpoint shows up as a mid-rate source rather
    than a clean green/red split."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    rows = conn.execute(
        """SELECT source, status,
                  COUNT(*) AS n,
                  SUM(events_found) AS events,
                  MAX(timestamp) AS last_ts
           FROM scrape_log WHERE timestamp>=?
           GROUP BY source, status""",
        (cutoff,),
    ).fetchall()
    h: dict = {}
    for r in rows:
        d = h.setdefault(
            r["source"],
            {"ok": 0, "error": 0, "total": 0, "rate": 0.0, "events": 0, "last": None},
        )
        if r["status"] == "ok":
            d["ok"] += r["n"] or 0
        else:
            d["error"] += r["n"] or 0
        d["events"] += r["events"] or 0
        if not d["last"] or (r["last_ts"] and r["last_ts"] > d["last"]):
            d["last"] = r["last_ts"]
    for d in h.values():
        d["total"] = d["ok"] + d["error"]
        d["rate"] = (d["ok"] / d["total"]) if d["total"] else 0.0
    return h


def insert_brief_turn(
    conn: sqlite3.Connection,
    thread_id: str,
    turn_index: int,
    asked_at: str,
    question: str,
) -> int:
    """Insert a pending brief turn. Returns the new row id."""
    cur = conn.execute(
        """INSERT INTO briefs (thread_id, turn_index, asked_at, question)
           VALUES (?, ?, ?, ?)""",
        (thread_id, turn_index, asked_at, question),
    )
    conn.commit()
    return int(cur.lastrowid)


def complete_brief_turn(
    conn: sqlite3.Connection,
    row_id: int,
    *,
    session_id: Optional[str],
    answer: Optional[str],
    model: Optional[str],
    cost_usd: Optional[float],
    duration_ms: Optional[int],
    error: Optional[str] = None,
) -> None:
    """Fill in the response fields on an existing brief row."""
    conn.execute(
        """UPDATE briefs
           SET session_id=?, answer=?, model=?, cost_usd=?, duration_ms=?, error=?
           WHERE id=?""",
        (session_id, answer, model, cost_usd, duration_ms, error, row_id),
    )
    conn.commit()


def get_thread_turns(conn: sqlite3.Connection, thread_id: str) -> list:
    return conn.execute(
        "SELECT * FROM briefs WHERE thread_id=? ORDER BY turn_index ASC",
        (thread_id,),
    ).fetchall()


def insert_context_snapshot(
    conn: sqlite3.Connection, kind: str, payload: dict
) -> int:
    """Store a reference-context snapshot (market state, etc.).

    These are injected into brief prompts alongside the SITREP but never
    surface in the main feed. Kept as raw JSON so the schema doesn't
    need to change when the snapshot shape evolves.
    """
    fetched_at = payload.get("fetched_at") or datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO context_snapshots (kind, fetched_at, payload) VALUES (?, ?, ?)",
        (kind, fetched_at, json.dumps(payload)),
    )
    conn.commit()
    return int(cur.lastrowid)


def latest_context(conn: sqlite3.Connection, kind: str) -> Optional[dict]:
    """Return the most recent snapshot of `kind` as a dict, or None."""
    row = conn.execute(
        "SELECT payload, fetched_at FROM context_snapshots "
        "WHERE kind=? ORDER BY fetched_at DESC LIMIT 1",
        (kind,),
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["payload"])
    except Exception:
        return None


def prune_context_snapshots(
    conn: sqlite3.Connection, keep_per_kind: int = 48
) -> int:
    """Keep the N most recent snapshots per kind. Returns rows deleted.

    At 30-min refresh, 48 snapshots = 24h of history — enough to debug a
    bad fetch, not enough to balloon the DB.
    """
    if keep_per_kind <= 0:
        return 0
    cur = conn.execute(
        """DELETE FROM context_snapshots
           WHERE id NOT IN (
             SELECT id FROM (
               SELECT id, ROW_NUMBER() OVER (
                 PARTITION BY kind ORDER BY fetched_at DESC
               ) rn FROM context_snapshots
             ) WHERE rn <= ?
           )""",
        (keep_per_kind,),
    )
    conn.commit()
    return cur.rowcount or 0


def list_brief_threads(conn: sqlite3.Connection, limit: int = 20) -> list:
    """Thread summaries for the history picker — most recent first.

    `total_cost_usd` coalesces NULLs to 0 so the picker can render a cost
    column without branching; rows with failed turns just count as $0
    for that turn."""
    return conn.execute(
        """SELECT thread_id,
                  MIN(asked_at) AS first_asked_at,
                  MAX(asked_at) AS last_asked_at,
                  COUNT(*) AS turn_count,
                  COALESCE(SUM(cost_usd), 0.0) AS total_cost_usd,
                  (SELECT question FROM briefs b2
                     WHERE b2.thread_id = b1.thread_id
                     ORDER BY turn_index ASC LIMIT 1) AS first_question
           FROM briefs b1
           GROUP BY thread_id
           ORDER BY last_asked_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()


def pending_alerts(
    conn: sqlite3.Connection,
    event_types: list,
    min_confidence: str = "CONFIRMED",
    limit: int = 20,
) -> list:
    """Events matching the alert criteria that haven't been alerted on yet.

    "Alerted" = there's a row in alerts_fired keyed by event id. The caller
    is responsible for inserting into alerts_fired after the notification
    actually fires — so a failed termux-notification can be retried on the
    next scrape cycle, but a successful fire is never repeated.
    """
    if not event_types:
        return []
    # Order: CONFIRMED > REPORTED > UNVERIFIED. A min_confidence gate
    # lets us accept CONFIRMED-or-above without hardcoding the ladder.
    rank = {"UNVERIFIED": 0, "REPORTED": 1, "CONFIRMED": 2}
    min_rank = rank.get(min_confidence, 2)
    allowed = [c for c, r in rank.items() if r >= min_rank]
    if not allowed:
        return []
    qs = ",".join("?" * len(event_types))
    cs = ",".join("?" * len(allowed))
    return conn.execute(
        f"""SELECT e.id, e.timestamp, e.event_type, e.confidence,
                   e.theater, e.location, e.summary
            FROM events e
            LEFT JOIN alerts_fired a ON a.event_id = e.id
            WHERE a.event_id IS NULL
              AND e.event_type IN ({qs})
              AND e.confidence IN ({cs})
            ORDER BY e.timestamp DESC
            LIMIT ?""",
        (*event_types, *allowed, limit),
    ).fetchall()


def mark_alerted(
    conn: sqlite3.Connection,
    event_id: str,
    event_type: str,
    confidence: str,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO alerts_fired
           (event_id, fired_at, event_type, confidence)
           VALUES (?, ?, ?, ?)""",
        (event_id, datetime.now(timezone.utc).isoformat(), event_type, confidence),
    )
    conn.commit()


def stats_today(conn: sqlite3.Connection) -> dict:
    since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    today_count = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE timestamp>=?", (since,),
    ).fetchone()["c"]
    last = conn.execute(
        "SELECT timestamp FROM events ORDER BY timestamp DESC LIMIT 1",
    ).fetchone()
    return {"today": today_count, "last": last["timestamp"] if last else None}
