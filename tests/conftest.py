"""Shared test fixtures for the warwatch suite.

The big one is `tmp_db`: point `models.DB_PATH` at a throwaway file so
tests that exercise upsert / dedup / query helpers never touch the live
`db/warwatch.db`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the warwatch modules importable without installing the package —
# the project is a flat directory, not a package on sys.path.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Route `models.get_conn()` at a fresh per-test SQLite file."""
    import models

    db_path = tmp_path / "warwatch_test.db"
    monkeypatch.setattr(models, "DB_PATH", db_path)
    models.init_db()
    conn = models.get_conn()
    try:
        yield conn
    finally:
        conn.close()
