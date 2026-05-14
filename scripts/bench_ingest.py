"""Benchmark the WarWatch ingest path end-to-end.

Synthesizes N realistic-shaped events across the 6 theaters, feeds them
through the real upsert_event() pipeline (find_dup + classify + merge),
and reports per-event cost in cold-DB and warm-DB conditions.

Usage from the repo root:
    python scripts/bench_ingest.py                 # default 2058 events
    python scripts/bench_ingest.py --events 5000   # bigger corpus
    python scripts/bench_ingest.py --warm-runs 3   # repeat the warm pass

The default 2058 corpus mirrors the README's "~2,058 raw items → ~1,544
events on a recent run" workload so before/after numbers are comparable
across changes.
"""
from __future__ import annotations

import argparse
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running from scripts/ or repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB = Path("/tmp/warwatch_bench.sqlite3")

import models  # noqa: E402

models.DB_PATH = DB

THEATERS = ["LEBANON", "IRAN", "GAZA", "SYRIA", "YEMEN", "ENERGY"]
TYPES = [
    "AIRSTRIKE", "ROCKET_FIRE", "CASUALTY", "DIPLOMATIC", "MARKET_MOVE",
    "DEPLOYMENT", "CLASH", "GROUND_OP", "HUMANITARIAN",
]
LOCS = [
    "beirut", "tyre", "tehran", "isfahan", "gaza", "rafah", "damascus",
    "homs", "sanaa", "hodeidah", "ras tanura", "jebel ali",
    None, None, None,  # ~20% locationless to exercise Pass 3
]
VOCAB = (
    "israeli army strikes target in southern lebanon overnight casualties "
    "reported civilians killed in airstrike on building near border idf "
    "warplanes hit hezbollah positions iran says response coming opec "
    "agrees to production cut tanker attacked in red sea oil prices "
    "rise on supply concerns saudi aramco reports drop in output "
    "ceasefire violation reported clashes break out humanitarian convoy "
    "evacuated hospital damaged"
).split()


def _make_event(i: int, base_dt: datetime) -> "models.Event":
    summary = " ".join(random.sample(VOCAB, k=random.randint(8, 18)))
    ts = base_dt + timedelta(minutes=random.randint(-180, 180))
    return models.Event(
        timestamp=ts.isoformat(),
        summary=summary,
        event_type=random.choice(TYPES),
        theater=random.choice(THEATERS),
        location=random.choice(LOCS),
        sources=[{"name": f"src_{i % 15}", "url": f"https://x/{i}"}],
        confidence="REPORTED",
    )


def _run_batch(n: int, seed: int) -> tuple[float, int, int]:
    random.seed(seed)
    models.init_db()
    base = datetime.now(timezone.utc)
    events = [_make_event(i, base) for i in range(n)]

    conn = models.get_conn()
    new_n = merged_n = 0
    t0 = time.perf_counter()
    for ev in events:
        r = models.upsert_event(conn, ev)
        if r == "new":
            new_n += 1
        elif r == "merged":
            merged_n += 1
    conn.commit()
    dt = time.perf_counter() - t0
    conn.close()
    return dt, new_n, merged_n


def _query_recent(loops: int = 10) -> float:
    conn = models.get_conn()
    t0 = time.perf_counter()
    for _ in range(loops):
        list(models.recent_events(conn, limit=400))
    dt = time.perf_counter() - t0
    conn.close()
    return dt / loops * 1000


def _show_pragmas() -> None:
    """Open via models.get_conn() — a bare sqlite3.connect() would skip
    the PRAGMA tuning we want to verify and print the defaults, which
    is misleading."""
    conn = models.get_conn()
    print("\n=== sqlite pragmas ===")
    for p in ("journal_mode", "synchronous", "cache_size", "temp_store", "mmap_size"):
        v = conn.execute(f"PRAGMA {p}").fetchone()
        print(f"  {p:14s} = {v[0]}")
    conn.close()


def _show_indexes() -> None:
    conn = sqlite3.connect(DB)
    print("\n=== events indexes ===")
    for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events' ORDER BY name"
    ):
        print(f"  {r[0]}")
    conn.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--events", type=int, default=2058,
                   help="events per batch (default: 2058 — matches README workload)")
    p.add_argument("--warm-runs", type=int, default=1,
                   help="extra warm-DB ingest passes after the cold one")
    p.add_argument("--seed", type=int, default=11)
    args = p.parse_args()

    if DB.exists():
        DB.unlink()
    for suffix in ("-shm", "-wal"):
        side = DB.with_name(DB.name + suffix)
        if side.exists():
            side.unlink()

    print(f"=== cold-DB ingest of {args.events} events ===")
    dt, new_n, merged_n = _run_batch(args.events, seed=args.seed)
    print(f"total: {dt*1000:7.0f} ms  ({dt/args.events*1000:.2f} ms/event)  "
          f"new={new_n} merged={merged_n}")

    for i in range(args.warm_runs):
        print(f"\n=== warm-DB ingest #{i+1} of {args.events} events ===")
        dt, new_n, merged_n = _run_batch(args.events, seed=args.seed + 1 + i)
        print(f"total: {dt*1000:7.0f} ms  ({dt/args.events*1000:.2f} ms/event)  "
              f"new={new_n} merged={merged_n}")

    print(f"\n=== recent_events(limit=400), avg of 10 calls ===")
    print(f"avg: {_query_recent():.2f} ms/call")

    _show_pragmas()
    _show_indexes()


if __name__ == "__main__":
    main()
