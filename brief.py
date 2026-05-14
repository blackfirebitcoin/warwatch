"""WARWATCH briefing subsystem.

Wraps `claude -p` (the Claude Code CLI in non-interactive print mode) as a
conversational Q&A surface over the live event DB. One BriefThread
corresponds to one Claude Code `session_id`; turns are persisted to the
`briefs` table so the conversation can be re-read later. Threads are
deliberately not resumed across TUI restarts — data moves too fast for
stale snapshots to be useful, per design discussion.

Architecture:
  • First turn: build a stuffed prompt = SYSTEM_PREAMBLE + market snapshot +
    SITREP + analyst question, pipe to
    `claude -p --output-format json --model claude-opus-4-7
     --add-dir warwatch --permission-mode bypassPermissions` via stdin,
    cwd=warwatch. Capture the returned session_id and the snapshot's
    fetched_at.
  • Follow-up turns: `claude -p --output-format json --resume <session_id>`
    with the question. SITREP is not re-injected (server-side history
    holds it). The market snapshot is re-checked against the last
    fetched_at we injected; if a newer one exists, a refreshed block is
    prepended to the question so the model isn't reasoning off stale
    spot prices on a multi-turn thread.

Failure modes:
  • Timeout (>180s): kill subprocess, record error="timeout after 180s".
  • Non-zero exit: capture stderr (or stdout), record.
  • Malformed JSON: record parse error.
  • FileNotFoundError (claude not on PATH): record.

All DB writes open their own short-lived connection via models.get_conn()
so we don't fight the Textual app's connection lifecycle.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import models
import sitrep as sitrep_mod

ROOT = Path(__file__).resolve().parent
CLAUDE_MODEL = "claude-opus-4-7"
TURN_TIMEOUT_SEC = 180

# Quick platform check: is the `claude` CLI on PATH?
CLAUDE_CLI_AVAILABLE: bool = shutil.which("claude") is not None


# Auto-fired as the first turn when the analyst opens an empty brief.
OPENING_QUESTION = (
    "Give me a tight situational brief right now: in 4-6 sentences of prose, "
    "what's the highest-signal thing happening across the tracked theaters "
    "and why it matters. No bullets, no headers — keep it short enough to "
    "read in one glance."
)

# System preamble prepended on the first turn of every thread.
SYSTEM_PREAMBLE = """\
You are briefing a Middle East analyst using the WARWATCH database — a \
TUI that aggregates 15 news sources across two lanes: a conflict lane \
(Lebanon, Iran, Gaza, Syria, Yemen theaters) and an energy/commodity \
lane (the ENERGY theater, sourced from Reuters Energy, OPEC, EIA, UKMTO, \
Bloomberg Energy, Oilprice.com, Lloyd's List). The analyst is in front of \
the live feed and wants synthesis, not raw headlines.

You have read-only access to the warwatch/ directory. You can query the \
live SQLite DB at db/warwatch.db with `python -c "import sqlite3; ..."`, \
and read config.json / README.md / models.py / sitrep.py for schema and \
context. Do not modify anything. The schema, dedup/confidence rules, \
event types (including SUPPLY_DISRUPTION and MARKET_MOVE), and the ENERGY \
theater semantics are documented in README.md — read it if you need to \
understand a field.

Scope boundary for the energy lane: WARWATCH ingests public reporting \
only. You DO have a market reference snapshot below (Brent + WTI front \
months, Brent futures curve shape, natgas, gold, VIX, DXY, USD/TRY, \
USD/RUB) — use those numbers directly when the question touches price or \
curve structure, and cite them as "market snapshot at HH:MM UTC". What \
you do NOT have: tanker transit volumes or AIS tracking, real-time OPEC+ \
internal positioning, SPR movement truth, options-market positioning / \
implied-vol / COT data, or any non-public commodity intelligence. When a \
question moves past what's in the snapshot and the news DB, say so \
plainly — don't synthesize numbers that aren't there.

How to answer:
- Prose, not bullet-heavy structure. Paragraphs over lists.
- Cite outlets by name and local time: "per Al Jazeera at 14:30 local",
  "Reuters reported at 18:10 local".
- Distinguish CONFIRMED (≥2 independent sources) from REPORTED (single
  source). Single-source claims should be flagged as such.
- When Israeli, Hezbollah-aligned, and UN sources disagree, say so rather
  than averaging them. Contested facts are the operational point. The
  same discipline applies to energy sources: Reuters Energy, Bloomberg,
  and OPEC press carry different biases and framings.
- If the SITREP snapshot below doesn't cover the question, query the DB
  directly. Don't guess.
- Never invent events, casualty numbers, barrel counts, or quotes. If you
  don't know, say you don't know.
"""


@dataclass
class BriefTurn:
    id: int
    turn_index: int
    asked_at: str  # ISO UTC
    question: str
    answer: Optional[str]
    cost_usd: Optional[float]
    duration_ms: Optional[int]
    error: Optional[str]
    session_id: Optional[str] = None


async def _run_claude(
    prompt: str, *, resume_session: Optional[str]
) -> tuple[str, Optional[str], float, int, Optional[str]]:
    """Invoke `claude -p` as a subprocess with the given prompt.

    Returns (answer, session_id, cost_usd, duration_ms, error).
    On success, error is None. On failure, answer is "" and error is set.
    """
    args: list[str] = [
        "claude", "-p",
        "--output-format", "json",
        "--model", CLAUDE_MODEL,
    ]
    if resume_session:
        args += ["--resume", resume_session]
    else:
        args += [
            "--add-dir", str(ROOT),
            "--permission-mode", "bypassPermissions",
        ]

    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(ROOT),
        )
    except FileNotFoundError:
        return "", None, 0.0, 0, "claude CLI not found on PATH"

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt.encode("utf-8")),
            timeout=TURN_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except Exception:
            pass
        dur = int((time.monotonic() - t0) * 1000)
        return "", None, 0.0, dur, f"timeout after {TURN_TIMEOUT_SEC}s"

    dur = int((time.monotonic() - t0) * 1000)

    if proc.returncode != 0:
        err = (stderr.decode(errors="replace") or stdout.decode(errors="replace") or "").strip()
        return "", None, 0.0, dur, f"exit {proc.returncode}: {err[:500]}"

    raw = stdout.decode(errors="replace").strip()
    if not raw:
        return "", None, 0.0, dur, "empty response from claude"

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return "", None, 0.0, dur, f"bad JSON: {exc} · first 200 chars: {raw[:200]!r}"

    if payload.get("is_error"):
        msg = payload.get("result") or payload.get("subtype") or "claude reported is_error"
        return "", payload.get("session_id"), 0.0, dur, str(msg)[:500]

    answer = (payload.get("result") or "").strip()
    session_id = payload.get("session_id")
    cost = float(payload.get("total_cost_usd") or 0.0)
    # Prefer claude's own duration_ms if present; fall back to our wall clock.
    duration_ms = int(payload.get("duration_ms") or dur)

    if not answer:
        return "", session_id, cost, duration_ms, "no result text in response"

    return answer, session_id, cost, duration_ms, None


class BriefThread:
    """One conversation thread backed by a Claude Code session."""

    def __init__(self, thread_id: Optional[str] = None):
        self.thread_id = thread_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self._turns: list[BriefTurn] = []
        # The most recent session_id returned by claude. Used to --resume
        # the same conversation on follow-up turns. None until the first
        # turn succeeds.
        self._session_id: Optional[str] = None
        # fetched_at of the market snapshot most recently injected into
        # this thread's prompts. Drives the resume-time refresh check so
        # follow-up turns aren't reasoning off stale prices.
        self._last_snapshot_fetched_at: Optional[str] = None

    def history(self) -> list[BriefTurn]:
        return list(self._turns)

    def is_empty(self) -> bool:
        return not self._turns

    async def ask(self, question: str) -> BriefTurn:
        """Blocking async ask. Returns the completed turn."""
        turn_index = len(self._turns)
        asked_at = datetime.now(timezone.utc).isoformat()

        # Insert pending row so the turn is durable even if the subprocess
        # hangs or the app crashes mid-call.
        conn = models.get_conn()
        try:
            row_id = models.insert_brief_turn(
                conn, self.thread_id, turn_index, asked_at, question
            )
        finally:
            conn.close()

        if not CLAUDE_CLI_AVAILABLE:
            return self._finalize(
                row_id, turn_index, asked_at, question,
                answer=None, session_id=None, cost=None, duration_ms=0,
                error=(
                    "claude CLI not found on PATH. "
                    "SITREP (s), theater filter (t), and CLI remain fully functional."
                ),
            )

        if turn_index == 0 or self._session_id is None:
            prompt = self._build_first_turn_prompt(question)
            resume = None
        else:
            prompt = self._build_resume_prompt(question)
            resume = self._session_id

        answer, session_id, cost, duration_ms, error = await _run_claude(
            prompt, resume_session=resume
        )

        # Track the latest session_id so subsequent turns --resume it.
        # Claude Code may fork sessions internally; always trust the most
        # recent response.
        if session_id:
            self._session_id = session_id

        return self._finalize(
            row_id, turn_index, asked_at, question,
            answer=answer or None,
            session_id=session_id,
            cost=cost if cost else None,
            duration_ms=duration_ms,
            error=error,
        )

    def _build_resume_prompt(self, question: str) -> str:
        """Question for a `--resume`d turn, with a refreshed snapshot block
        prepended only if the latest market snapshot is newer than what we
        last injected. SITREP is intentionally not re-injected — it's
        chunky and less time-sensitive than spot prices."""
        try:
            conn = models.get_conn()
            try:
                snap = models.latest_context(conn, "market")
            finally:
                conn.close()
        except Exception:
            snap = None
        if not snap:
            return question
        new_ts = snap.get("fetched_at")
        if not new_ts or new_ts == self._last_snapshot_fetched_at:
            return question
        if self._last_snapshot_fetched_at and new_ts <= self._last_snapshot_fetched_at:
            return question
        try:
            import context as context_mod
            rendered = context_mod.render_context_block(snap)
        except Exception:
            return question
        if not rendered:
            return question
        self._last_snapshot_fetched_at = new_ts
        header = f"[Market snapshot refreshed since last turn — fetched {new_ts[:19]}Z]"
        return (
            header + "\n-----\n" + rendered + "\n-----\n\n" + question
        )

    def _build_first_turn_prompt(self, question: str) -> str:
        try:
            sitrep_text = sitrep_mod.generate_sitrep(window_hours=24)
        except Exception as exc:
            sitrep_text = f"(SITREP generator failed: {exc})"

        context_block = ""
        try:
            conn = models.get_conn()
            try:
                snap = models.latest_context(conn, "market")
            finally:
                conn.close()
            if snap:
                import context as context_mod
                rendered = context_mod.render_context_block(snap)
                if rendered:
                    context_block = (
                        "\nMarket reference snapshot:\n-----\n"
                        + rendered
                        + "\n-----\n"
                    )
                    self._last_snapshot_fetched_at = snap.get("fetched_at")
        except Exception:
            pass

        now_local = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        return (
            SYSTEM_PREAMBLE
            + context_block
            + f"\nCurrent SITREP snapshot (generated {now_local}):\n"
            + "-----\n"
            + sitrep_text
            + "\n-----\n\n"
            + "Analyst's question:\n"
            + question
        )

    def _finalize(
        self, row_id, turn_index, asked_at, question,
        *, answer, session_id, cost, duration_ms, error,
    ) -> BriefTurn:
        conn = models.get_conn()
        try:
            models.complete_brief_turn(
                conn, row_id,
                session_id=session_id,
                answer=answer,
                model=CLAUDE_MODEL,
                cost_usd=cost,
                duration_ms=duration_ms,
                error=error,
            )
        finally:
            conn.close()
        turn = BriefTurn(
            id=row_id,
            turn_index=turn_index,
            asked_at=asked_at,
            question=question,
            answer=answer,
            cost_usd=cost,
            duration_ms=duration_ms,
            error=error,
            session_id=session_id,
        )
        self._turns.append(turn)
        return turn
