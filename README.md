# WarWatch

> Terminal dashboard for tracking geopolitical and energy-market signals across
> OSINT, news, and shipping sources. Ingests, deduplicates, classifies, and
> synthesizes events into SITREP-style briefings via an LLM layer.

<p align="center">
  <a href="docs/dashboard.mp4?raw=true">
    <img src="docs/dashboard.gif" alt="WarWatch live demo — feed, theater filter, SITREP" width="900">
  </a>
</p>
<p align="center">
  <sub>↑ Click the GIF to download the 745 KB MP4 (HTML5-video controls, sharper).</sub>
</p>

<p align="center">
  <img alt="Python 3.10+"      src="https://img.shields.io/badge/python-3.10+-blue">
  <img alt="License: MIT"      src="https://img.shields.io/badge/license-MIT-green">
  <img alt="Tests: 79 passing" src="https://img.shields.io/badge/tests-79%20passing-brightgreen">
  <img alt="Status: prototype" src="https://img.shields.io/badge/status-prototype-orange">
</p>

## What it does

- Pulls from **15 configured sources** (OSINT, news, shipping)
- **Multi-pass fuzzy deduplication** — tight time/location, wide time/theater,
  cluster-based token + bigram match (recent run: ~2,058 raw items → ~1,544
  events, ~25% reduction)
- Classifies events into **6 theaters** (Lebanon, Iran, Gaza, Syria, Yemen,
  Energy) and **11 event types**
- Stores in WAL-mode SQLite with compound indexes, FTS5 full-text
  search (prefix support: `hez*`), and full multi-source confirmation
  tracking
- Self-maintaining: per-startup data hygiene prunes stale scrape logs
  + alert dedup marks, backfills legacy columns, runs `VACUUM`/`ANALYZE`,
  and surfaces a one-line audit (events total, missing fields,
  multi-source unconfirmed)
- Generates **SITREP-style briefings** on demand using an LLM synthesis layer
  over the live event database
- Optional Android push alerts via `termux-notification` for high-severity
  CONFIRMED events

## Why I built it

Mainstream news reporting is a failed model — I needed a ground-truth
heartbeat. WarWatch cut through the fog of war and supported profitable
Brent crude trades based on its analysis in real-time scenarios.

## Stack

Python 3.10+ · SQLite (WAL + compound indexes) · `httpx` · `feedparser` ·
`beautifulsoup4` · `lxml` ·
[`textual`](https://github.com/Textualize/textual) · pytest · provider-agnostic
LLM synthesis (Claude Code CLI by default)

## Quick start

```bash
git clone https://github.com/blackfirebitcoin/warwatch
cd warwatch

# Recommended: isolate deps (Python 3.10+ required)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Brief / SITREP needs an LLM. Either install the Claude Code CLI
# (https://docs.anthropic.com/en/docs/claude-code) — no key needed if you
# have a subscription — or drop a key in .env for the standalone API:
cp .env.example .env  # then fill in ANTHROPIC_API_KEY=...

python app.py
```

The TUI launches into the live feed. Background scrapes run every
`auto_scrape_minutes` minutes (default 5).

## Hotkeys

| Key       | Action                          |
|-----------|---------------------------------|
| `r`       | Refresh / scrape now            |
| `t`       | Theater filter                  |
| `c`       | Cycle confidence filter         |
| `f`       | Cycle event-type filter         |
| `/`       | Substring search                |
| `x`       | Clear filters                   |
| `d` / ⏎   | Open detail view                |
| `s`       | SITREP for current theater      |
| `b`       | LLM brief modal                 |
| `B`       | Brief archive                   |
| `h`       | Source health                   |
| `e`       | Export current view             |
| `g`       | GeoJSON export                  |
| `q`       | Quit                            |

## Demo

The demo above is recorded with [Charm `vhs`](https://github.com/charmbracelet/vhs)
from a scripted tape file. Re-record after a UI change with:

```bash
brew install vhs ffmpeg gifsicle    # one-time
brew install --cask font-jetbrains-mono   # tape requests JetBrains Mono

vhs docs/demo.tape                  # writes docs/dashboard.gif (~1.4 MB raw)

# Lossless palette + lossy=80 dithering, drops ~32% (1.4 MB → ~950 KB)
gifsicle -O3 --lossy=80 -k 256 docs/dashboard.gif -o docs/dashboard.gif

# Optional: produce an MP4 with HTML5-video controls (smaller + crisper).
ffmpeg -y -i docs/dashboard.gif -movflags +faststart -pix_fmt yuv420p \
  -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" -crf 22 -preset slow \
  docs/dashboard.mp4
```

The tape file lives at [`docs/demo.tape`](docs/demo.tape) so the demo stays
reproducible.

## Tests

```bash
pytest
```

79 tests cover classifier behavior, three-pass deduplication, filter
composition, brief threading, FTS5 search/prefix semantics, retention, audit,
and data-backfill correctness.

## Benchmarking ingest

A reproducible micro-benchmark lives in `scripts/bench_ingest.py` so perf
changes can be measured before/after instead of guessed:

```bash
python scripts/bench_ingest.py                  # default 2058 events
python scripts/bench_ingest.py --warm-runs 3    # repeat the warm pass
python scripts/bench_ingest.py --events 5000    # bigger corpus
```

Reference numbers (M3 MacBook Pro, after the recent perf pass):

| Phase                       | Wall time | Per event |
|-----------------------------|----------:|----------:|
| Cold ingest of 2,058 events |   ~250 ms |   0.12 ms |
| Warm ingest of 2,058 events |   ~400 ms |   0.19 ms |
| `recent_events(limit=400)`  |   ~0.5 ms |     —     |

## Configuration cheat sheet

| key                       | default | what it does                                 |
| ------------------------- | ------- | -------------------------------------------- |
| `auto_scrape_minutes`     | 5       | Background ingest interval (0 disables)      |
| `context_refresh_minutes` | 30      | Market-snapshot refresh interval             |
| `ingest_max_age_days`     | 3       | Drop events older than N days at ingest      |
| `retention_days`          | 30      | Prune events older than N days from the DB   |
| `request_timeout`         | 20      | HTTP read timeout per source (seconds)       |
| `alerts.enabled`          | true    | Fire push notifications for matching events  |
| `scrape_log_retention_days` | 14    | Drop scrape_log rows older than N days       |
| `alerts_log_retention_days` | 60    | Drop alerts_fired rows older than N days     |
| `vacuum_on_startup`       | true    | Run VACUUM + ANALYZE once per launch         |
| `dedup.tight_window_min`  | 15      | Pass 1 dedup time window (minutes)           |
| `dedup.wide_window_min`   | 240     | Pass 2 dedup time window (minutes)           |
| `dedup.cluster_window_min`| 720     | Pass 3 dedup time window (minutes)           |

Per-source overrides (e.g. `max_age_days`, `relevance_gate`,
`theater_hint`) live alongside each entry under `sources` in
[`config.json`](config.json).

## Status

Working prototype. Not production-ready.

## License

MIT
