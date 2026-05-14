# WarWatch

Terminal dashboard for tracking geopolitical and energy-market signals
across OSINT, news, and shipping sources. Ingests, deduplicates, classifies,
and synthesizes events into SITREP-style briefings via an LLM layer.

![dashboard screenshot](docs/dashboard.png)

## What it does

- Pulls from 15 configured sources (OSINT, news, shipping)
- Fuzzy deduplication (compressed ~2,058 raw items → ~1,544 events on a
  recent run, ~25% reduction)
- Classifies into 6 theaters and 11 event types
- Stores in SQLite with full-text search and multi-source confirmation
  tracking
- Generates SITREP-style briefings on demand using an LLM synthesis layer
  over the live event database

## Why I built it

OSINT is noisy. The hard problem isn't gathering data — it's deduplication,
cross-source confirmation, and turning a wall of events into something a
human can act on. WarWatch is my prototype answer.

## Stack

Python · SQLite (FTS5) · LLM synthesis (provider-agnostic) · pytest

## Quick start

```bash
git clone https://github.com/blackfirebitcoin/warwatch
cd warwatch
pip install -r requirements.txt
cp .env.example .env  # fill in your LLM API key
python app.py
```

## Tests

```bash
pytest
```

Coverage focuses on classifier behavior, deduplication logic, and filter
composition.

## Status

Working prototype. Not production-ready.

## License

MIT
