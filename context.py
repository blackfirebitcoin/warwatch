"""WARWATCH reference-context snapshots.

Not part of the event feed — this module fetches market-state data
(spot prices, Brent futures curve) and stashes it in the `context`
table. The brief subsystem injects the latest snapshot alongside the
SITREP so answers can reason about price action and curve shape
without the analyst having to paste numbers into the chat.

Data source: Yahoo Finance v8 chart endpoint (no auth, public JSON).
Deliberately lightweight — no yfinance / pandas / numpy dependency,
just httpx (already a warwatch dep).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

ROOT = Path(__file__).resolve().parent

# Spot / near-spot instruments. Yahoo's v8 `chart` endpoint returns the
# `regularMarketPrice` + `chartPreviousClose` in the `meta` block, which
# is all we need — we aren't storing full bar history.
SPOT_SYMBOLS: dict[str, str] = {
    "BZ=F": "Brent front month",
    "CL=F": "WTI front month",
    "NG=F": "Henry Hub natgas",
    "GC=F": "Gold",
    "^VIX": "VIX",
    "DX-Y.NYB": "US dollar index (DXY)",
    "TRY=X": "USD/TRY",
    "RUB=X": "USD/RUB",
}

# Brent futures month codes — CME/ICE single-letter convention.
_MONTH_CODES = {1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
                7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"}


def _brent_curve_symbols(now: Optional[datetime] = None, n_months: int = 4) -> list[tuple[str, str]]:
    """Return the next `n_months` Brent contract symbols with human labels.

    Brent (BZ=F on Yahoo) expires ~end of the month prior to delivery,
    so at mid-April the active front is June delivery. We start two
    calendar months ahead of the current month and step forward.
    """
    now = now or datetime.now(timezone.utc)
    out = []
    month, year = now.month, now.year
    # Advance by 2 to land on the next un-expired contract
    month += 2
    while month > 12:
        month -= 12
        year += 1
    yy = year % 100
    for _ in range(n_months):
        code = _MONTH_CODES[month]
        sym = f"BZ{code}{yy:02d}.NYM"
        label = f"{datetime(year, month, 1):%b %Y}"
        out.append((sym, label))
        month += 1
        if month > 12:
            month -= 12
            year += 1
            yy = year % 100
    return out


_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
_UA = "Mozilla/5.0 (Android) warwatch-context/1"


def _fetch_meta(client: httpx.Client, sym: str) -> Optional[dict]:
    """Return the `meta` dict from Yahoo v8 chart, or None on error."""
    url = _CHART_URL.format(sym=sym.replace("^", "%5E"))
    try:
        r = client.get(url, params={"interval": "1d", "range": "5d"}, timeout=15)
        if r.status_code != 200:
            return None
        j = r.json()
        result = (j.get("chart") or {}).get("result") or []
        if not result:
            return None
        return result[0].get("meta") or None
    except Exception:
        return None


def _extract(meta: dict) -> dict:
    """Pull the fields we care about out of a meta block."""
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    pct = None
    if price is not None and prev not in (None, 0):
        try:
            pct = (float(price) - float(prev)) / float(prev) * 100.0
        except Exception:
            pct = None
    return {
        "symbol": meta.get("symbol"),
        "price": price,
        "prev_close": prev,
        "change_pct": pct,
        "currency": meta.get("currency"),
        "exchange": meta.get("exchangeName"),
        "regular_market_time": meta.get("regularMarketTime"),
    }


def fetch_market_snapshot() -> dict:
    """Blocking fetch. Returns a structured dict with spot + curve.

    Never raises — failures record an `error` field on the relevant
    section. Callers should treat the whole return as advisory context,
    not load-bearing data.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    snapshot: dict = {
        "fetched_at": now_iso,
        "spot": {},
        "brent_curve": [],
        "errors": [],
    }
    with httpx.Client(headers=headers) as client:
        # Spot block
        for sym, label in SPOT_SYMBOLS.items():
            meta = _fetch_meta(client, sym)
            if meta is None:
                snapshot["errors"].append({"section": "spot", "symbol": sym})
                continue
            snapshot["spot"][sym] = {"label": label, **_extract(meta)}
        # Brent curve
        for sym, label in _brent_curve_symbols():
            meta = _fetch_meta(client, sym)
            if meta is None:
                snapshot["errors"].append({"section": "curve", "symbol": sym})
                continue
            snapshot["brent_curve"].append(
                {"label": label, **_extract(meta)}
            )
    # Derived curve shape
    curve = snapshot["brent_curve"]
    if len(curve) >= 2:
        front = curve[0].get("price")
        back = curve[-1].get("price")
        if front and back:
            spread = float(front) - float(back)
            snapshot["curve_shape"] = {
                "front_label": curve[0]["label"],
                "back_label": curve[-1]["label"],
                "front_price": front,
                "back_price": back,
                "spread_usd": round(spread, 2),
                # Positive spread = backwardation (near > far, tight market).
                # Negative = contango (far > near, oversupply).
                "regime": "backwardation" if spread > 0 else ("contango" if spread < 0 else "flat"),
            }
    # Brent-WTI spread — a classic geopolitics proxy
    b = snapshot["spot"].get("BZ=F", {}).get("price")
    w = snapshot["spot"].get("CL=F", {}).get("price")
    if b and w:
        snapshot["brent_wti_spread_usd"] = round(float(b) - float(w), 2)
    return snapshot


def _fmt_price(p, ccy="") -> str:
    if p is None:
        return "—"
    try:
        return f"{float(p):,.2f}" + (f" {ccy}" if ccy and ccy != "USD" else "")
    except Exception:
        return str(p)


def _fmt_pct(p) -> str:
    if p is None:
        return ""
    try:
        sign = "+" if p >= 0 else ""
        return f" ({sign}{p:.2f}%)"
    except Exception:
        return ""


def render_context_block(snap: dict) -> str:
    """Render a text block suitable for inclusion in the brief preamble.

    Compact on purpose — each extra line of context is tokens billed
    against the brief subscription. One screen of market state is
    enough for Claude to anchor answers.
    """
    if not snap:
        return ""
    lines: list[str] = []
    fetched_at = snap.get("fetched_at", "")
    lines.append(f"MARKET CONTEXT (fetched {fetched_at[:19]}Z)")
    # Spot block
    spot = snap.get("spot", {})
    if spot:
        lines.append("  Spot / near-spot:")
        for sym, meta in spot.items():
            label = meta.get("label", sym)
            p = _fmt_price(meta.get("price"))
            pct = _fmt_pct(meta.get("change_pct"))
            lines.append(f"    · {label:<30} {p}{pct}")
    bw = snap.get("brent_wti_spread_usd")
    if bw is not None:
        lines.append(f"    · Brent-WTI spread              ${bw:+.2f}")
    # Curve
    curve = snap.get("brent_curve", [])
    shape = snap.get("curve_shape")
    if curve:
        lines.append("  Brent futures curve:")
        for leg in curve:
            p = _fmt_price(leg.get("price"))
            lines.append(f"    · {leg['label']:<10} {p}")
        if shape:
            lines.append(
                f"    → {shape['front_label']}→{shape['back_label']} spread "
                f"${shape['spread_usd']:+.2f} "
                f"({shape['regime']})"
            )
    errs = snap.get("errors") or []
    if errs:
        lines.append(f"  [{len(errs)} symbols failed to fetch]")
    return "\n".join(lines)


if __name__ == "__main__":
    snap = fetch_market_snapshot()
    print(render_context_block(snap))
