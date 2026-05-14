"""WARWATCH — Textual TUI entry point."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Grid, Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button, Footer, Header, Input, Label, ListItem, ListView, Static,
)
from rich.markup import escape as rich_escape

import models
import sitrep as sitrep_mod
from brief import BriefThread, BriefTurn, OPENING_QUESTION, CLAUDE_CLI_AVAILABLE
from scraper import run_all

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
CONFIG = json.loads((ROOT / "config.json").read_text())
STATE_PATH = ROOT / "db" / "state.json"


# ---------- time helpers ----------

def _local_hm(iso_ts: str) -> str:
    """Format an ISO UTC timestamp as local-time HH:MM.

    Falls back to the raw [11:16] slice if parsing fails, so a malformed
    timestamp never crashes row rendering.
    """
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


def _local_tz_abbr() -> str:
    try:
        return datetime.now().astimezone().strftime("%Z") or "LOCAL"
    except Exception:
        return "LOCAL"


# ---------- styles ----------

TYPE_COLOR = {
    "CLASH": "red",
    "AIRSTRIKE": "red",
    "CASUALTY": "red",
    "GROUND_OP": "red",
    "SUPPLY_DISRUPTION": "magenta",
    "MARKET_MOVE": "cyan",
    "CEASEFIRE_UPDATE": "yellow",
    "DIPLOMATIC": "yellow",
    "HUMANITARIAN": "blue",
    "ROCKET_FIRE": "white",
    "DEPLOYMENT": "white",
}


# ---------- filter definitions ----------

# Cycle order for the `c` key (confidence filter). None = all events.
CONF_FILTER_CYCLE = [None, "CONFIRMED", "NO_UNVERIFIED"]
CONF_FILTER_LABEL = {
    None: "",
    "CONFIRMED": "confirmed-only",
    "NO_UNVERIFIED": "hide-unverified",
}

# Cycle order for the `f` key (event-type filter). Groups mirror the
# classifier's kinetic/diplo clusters so one keystroke collapses the feed
# to "shooting" or "talking" — the two modes a reader usually wants.
ETYPE_GROUPS = {
    "KINETIC": {"CLASH", "GROUND_OP", "AIRSTRIKE", "ROCKET_FIRE", "CASUALTY"},
    "DIPLO": {"CEASEFIRE_UPDATE", "DIPLOMATIC"},
    "ENERGY": {"SUPPLY_DISRUPTION", "MARKET_MOVE"},
}
ETYPE_FILTER_CYCLE = [None, "KINETIC", "DIPLO", "ENERGY"]
ETYPE_FILTER_LABEL = {
    None: "",
    "KINETIC": "kinetic",
    "DIPLO": "diplomatic",
    "ENERGY": "energy",
}


def _source_stats(ev) -> tuple[int, int]:
    """(distinct_source_names, distinct_attributions). Attributions only
    count non-empty values — outlets without an attribution label (e.g.
    LiveUAMap) don't contribute to a conflict."""
    try:
        srcs = json.loads(ev["sources"] or "[]")
    except Exception:
        return 1, 0
    names = {s.get("name") for s in srcs if isinstance(s, dict) and s.get("name")}
    attrs = {s.get("attribution") for s in srcs if isinstance(s, dict) and s.get("attribution")}
    return max(len(names), 1), len(attrs)


def fmt_event_row(ev) -> str:
    ts = _local_hm(ev["timestamp"] or "")
    etype = ev["event_type"] or "?"
    loc = rich_escape(ev["location"] or "")
    summary = rich_escape((ev["summary"] or "").replace("\n", " "))
    if len(summary) > 56:
        summary = summary[:53] + "…"
    color = TYPE_COLOR.get(etype, "white")
    conf = ev["confidence"] or "REPORTED"

    n_sources, n_attrs = _source_stats(ev)
    # Contested-attribution glyph: ≥2 distinct non-empty attributions on
    # the merged source list means outlets from different sides of the
    # war are reporting this event. The raw [multi-source: …] suffix
    # lives in the summary; the glyph surfaces the same signal at feed
    # scanning distance.
    conflict = "⚠" if n_attrs >= 2 else ""

    # Confidence marker: an inline pill prefix that makes confirmations
    # visually pop against a single merged feed. The row body keeps the
    # existing type-color + bold/dim weighting. Conflict glyph rides
    # inside the pill so contested rows stay one visual unit.
    if conf == "CONFIRMED":
        pill = f" ✓×{n_sources}{conflict} "
        marker = f"[black on green]{pill}[/] "
        row_style = f"bold {color}"
    elif conf == "UNVERIFIED":
        pill = f" ?{conflict} "
        marker = f"[black on yellow]{pill}[/] "
        row_style = f"dim {color}"
    else:
        pill = f" ·{conflict} " if conflict else " · "
        marker = f"[dim]{pill}[/] "
        row_style = color

    loc_part = f" {loc}" if loc else ""
    return f"{marker}[{row_style}]{ts} {etype:<13}{loc_part} — {summary}[/{row_style}]"


# ---------- screens ----------

class DetailScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("j", "next", "Next", show=False),
        Binding("down", "next", "Next", show=False),
        Binding("k", "prev", "Prev", show=False),
        Binding("up", "prev", "Prev", show=False),
    ]

    def __init__(self, rows, index: int):
        super().__init__()
        self.rows = rows
        self.index = index

    @property
    def event_row(self):
        return self.rows[self.index]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="detail"):
            yield Static(self._build_body(), id="detail-body")
        with Horizontal(id="detail-nav"):
            yield Button("◀ Prev", id="btn-detail-prev")
            yield Button("Close", id="btn-detail-close", variant="error")
            yield Button("Next ▶", id="btn-detail-next")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-detail-close":
            self.action_dismiss()
        elif event.button.id == "btn-detail-prev":
            self.action_prev()
        elif event.button.id == "btn-detail-next":
            self.action_next()

    def action_next(self) -> None:
        if self.index < len(self.rows) - 1:
            self.index += 1
            self._refresh_body()

    def action_prev(self) -> None:
        if self.index > 0:
            self.index -= 1
            self._refresh_body()

    def _refresh_body(self) -> None:
        self.query_one("#detail-body", Static).update(self._build_body())
        # Keep the underlying feed list in sync so the highlight follows.
        app = self.app
        lv = app.query_one("#feed-list", ListView)
        if 0 <= self.index < len(lv):
            lv.index = self.index

    def _build_body(self) -> str:
        e = self.event_row
        esc = rich_escape
        lines = []
        lines.append(f"[bold]{esc(e['event_type'] or '')}[/bold]  [dim]{esc(e['confidence'] or '')}[/dim]")
        raw_ts = e["timestamp"] or ""
        local_ts = _local_hm(raw_ts)
        lines.append(f"When: {esc(raw_ts)}  [dim](local {local_ts} {_local_tz_abbr()})[/dim]")
        if e["location"]:
            lines.append(f"Where: {esc(e['location'])}")
        if e["lat"] is not None and e["lon"] is not None:
            lines.append(f"Coords: {e['lat']:.4f}, {e['lon']:.4f}")
        lines.append(f"Theater: {esc(e['theater'] or '')}")
        lines.append("")
        lines.append("[bold]Summary[/bold]")
        lines.append(esc(e["summary"] or ""))
        lines.append("")
        lines.append("[bold]Sources[/bold]")
        try:
            sources = json.loads(e["sources"] or "[]")
        except Exception:
            sources = []
        if not sources:
            lines.append("  (none)")
        for s in sources:
            if isinstance(s, dict):
                name = esc(s.get("name", "?"))
                url = esc(s.get("url", ""))
                attr = s.get("attribution")
                attr_s = f" ({esc(attr)})" if attr else ""
                lines.append(f"  • {name}{attr_s}")
                if url:
                    lines.append(f"    {url}")
        lines.append("")

        # confidence reasoning
        distinct = len({s.get("name") for s in sources if isinstance(s, dict)})
        reason = {
            "CONFIRMED": f"{distinct} independent sources corroborate.",
            "REPORTED": "Single credible source.",
            "UNVERIFIED": "Only unverified / social-media level sourcing.",
        }.get(e["confidence"], "")
        lines.append(f"[bold]Confidence[/bold]: {e['confidence']} — {reason}")

        # cross-reference note
        attrs = {s.get("attribution") for s in sources if isinstance(s, dict) and s.get("attribution")}
        if len(attrs) > 1:
            lines.append(f"[yellow]Cross-reference[/yellow]: differing attributions — {', '.join(sorted(a for a in attrs if a))}")
        lines.append("")

        # related events
        conn = models.get_conn()
        try:
            related = models.related_events(conn, e)
        finally:
            conn.close()
        lines.append("[bold]Related events[/bold]")
        if not related:
            lines.append("  (none within 1hr location / 30min theater)")
        for r in related[:10]:
            rts = _local_hm(r["timestamp"] or "")
            loc = esc(r["location"] or "")
            sm = esc((r["summary"] or "")[:70])
            lines.append(f"  • {rts} {esc(r['event_type'] or '')} {loc} — {sm}")
        lines.append("")
        lines.append("[dim]Press Esc to return[/dim]")
        return "\n".join(lines)

    def action_dismiss(self, result=None) -> None:  # type: ignore[override]
        self.app.pop_screen()


class SitrepScreen(ModalScreen):
    """SITREP modal with selectable time windows.

    Number keys swap the window without closing the modal; the body
    updates in place. Default window is 24h (the most common glance).
    """

    _WINDOWS: list[int] = [6, 12, 24]

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("1", "set_window(6)", "6h"),
        Binding("2", "set_window(12)", "12h"),
        Binding("3", "set_window(24)", "24h"),
    ]

    def __init__(self, window_hours: int = 24) -> None:
        super().__init__()
        self.window_hours = window_hours

    def compose(self) -> ComposeResult:
        with Horizontal(id="sitrep-window-bar"):
            yield Button("6hr", id="btn-sitrep-6")
            yield Button("12hr", id="btn-sitrep-12")
            yield Button("24hr", id="btn-sitrep-24")
        with VerticalScroll(id="sitrep"):
            yield Static(self._render_body(), id="sitrep-body")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        mapping = {
            "btn-sitrep-6": 6,
            "btn-sitrep-12": 12,
            "btn-sitrep-24": 24,
        }
        hours = mapping.get(event.button.id)
        if hours is not None:
            self.action_set_window(hours)

    def on_mount(self) -> None:
        self._refresh_window_bar()

    def _render_body(self) -> str:
        return rich_escape(sitrep_mod.generate_sitrep(window_hours=self.window_hours))

    def _refresh_window_bar(self) -> None:
        for hours in self._WINDOWS:
            btn = self.query_one(f"#btn-sitrep-{hours}", Button)
            btn.variant = "primary" if hours == self.window_hours else "default"

    def action_set_window(self, hours: int) -> None:
        if hours == self.window_hours:
            return
        self.window_hours = hours
        self._refresh_window_bar()
        self.query_one("#sitrep-body", Static).update(self._render_body())

    def action_dismiss(self, result=None) -> None:  # type: ignore[override]
        self.app.pop_screen()


class TheaterScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("1", "pick('LEBANON')", "Lebanon"),
        Binding("2", "pick('IRAN')", "Iran"),
        Binding("3", "pick('GAZA')", "Gaza"),
        Binding("4", "pick('SYRIA')", "Syria"),
        Binding("5", "pick('YEMEN')", "Yemen"),
        Binding("6", "pick('ENERGY')", "Energy"),
        Binding("0", "pick('ALL')", "All"),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(
                "[bold]Select theater[/bold]\n\n"
                "  [1] LEBANON\n"
                "  [2] IRAN\n"
                "  [3] GAZA\n"
                "  [4] SYRIA\n"
                "  [5] YEMEN\n"
                "  [6] ENERGY\n"
                "  [0] ALL\n\n"
                "[dim]Esc to cancel[/dim]"
            )

    def action_pick(self, theater: str) -> None:
        self.app.filter_theater = None if theater == "ALL" else theater
        self.app._save_filter_state()
        self.app.refresh_feed()
        self.app.pop_screen()

    def action_dismiss(self, result=None) -> None:  # type: ignore[override]
        self.app.pop_screen()


class SearchScreen(ModalScreen):
    """Text-filter prompt. Submitting empty clears the filter."""

    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    def compose(self) -> ComposeResult:
        with Container(id="search-box"):
            yield Static(
                "[bold]Filter feed[/bold]  [dim]substring match on summary · Enter apply · empty clears · Esc cancel[/dim]"
            )
            # Pre-populate so users can edit the current filter.
            yield Input(
                value=self.app.filter_search or "",
                placeholder="e.g. nabatieh, drone, rocket launcher…",
                id="search-input",
            )

    def on_mount(self) -> None:
        self.query_one("#search-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        q = (event.value or "").strip()
        self.app.filter_search = q or None
        self.app._save_filter_state()
        self.app.refresh_feed()
        self.app.pop_screen()

    def action_dismiss(self, result=None) -> None:  # type: ignore[override]
        self.app.pop_screen()


class BriefScreen(ModalScreen):
    """Conversational brief modal backed by a `claude -p` subprocess.

    The modal holds no thread state of its own — the WarWatchApp owns the
    BriefThread so it survives closing and reopening the modal within a
    single TUI session. `ctrl-n` replaces the app's thread with a fresh
    one; `esc` just hides the modal.
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("ctrl+n", "new_thread", "New thread"),
    ]

    def __init__(self, thread: BriefThread):
        super().__init__()
        self.thread = thread
        self._busy: bool = False

    def compose(self) -> ComposeResult:
        with Container(id="brief-box"):
            yield Static(id="brief-header")
            with VerticalScroll(id="brief-body"):
                yield Static(id="brief-conversation")
            yield Input(
                placeholder="Ask about the current situation…",
                id="brief-input",
            )
            yield Static(id="brief-hint")

    def on_mount(self) -> None:
        if not CLAUDE_CLI_AVAILABLE:
            self.query_one("#brief-header", Static).update(
                "⚠ Brief unavailable — `claude` CLI not found on PATH.\n"
                "Use SITREP (s), theater filter (t), or the CLI instead."
            )
            self.query_one("#brief-input", Input).disabled = True
            return
        self._refresh_view()
        self.query_one("#brief-input", Input).focus()
        if self.thread.is_empty() and not self._busy:
            self._auto_ask(OPENING_QUESTION)

    def _auto_ask(self, question: str) -> None:
        self._busy = True
        self._refresh_view()
        self.app.run_worker(self._do_ask(question), exclusive=False)

    # ---------- rendering ----------

    def _refresh_view(self) -> None:
        # NB: do NOT rename this back to `_render` — that collides with
        # Textual's `Widget._render()` internal, which must return a
        # Visual. Overriding it with a None-returning method crashes the
        # renderer with `'NoneType' object has no attribute render_strips`.
        #
        # Guard against the worker finishing after the modal has been
        # popped by `esc`. Once unmounted, `query_one` would raise
        # `NoMatches` — so just no-op; the DB row is already written, the
        # next time the modal opens it will pick up the completed turn.
        if not self.is_mounted:
            return
        turns = self.thread.history()
        header = (
            f"[bold]📋 BRIEFING[/bold]   "
            f"[dim]thread {self.thread.thread_id} · Opus 4.7 · "
            f"{len(turns)} turn{'s' if len(turns) != 1 else ''}[/dim]"
        )
        if self._busy:
            header += "   [yellow]⏳ thinking…[/yellow]"
        self.query_one("#brief-header", Static).update(header)

        if not turns and not self._busy:
            body = (
                "[dim]Ask anything about the live WARWATCH feed. "
                "The first turn bundles a 24h SITREP snapshot; "
                "follow-ups resume the same Claude Code session and can "
                "query the DB directly.\n\n"
                "Try:  what's changed in Lebanon in the last 6h?[/dim]"
            )
        else:
            parts: list[str] = []
            for t in turns:
                parts.append(self._render_turn(t))
            if self._busy:
                parts.append("[yellow]⏳ thinking…[/yellow]")
            body = "\n\n".join(parts)

        self.query_one("#brief-conversation", Static).update(body)
        self.query_one("#brief-hint", Static).update(
            "[dim]enter=ask · esc=close · ctrl-n=new thread[/dim]"
        )
        # Scroll to bottom so the newest turn (or the spinner) is visible.
        try:
            self.query_one("#brief-body", VerticalScroll).scroll_end(animate=False)
        except Exception:
            pass

    def _render_turn(self, t: BriefTurn) -> str:
        q_local = _local_hm(t.asked_at)
        lines: list[str] = []
        lines.append(
            f"[bold cyan]Q {q_local}[/bold cyan]  {rich_escape(t.question)}"
        )
        lines.append("[dim]──────[/dim]")
        if t.error:
            lines.append(f"[red]✗ {rich_escape(t.error)}[/red]")
            if t.answer:
                lines.append(rich_escape(t.answer))
        elif t.answer is None:
            lines.append("[yellow]…no response[/yellow]")
        else:
            lines.append(rich_escape(t.answer))
        if t.cost_usd is not None or t.duration_ms is not None:
            cost_s = f"${t.cost_usd:.4f}" if t.cost_usd is not None else "?"
            dur_s = f"{t.duration_ms/1000:.1f}s" if t.duration_ms is not None else "?"
            lines.append(f"[dim](cost {cost_s} · {dur_s})[/dim]")
        return "\n".join(lines)

    # ---------- actions ----------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._busy:
            return
        q = (event.value or "").strip()
        if not q:
            return
        event.input.value = ""
        self._busy = True
        self._refresh_view()
        # Run the subprocess call as a Textual worker so the UI doesn't
        # block. The worker is non-exclusive because we want it to coexist
        # with the background scrape worker.
        self.app.run_worker(self._do_ask(q), exclusive=False)

    async def _do_ask(self, question: str) -> None:
        try:
            await self.thread.ask(question)
        finally:
            self._busy = False
            self._refresh_view()

    def action_new_thread(self) -> None:
        if self._busy:
            return
        self.thread = BriefThread()
        # Let the app know so the next `b` reopens the same fresh thread.
        if hasattr(self.app, "_brief_thread"):
            self.app._brief_thread = self.thread  # type: ignore[attr-defined]
        self._refresh_view()
        # Clear any leftover text from the previous thread's draft, then
        # re-focus. Without the clear, a half-typed question from the old
        # thread would carry into the new one.
        input_widget = self.query_one("#brief-input", Input)
        input_widget.value = ""
        input_widget.focus()
        self._auto_ask(OPENING_QUESTION)

    def action_dismiss(self, result=None) -> None:  # type: ignore[override]
        self.app.pop_screen()


class SourceHealthScreen(ModalScreen):
    """7-day rolling scrape-success view.

    The stats bar's 24h snapshot catches a down source, but a source
    that's flapping — returning events in some windows and erroring
    in others — looks healthy today even though its data is
    unreliable. This modal shows success rate per source over 7d so
    intermittent rot is visible.
    """

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Container(id="archive-box"):
            yield Static(id="health-header")
            with VerticalScroll():
                yield Static(id="health-body")
            yield Static(
                "[dim]esc=close · window: last 7 days[/dim]",
                id="archive-hint",
            )

    def on_mount(self) -> None:
        conn = models.get_conn()
        try:
            h = models.source_health_rolling(conn, window_hours=168)
        finally:
            conn.close()
        self.query_one("#health-header", Static).update(
            "[bold]🩺 SOURCE HEALTH[/bold]   [dim]rolling 7d scrape-success[/dim]"
        )
        if not h:
            self.query_one("#health-body", Static).update(
                "[dim](no scrape history yet)[/dim]"
            )
            return
        # Order ascending by rate — the flapping / broken sources
        # surface at the top, which is where a reader triaging a
        # degraded feed actually looks.
        items = sorted(h.items(), key=lambda kv: (kv[1]["rate"], kv[0]))
        lines = []
        for name, d in items:
            pct = int(round(d["rate"] * 100))
            if pct >= 95:
                color = "green"
            elif pct >= 70:
                color = "yellow"
            else:
                color = "red"
            last_s = _local_hm(d["last"]) if d["last"] else "—"
            lines.append(
                f"[{color}]{pct:>3}%[/{color}]  "
                f"{name:<20}  "
                f"[dim]{d['ok']}✓/{d['error']}✗ · "
                f"{d['events']}ev · last {last_s}[/dim]"
            )
        self.query_one("#health-body", Static).update("\n".join(lines))

    def action_dismiss(self, result=None) -> None:  # type: ignore[override]
        self.app.pop_screen()


class BriefArchiveScreen(ModalScreen):
    """Read-only browser over prior brief threads.

    Lists threads most-recent-first with date, first question snippet,
    turn count, and total cost. Enter opens a transcript view of the
    selected thread. No resume — stale briefs reference a SITREP that
    no longer reflects the feed, so replaying them would mislead.
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("enter", "open", "Open"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._threads: list = []

    def compose(self) -> ComposeResult:
        with Container(id="archive-box"):
            yield Static(id="archive-header")
            yield ListView(id="archive-list")
            yield Static(
                "[dim]enter=open transcript · esc=close[/dim]",
                id="archive-hint",
            )

    def on_mount(self) -> None:
        self._load()

    def _load(self) -> None:
        conn = models.get_conn()
        try:
            self._threads = list(models.list_brief_threads(conn, limit=40))
        finally:
            conn.close()

        lv = self.query_one("#archive-list", ListView)
        lv.clear()
        if not self._threads:
            self.query_one("#archive-header", Static).update(
                "[bold]📚 BRIEF ARCHIVE[/bold]   [dim]no prior threads[/dim]"
            )
            return
        self.query_one("#archive-header", Static).update(
            f"[bold]📚 BRIEF ARCHIVE[/bold]   "
            f"[dim]{len(self._threads)} thread"
            f"{'s' if len(self._threads) != 1 else ''}[/dim]"
        )
        for t in self._threads:
            q = (t["first_question"] or "").strip().replace("\n", " ")
            if len(q) > 72:
                q = q[:72] + "…"
            stamp = _local_hm(t["last_asked_at"])
            date = (t["last_asked_at"] or "")[:10]
            cost = t["total_cost_usd"] or 0.0
            label = (
                f"{date} {stamp}  "
                f"[dim]{t['turn_count']}t ${cost:.3f}[/dim]  "
                f"{rich_escape(q)}"
            )
            lv.append(ListItem(Static(label)))
        lv.index = 0
        lv.focus()

    def on_list_view_selected(self, event) -> None:
        self.action_open()

    def action_open(self) -> None:
        lv = self.query_one("#archive-list", ListView)
        idx = lv.index if lv.index is not None else 0
        if not self._threads or idx < 0 or idx >= len(self._threads):
            return
        thread_id = self._threads[idx]["thread_id"]
        self.app.push_screen(BriefTranscriptScreen(thread_id))

    def action_dismiss(self, result=None) -> None:  # type: ignore[override]
        self.app.pop_screen()


class BriefTranscriptScreen(ModalScreen):
    """Read-only transcript view of a single archived thread."""

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def __init__(self, thread_id: str) -> None:
        super().__init__()
        self.thread_id = thread_id

    def compose(self) -> ComposeResult:
        with Container(id="brief-box"):
            yield Static(id="brief-header")
            with VerticalScroll(id="brief-body"):
                yield Static(id="brief-conversation")
            yield Static(
                "[dim]esc=close · read-only archive[/dim]",
                id="brief-hint",
            )

    def on_mount(self) -> None:
        conn = models.get_conn()
        try:
            turns = list(models.get_thread_turns(conn, self.thread_id))
        finally:
            conn.close()

        self.query_one("#brief-header", Static).update(
            f"[bold]📋 BRIEFING[/bold]   "
            f"[dim]thread {self.thread_id} · archive · "
            f"{len(turns)} turn{'s' if len(turns) != 1 else ''}[/dim]"
        )
        if not turns:
            self.query_one("#brief-conversation", Static).update(
                "[dim](empty thread)[/dim]"
            )
            return
        parts: list[str] = []
        for r in turns:
            parts.append(self._render_row(r))
        self.query_one("#brief-conversation", Static).update(
            "\n\n".join(parts)
        )

    def _render_row(self, r) -> str:
        q_local = _local_hm(r["asked_at"])
        lines: list[str] = [
            f"[bold cyan]Q {q_local}[/bold cyan]  "
            f"{rich_escape(r['question'] or '')}",
            "[dim]──────[/dim]",
        ]
        err = r["error"]
        ans = r["answer"]
        if err:
            lines.append(f"[red]✗ {rich_escape(err)}[/red]")
            if ans:
                lines.append(rich_escape(ans))
        elif not ans:
            lines.append("[yellow]…no response[/yellow]")
        else:
            lines.append(rich_escape(ans))
        cost = r["cost_usd"]
        dur = r["duration_ms"]
        if cost is not None or dur is not None:
            cost_s = f"${cost:.4f}" if cost is not None else "?"
            dur_s = f"{dur/1000:.1f}s" if dur is not None else "?"
            lines.append(f"[dim](cost {cost_s} · {dur_s})[/dim]")
        return "\n".join(lines)

    def action_dismiss(self, result=None) -> None:  # type: ignore[override]
        self.app.pop_screen()


# ---------- main app ----------

class WarWatchApp(App):
    TITLE = "WARWATCH"
    SUB_TITLE = "v1.0 — live conflict monitor"

    CSS = """
    Screen { background: $surface; }
    #header-bar { height: 3; padding: 0 1; background: $panel; }
    #feed-box { height: 1fr; border: round $primary; }
    #theaters { height: 9; border: round $accent; padding: 0 1; }
    #stats { height: 3; border: round $warning; padding: 0 1; }
    #feed-list { height: 1fr; }
    #action-bar {
        dock: bottom;
        height: 11;
        grid-size: 4 3;
        grid-gutter: 0 1;
        background: $panel;
        border-top: solid $accent;
        padding: 1 1 0 1;
    }
    #action-bar Button {
        width: 100%;
        height: 3;
        margin: 0;
    }
    #detail-nav {
        dock: bottom;
        height: 3;
        background: $panel;
        border-top: solid $accent;
    }
    #detail-nav Button {
        width: 1fr;
        height: 3;
        border: none;
        margin: 0;
    }
    #sitrep-window-bar {
        dock: top;
        height: 3;
        background: $panel;
        border-bottom: solid $accent;
    }
    #sitrep-window-bar Button {
        width: 1fr;
        height: 3;
        border: none;
        margin: 0;
    }
    #sitrep, #detail { padding: 1 2; }
    #search-box { width: 70%; height: auto; padding: 1 2; border: round $accent; background: $panel; }
    #search-box Input { margin-top: 1; }
    #brief-box {
        width: 90%;
        height: 85%;
        padding: 1 2;
        border: round $accent;
        background: $panel;
    }
    #brief-header { height: 1; }
    #brief-body { height: 1fr; padding: 1 0; }
    #brief-conversation { width: 100%; }
    #brief-input { margin-top: 1; }
    #brief-hint { height: 1; padding-top: 1; }
    #archive-box {
        width: 90%;
        height: 85%;
        padding: 1 2;
        border: round $accent;
        background: $panel;
    }
    #archive-header { height: 1; }
    #archive-list { height: 1fr; margin-top: 1; }
    #archive-hint { height: 1; padding-top: 1; }
    """

    BINDINGS = [
        Binding("r", "refresh_scrape", "Refresh"),
        Binding("s", "sitrep", "Sitrep"),
        Binding("b", "brief", "Brief"),
        Binding("B", "brief_archive", "Archive", show=False),
        Binding("t", "theater", "Theater"),
        Binding("c", "cycle_confidence", "Conf"),
        Binding("f", "cycle_etype", "Type"),
        Binding("slash", "search", "Search"),
        Binding("x", "clear_filters", "Clear", show=False),
        Binding("d", "detail", "Detail"),
        Binding("enter", "detail", "Detail"),
        Binding("e", "export", "Export"),
        Binding("g", "geojson_export", "GeoJSON", show=False),
        Binding("h", "source_health", "Health", show=False),
        Binding("q", "quit", "Quit"),
        Binding("j", "next", "Down", show=False),
        Binding("k", "prev", "Up", show=False),
    ]

    filter_theater: Optional[str] = reactive(None)
    filter_confidence: Optional[str] = reactive(None)
    filter_etype: Optional[str] = reactive(None)
    filter_search: Optional[str] = reactive(None)
    status_msg: str = reactive("")

    def __init__(self):
        super().__init__()
        self._rows: list = []
        self._scraping: bool = False
        # One BriefThread per TUI launch, created lazily on first `b` press.
        # Rows persist in the `briefs` table, but resume across restarts is
        # deliberately not supported (stale snapshots would mislead).
        self._brief_thread: Optional[BriefThread] = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(id="header-bar")
        with Vertical():
            with Container(id="feed-box"):
                yield Static(id="feed-title")
                yield ListView(id="feed-list")
            with Container(id="theaters"):
                yield Static("📊 THEATERS", id="theaters-title")
                yield Static(id="theaters-body")
            with Container(id="stats"):
                yield Static(id="stats-body")
        with Grid(id="action-bar"):
            yield Button("Refresh", id="btn-refresh", variant="primary")
            yield Button("Detail", id="btn-detail", variant="primary")
            yield Button("Sitrep", id="btn-sitrep")
            yield Button("Brief", id="btn-brief")
            yield Button("Theater", id="btn-theater")
            yield Button("Conf", id="btn-conf")
            yield Button("Type", id="btn-type")
            yield Button("Search", id="btn-search")
            yield Button("Clear", id="btn-clear")
            yield Button("Export", id="btn-export")
            yield Static()
            yield Button("Quit", id="btn-quit", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        handler = {
            "btn-refresh": self.action_refresh_scrape,
            "btn-sitrep": self.action_sitrep,
            "btn-brief": self.action_brief,
            "btn-theater": self.action_theater,
            "btn-conf": self.action_cycle_confidence,
            "btn-type": self.action_cycle_etype,
            "btn-search": self.action_search,
            "btn-clear": self.action_clear_filters,
            "btn-detail": self.action_detail,
            "btn-export": self.action_export,
            "btn-quit": self.exit,
        }.get(event.button.id)
        if handler:
            handler()

    def on_mount(self) -> None:
        models.init_db()
        self._prune_old_events()
        self._run_data_hygiene()
        self._load_filter_state()
        self.refresh_header()
        self.refresh_feed()
        self.set_interval(30, self.refresh_header)
        # Background auto-scrape. Skip if disabled (<=0). Interval is in
        # minutes; a 5-min default keeps the feed live without hammering
        # Google News and friends.
        auto_min = int(CONFIG.get("auto_scrape_minutes", 0) or 0)
        if auto_min > 0:
            self.set_interval(auto_min * 60, self._auto_refresh)
        # Market-context refresh. Silent by design — never touches the feed
        # or status bar, only populates the `context_snapshots` table so
        # the brief subsystem has fresh price + curve data to cite.
        ctx_min = int(CONFIG.get("context_refresh_minutes", 30) or 0)
        if ctx_min > 0:
            self.set_interval(ctx_min * 60, self._refresh_context)
            # Kick an initial fetch so the first `b`-modal already has
            # a snapshot in hand.
            self._refresh_context()

    # ---------- rendering helpers ----------

    def _filter_tags(self) -> str:
        """Inline display of active filters for the header bar."""
        tags: list[str] = []
        if self.filter_theater:
            tags.append(f"[reverse] {self.filter_theater} [/]")
        if self.filter_confidence:
            tags.append(f"[reverse] {CONF_FILTER_LABEL[self.filter_confidence]} [/]")
        if self.filter_etype:
            tags.append(f"[reverse] {ETYPE_FILTER_LABEL[self.filter_etype]} [/]")
        if self.filter_search:
            tags.append(f"[reverse] /{rich_escape(self.filter_search)} [/]")
        return ("  " + " ".join(tags)) if tags else ""

    def refresh_header(self) -> None:
        now_local = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        busy = " [yellow]⟳[/yellow]" if self._scraping else ""
        msg = f"  {self.status_msg}" if self.status_msg else ""
        self.query_one("#header-bar", Static).update(
            f"[bold]WARWATCH v1.0[/bold]{self._filter_tags()}   {now_local}{busy}{msg}"
        )

    def _apply_filters(self, rows: list) -> list:
        """Apply confidence / type-group / text filters to a row list.

        Theater filtering happens upstream in the SQL query because it's
        indexed; the other three are cheap Python-side predicates.
        """
        out = rows
        if self.filter_confidence == "CONFIRMED":
            out = [r for r in out if r["confidence"] == "CONFIRMED"]
        elif self.filter_confidence == "NO_UNVERIFIED":
            out = [r for r in out if r["confidence"] != "UNVERIFIED"]

        if self.filter_etype:
            allowed = ETYPE_GROUPS.get(self.filter_etype, set())
            out = [r for r in out if r["event_type"] in allowed]

        if self.filter_search:
            q = self.filter_search.lower()
            # Python substring still applied here so this composes with
            # confidence/etype filters that operate on the row list. The
            # FTS5-backed pre-filter happens upstream in refresh_feed
            # when no other row-level filters are active.
            out = [r for r in out if q in (r["summary"] or "").lower()
                   or q in (r["location"] or "").lower()]
        return out

    def refresh_feed(self) -> None:
        # Capture the id of the currently-selected event so we can restore
        # the cursor onto the *same event* after clear+rebuild — otherwise
        # an auto-scrape that inserts a new row at position 0 would jump
        # the reader's selection to an unrelated item.
        lv = self.query_one("#feed-list", ListView)
        prior_id = None
        prior_idx = lv.index
        if prior_idx is not None and 0 <= prior_idx < len(self._rows):
            prior_id = self._rows[prior_idx]["id"]

        conn = models.get_conn()
        try:
            raw_rows = list(models.recent_events(conn, limit=400, theater=self.filter_theater))
            self._render_theaters(conn)
            self._render_stats(conn)
        finally:
            conn.close()

        self._rows = self._apply_filters(raw_rows)[:200]

        # batch_update suppresses intermediate redraws; mount(*items) inserts
        # all ListItems in one DOM pass instead of triggering a layout on each.
        with self.batch_update():
            lv.clear()
            if not self._rows:
                lv.mount(ListItem(Label("[dim]No events match the active filters. Press [bold]x[/bold] to clear, [bold]r[/bold] to scrape.[/dim]")))
            else:
                lv.mount(*[ListItem(Label(fmt_event_row(ev))) for ev in self._rows])

        # Restore cursor onto the same event id if it survived the filter;
        # otherwise clamp the old index into the new list bounds.
        if self._rows:
            new_idx = 0
            if prior_id is not None:
                for i, r in enumerate(self._rows):
                    if r["id"] == prior_id:
                        new_idx = i
                        break
                else:
                    if prior_idx is not None:
                        new_idx = max(0, min(prior_idx, len(self._rows) - 1))
            lv.index = new_idx

        n_total = len(self._rows)
        n_conf = sum(1 for r in self._rows if r["confidence"] == "CONFIRMED")
        n_unv = sum(1 for r in self._rows if r["confidence"] == "UNVERIFIED")
        n_rep = n_total - n_conf - n_unv
        self.query_one("#feed-title", Static).update(
            f"[bold]⚡ LIVE FEED ({n_total})[/bold]   "
            f"[black on green] ✓ [/] {n_conf} confirmed   "
            f"[dim] · [/] {n_rep} reported   "
            f"[black on yellow] ? [/] {n_unv} unverified"
        )
        self.refresh_header()

    def _render_theaters(self, conn) -> None:
        lines = []
        # Apply the non-theater filters to the 6h counts so the box stays
        # consistent with the feed. Theater filter doesn't apply here —
        # each row already is one theater.
        filters_active = bool(self.filter_confidence or self.filter_etype or self.filter_search)
        # Fast path: no filters → one GROUP BY query covers all 6 theaters
        # instead of 6 round-trips (each fetching full _FEED_COLS rows
        # only to count + status-classify them).
        bulk_counts = None
        if not filters_active:
            bulk_counts = models.theater_counts_since(conn, hours=6)
        for theater in ["LEBANON", "IRAN", "GAZA", "SYRIA", "YEMEN", "ENERGY"]:
            if bulk_counts is not None:
                # We still need the row list for status/badge classification,
                # but we can short-circuit zero-count theaters.
                if not bulk_counts.get(theater):
                    badge = sitrep_mod.theater_badge("QUIET")
                    lines.append(f"{theater:<8} {badge} QUIET  [dim](0 last 6h)[/dim]")
                    continue
            evs = list(models.events_since(conn, hours=6, theater=theater))
            if filters_active:
                evs = self._apply_filters(evs)
            status = sitrep_mod.theater_status(evs, theater=theater)
            badge = sitrep_mod.theater_badge(status)
            lines.append(f"{theater:<8} {badge} {status}  [dim]({len(evs)} last 6h)[/dim]")
        self.query_one("#theaters-body", Static).update("\n".join(lines))
        title = "📊 THEATERS"
        if filters_active:
            title += "  [dim][filtered][/dim]"
        self.query_one("#theaters-title", Static).update(title)

    def _render_stats(self, conn) -> None:
        health = models.source_health(conn, window_hours=24)
        total_sources = len(CONFIG["sources"])
        ok = sum(1 for v in health.values() if v.get("ok") and not v.get("error"))
        stats = models.stats_today(conn)
        last = stats["last"]
        last_s = "—"
        if last:
            try:
                lt = last.replace("Z", "+00:00")
                ldt = datetime.fromisoformat(lt)
                if ldt.tzinfo is None:
                    ldt = ldt.replace(tzinfo=timezone.utc)
                delta = datetime.now(timezone.utc) - ldt
                mins = int(delta.total_seconds() // 60)
                if mins < 1:
                    last_s = "just now"
                elif mins < 60:
                    last_s = f"{mins}m ago"
                else:
                    last_s = f"{mins // 60}h ago"
            except Exception:
                last_s = "?"
        degraded = [name for name, v in health.items() if v.get("error") and not v.get("ok")]
        warning = ""
        if degraded:
            warning = f"  [red]⚠ degraded: {', '.join(degraded)}[/red]"
        self.query_one("#stats-body", Static).update(
            f"📈 Sources: {ok}/{total_sources} │ Today: {stats['today']} events │ Last: {last_s}{warning}"
        )

    # ---------- actions ----------

    # ---------- lifecycle helpers ----------

    def _prune_old_events(self) -> None:
        """Drop events older than `retention_days`. Runs once at startup."""
        retention = CONFIG.get("retention_days")
        if not retention or int(retention) <= 0:
            return
        conn = models.get_conn()
        try:
            n = models.prune_events(conn, int(retention))
        finally:
            conn.close()
        if n:
            self.status_msg = f"[dim]pruned {n} event{'s' if n != 1 else ''} older than {retention}d[/dim]"

    def _run_data_hygiene(self) -> None:
        """Per-startup data hygiene: prune log tables, backfill legacy
        columns, ensure FTS index is in sync, optionally VACUUM/ANALYZE.

        All steps are bounded and idempotent — safe to run on every
        launch even when nothing needs cleaning. Failures are swallowed
        with a status-bar dim line; we never want a hygiene hiccup to
        block the main feed."""
        try:
            conn = models.get_conn()
            try:
                pruned_log = models.prune_scrape_log(
                    conn, int(CONFIG.get("scrape_log_retention_days", 0) or 0)
                )
                pruned_alerts = models.prune_alerts_fired(
                    conn, int(CONFIG.get("alerts_log_retention_days", 0) or 0)
                )
                backfilled = models.backfill_first_seen_at(conn)
                # Rebuild FTS once if it's stale (legacy DB upgraded to
                # the FTS5 schema mid-life). Cheap when up to date.
                try:
                    fts_count = conn.execute(
                        "SELECT COUNT(*) FROM events_fts"
                    ).fetchone()[0]
                    ev_count = conn.execute(
                        "SELECT COUNT(*) FROM events"
                    ).fetchone()[0]
                    if fts_count < ev_count:
                        models.rebuild_fts(conn)
                except sqlite3.OperationalError:
                    # FTS5 unavailable on this SQLite build — search_events
                    # already has a LIKE fallback.
                    pass
                if CONFIG.get("vacuum_on_startup"):
                    models.vacuum_analyze(conn)
                audit = models.data_audit(conn)
            finally:
                conn.close()
        except Exception as exc:
            self.status_msg = f"[red dim]hygiene: {exc}[/red dim]"
            return
        bits = []
        if pruned_log:
            bits.append(f"-{pruned_log} log")
        if pruned_alerts:
            bits.append(f"-{pruned_alerts} alerts")
        if backfilled:
            bits.append(f"backfilled {backfilled}")
        if audit.get("unconfirmed_with_2plus_sources"):
            # Surface a quiet warning — not a crash, but worth knowing.
            bits.append(
                f"[yellow]⚠ {audit['unconfirmed_with_2plus_sources']} multi-source unconfirmed[/yellow]"
            )
        if bits:
            self.status_msg = "[dim]hygiene: " + ", ".join(bits) + "[/dim]"

    def _load_filter_state(self) -> None:
        """Restore filter pills saved by the previous run. Missing or
        malformed state is silently ignored — filters just stay at
        defaults. Theater/etype/confidence values are validated against
        the known vocabularies; anything else is dropped."""
        try:
            data = json.loads(STATE_PATH.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        t = data.get("theater")
        if t in models.THEATERS and t != "OTHER":
            self.filter_theater = t
        c = data.get("confidence")
        if c in CONF_FILTER_LABEL and c is not None:
            self.filter_confidence = c
        e = data.get("etype")
        if e in ETYPE_FILTER_LABEL and e is not None:
            self.filter_etype = e
        s = data.get("search")
        if isinstance(s, str) and s.strip():
            self.filter_search = s.strip()

    def _save_filter_state(self) -> None:
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(json.dumps({
                "theater": self.filter_theater,
                "confidence": self.filter_confidence,
                "etype": self.filter_etype,
                "search": self.filter_search,
            }))
        except OSError:
            # State persistence is best-effort; a failed write should never
            # crash the TUI (e.g. read-only FS in a dev container).
            pass

    # ---------- actions ----------

    def action_refresh_scrape(self) -> None:
        """User-initiated scrape. Loud: posts status, flashes busy indicator."""
        self._trigger_scrape(silent=False)

    def _auto_refresh(self) -> None:
        """Interval-driven scrape. Silent unless something interesting happens."""
        self._trigger_scrape(silent=True)

    def _refresh_context(self) -> None:
        """Fire-and-forget market snapshot fetch. Never surfaces errors
        to the reader — a failed fetch just leaves the previous snapshot
        in place for the brief to use.
        """
        self.run_worker(self._do_refresh_context, thread=True, exclusive=False)

    def _do_refresh_context(self) -> None:
        try:
            import context as context_mod
            snap = context_mod.fetch_market_snapshot()
            # Require at least one successful fetch — don't store empty
            # snapshots that would replace a useful previous one.
            if snap and snap.get("spot"):
                conn = models.get_conn()
                try:
                    models.insert_context_snapshot(conn, "market", snap)
                    models.prune_context_snapshots(conn, keep_per_kind=48)
                finally:
                    conn.close()
        except Exception:
            # Silent by design — see docstring.
            pass

    def _trigger_scrape(self, silent: bool) -> None:
        if self._scraping:
            # A scrape is already in flight. For user-initiated refreshes
            # we still say so; background ticks just no-op.
            if not silent:
                self.status_msg = "[dim]…already scraping[/dim]"
                self.refresh_header()
            return
        self._scraping = True
        if not silent:
            self.status_msg = "⏳ scraping…"
        self.refresh_header()
        self.run_worker(self._do_scrape(silent=silent), exclusive=True)

    async def _do_scrape(self, silent: bool = False) -> None:
        new_n = merged_n = 0
        try:
            summary = await run_all()
            new_n = summary.get("total_new", 0)
            merged_n = summary.get("total_merged", 0)
            if silent:
                # Only poke the status bar if something actually changed —
                # otherwise the background scrape is completely invisible.
                if new_n or merged_n:
                    self.status_msg = f"[dim]auto: +{new_n} new, {merged_n} merged[/dim]"
            else:
                self.status_msg = f"✓ +{new_n} new, {merged_n} merged"
        except Exception as exc:
            if not silent:
                self.status_msg = f"[red]✗ scrape error: {exc}[/red]"
            # Silent errors are dropped — the source_health indicator in
            # the stats bar already surfaces persistently-degraded feeds.
        finally:
            self._scraping = False
        # Skip the ListView rebuild when a silent (background) scrape
        # produced no DB changes — the existing rows are still correct
        # and the rebuild would clear+remount ~200 widgets for no reason.
        # User-initiated refreshes still always repaint so the user sees
        # the "✓ +0 new" status against a freshly-rendered feed.
        if (not silent) or new_n or merged_n:
            self.refresh_feed()
        else:
            self.refresh_header()

    def action_sitrep(self) -> None:
        self.push_screen(SitrepScreen())

    def action_brief(self) -> None:
        """Open the briefing modal."""
        if not CLAUDE_CLI_AVAILABLE:
            self.notify(
                "Brief unavailable — `claude` CLI not found on PATH.\n"
                "SITREP (s), theater filter (t), and CLI remain functional.",
                title="Brief", severity="warning",
            )
            return
        if self._brief_thread is None:
            self._brief_thread = BriefThread()
        self.push_screen(BriefScreen(self._brief_thread))

    def action_brief_archive(self) -> None:
        self.push_screen(BriefArchiveScreen())

    def action_source_health(self) -> None:
        self.push_screen(SourceHealthScreen())

    def action_theater(self) -> None:
        self.push_screen(TheaterScreen())

    def action_cycle_confidence(self) -> None:
        cur = self.filter_confidence
        idx = CONF_FILTER_CYCLE.index(cur) if cur in CONF_FILTER_CYCLE else 0
        self.filter_confidence = CONF_FILTER_CYCLE[(idx + 1) % len(CONF_FILTER_CYCLE)]
        self._save_filter_state()
        self.refresh_feed()

    def action_cycle_etype(self) -> None:
        cur = self.filter_etype
        idx = ETYPE_FILTER_CYCLE.index(cur) if cur in ETYPE_FILTER_CYCLE else 0
        self.filter_etype = ETYPE_FILTER_CYCLE[(idx + 1) % len(ETYPE_FILTER_CYCLE)]
        self._save_filter_state()
        self.refresh_feed()

    def action_search(self) -> None:
        self.push_screen(SearchScreen())

    def action_clear_filters(self) -> None:
        self.filter_theater = None
        self.filter_confidence = None
        self.filter_etype = None
        self.filter_search = None
        self._save_filter_state()
        self.refresh_feed()

    def _selected_event(self):
        lv = self.query_one("#feed-list", ListView)
        idx = lv.index
        if idx is None and self._rows:
            idx = 0
        if idx is None or idx < 0 or idx >= len(self._rows):
            return None
        return self._rows[idx]

    def action_detail(self) -> None:
        lv = self.query_one("#feed-list", ListView)
        idx = lv.index if lv.index is not None else 0
        if self._rows and 0 <= idx < len(self._rows):
            self.push_screen(DetailScreen(self._rows, idx))

    def action_export(self) -> None:
        REPORTS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = REPORTS / f"export_{stamp}.json"
        conn = models.get_conn()
        try:
            rows = conn.execute("SELECT * FROM events ORDER BY timestamp DESC").fetchall()
            data = []
            for r in rows:
                d = dict(r)
                try:
                    d["sources"] = json.loads(d.get("sources") or "[]")
                except Exception:
                    pass
                data.append(d)
        finally:
            conn.close()
        path.write_text(json.dumps(data, indent=2, default=str))
        self.status_msg = f"✓ exported → {path.name}"
        self.refresh_header()

    def action_geojson_export(self) -> None:
        """Dump geocoded events as a GeoJSON FeatureCollection.

        Only events with non-null lat/lon are written — the output is
        intended for map overlays (QGIS, geojson.io, Kepler) where a
        missing coordinate would just be dropped anyway. `sources` is
        expanded from its JSON blob so downstream tooling can render
        per-outlet attribution without a second parse.
        """
        REPORTS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = REPORTS / f"events_{stamp}.geojson"
        conn = models.get_conn()
        try:
            rows = conn.execute(
                "SELECT id, timestamp, first_seen_at, location, lat, lon, "
                "event_type, summary, sources, confidence, theater "
                "FROM events "
                "WHERE lat IS NOT NULL AND lon IS NOT NULL "
                "ORDER BY timestamp DESC"
            ).fetchall()
        finally:
            conn.close()

        features = []
        for r in rows:
            try:
                sources = json.loads(r["sources"] or "[]")
            except Exception:
                sources = []
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [r["lon"], r["lat"]],
                },
                "properties": {
                    "id": r["id"],
                    "timestamp": r["timestamp"],
                    "first_seen_at": r["first_seen_at"],
                    "location": r["location"],
                    "event_type": r["event_type"],
                    "confidence": r["confidence"],
                    "theater": r["theater"],
                    "summary": r["summary"],
                    "sources": sources,
                },
            })
        fc = {"type": "FeatureCollection", "features": features}
        path.write_text(json.dumps(fc, indent=2, default=str))
        self.status_msg = f"✓ geojson → {path.name} ({len(features)} geocoded)"
        self.refresh_header()

    def on_list_view_selected(self, event) -> None:
        """Enter on a feed row opens detail."""
        self.action_detail()

    def action_next(self) -> None:
        self.query_one("#feed-list", ListView).action_cursor_down()

    def action_prev(self) -> None:
        self.query_one("#feed-list", ListView).action_cursor_up()


if __name__ == "__main__":
    WarWatchApp().run()
