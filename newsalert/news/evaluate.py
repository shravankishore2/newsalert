"""Event study for news alerts, and its RESULTS.md section.

For each affected stock of each news alert:
- t0 = our alert time if the market is open then, else the next session open ("next open").
  We alert on the news, so returns are measured from when we could act, not from publication.
- Returns at +15 min, +1 hour and the session close after t0, each relative to NIFTY 50
  (abnormal = stock return - NIFTY return over the same interval). A horizon that would
  run past that session's close is left empty rather than stretched into the next day.
- A directional call is a hit if the abnormal return has the predicted sign. Exact zeros
  are ties and excluded. The baseline is a random up/down call, which hits 50% in
  expectation whatever the returns are, so each group's hit rate is tested against 0.5
  with an exact two-sided binomial test, alongside a Wilson 95% interval.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta
from typing import Callable

from ..market import IST, MarketCalendar
from ..replay import percentile, wilson
from ..store import Store

PriceFn = Callable[[str, float, float], list[tuple[int, float]]]   # symbol, start (excl), end (incl)
HORIZONS = (("15m", 15 * 60), ("1h", 60 * 60))
TOL_S = 180          # a price sample must be within 3 min of the target time
CLOSE_TOL_S = 1800   # Dhan stock bars can end at 15:14, so accept a close sample up to 30 min early


def _at_or_before(prices: PriceFn, sym: str, t: float, floor: float, tol: float) -> float | None:
    pts = prices(sym, max(floor, t - tol) - 1, t)
    return pts[-1][1] if pts else None


def _first_at_or_after(prices: PriceFn, sym: str, t: float, tol: float) -> float | None:
    pts = prices(sym, t - 1, t + tol)
    return pts[0][1] if pts else None


def event_window(cal: MarketCalendar, alert_ts: float) -> tuple[float, str, float]:
    """(t0, rule, session_close) for an alert fired at alert_ts."""
    at = datetime.fromtimestamp(alert_ts, IST)
    if cal.is_open(at):
        t0, rule = alert_ts, "alert time"
    else:
        t0, rule = cal.next_open(at).timestamp(), "next open"
    _, close = cal.session(datetime.fromtimestamp(t0, IST).date())
    return t0, rule, close.timestamp()


def evaluate_stock(cal: MarketCalendar, prices: PriceFn, index: str, sym: str, alert_ts: float) -> dict:
    t0, rule, close = event_window(cal, alert_ts)
    out = {"t0": t0, "t0_rule": rule, "eval_note": None}
    if rule == "alert time":
        p0, i0 = _at_or_before(prices, sym, t0, t0 - TOL_S, TOL_S), _at_or_before(prices, index, t0, t0 - TOL_S, TOL_S)
    else:
        p0, i0 = _first_at_or_after(prices, sym, t0, TOL_S), _first_at_or_after(prices, index, t0, TOL_S)
    if p0 is None or i0 is None:
        out["eval_note"] = "no price at t0"
        return out
    targets = [(k, t0 + s) for k, s in HORIZONS if t0 + s <= close] + [("close", close - 1)]
    for key, t in targets:
        tol = CLOSE_TOL_S if key == "close" else TOL_S
        p1, i1 = _at_or_before(prices, sym, t, t0, tol), _at_or_before(prices, index, t, t0, tol)
        if p1 is None or i1 is None:
            continue
        r, ri = p1 / p0 - 1, i1 / i0 - 1
        out[f"ret_{key}"], out[f"idx_{key}"], out[f"abn_{key}"] = r, ri, r - ri
    return out


def evaluate_pending(store: Store, cal: MarketCalendar, prices: PriceFn, index: str,
                     now: float | None = None, settle_s: float = 600) -> int:
    """Evaluate stocks whose t0 session has closed (plus settle_s). Returns how many were evaluated."""
    now = time.time() if now is None else now
    rows = store.conn.execute(
        """SELECT s.id, s.ticker, a.created_at FROM news_alert_stocks s JOIN news_alerts a ON a.id = s.news_alert_id
           WHERE s.evaluated_at IS NULL""").fetchall()
    n = 0
    for sid, sym, created in rows:
        _, _, close = event_window(cal, created)
        if now < close + settle_s:
            continue
        r = evaluate_stock(cal, prices, index, sym, created)
        cols = ["t0", "t0_rule", "eval_note"] + [f"{p}_{h}" for h in ("15m", "1h", "close") for p in ("ret", "idx", "abn")]
        store.conn.execute(f"UPDATE news_alert_stocks SET {', '.join(c + '=?' for c in cols)}, evaluated_at=? WHERE id=?",
                           (*[r.get(c) for c in cols], now, sid))
        n += 1
    store.conn.commit()
    return n


# --- statistics -----------------------------------------------------------------------------

def binom_two_sided(k: int, n: int, p: float = 0.5) -> float:
    """Exact two-sided binomial p-value (sum of outcomes no more likely than k)."""
    if n == 0:
        return math.nan
    pk = math.comb(n, k) * p ** k * (1 - p) ** (n - k)
    total = sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(n + 1)
                if math.comb(n, i) * p ** i * (1 - p) ** (n - i) <= pk * (1 + 1e-9))
    return min(1.0, total)


def hit_stats(rows: list[tuple[str, float | None]]) -> dict:
    """rows: (direction, abnormal return). Ties (0.0) and missing returns are excluded."""
    usable = [(d, a) for d, a in rows if d in ("up", "down") and a is not None and a != 0]
    k = sum((d == "up") == (a > 0) for d, a in usable)
    n = len(usable)
    lo, hi = wilson(k, n)
    return {"n": n, "hits": k, "rate": k / n if n else math.nan, "ci": (lo, hi), "p": binom_two_sided(k, n),
            "ties": sum(1 for d, a in rows if d in ("up", "down") and a == 0),
            "missing": sum(1 for d, a in rows if d in ("up", "down") and a is None),
            "no_direction": sum(1 for d, _ in rows if d not in ("up", "down"))}


def render_news_results(store: Store, *, generated: datetime, min_n: int = 30, mode: str = "live") -> str:
    rows = store.conn.execute(
        """SELECT a.event_type, s.relation, s.direction, s.abn_15m, s.abn_1h, s.abn_close, a.source, s.evaluated_at
           FROM news_alert_stocks s JOIN news_alerts a ON a.id = s.news_alert_id WHERE a.mode = ?""", (mode,)).fetchall()
    alerts = store.conn.execute("SELECT created_at, published_at, source FROM news_alerts WHERE mode=?", (mode,)).fetchall()
    items = store.conn.execute("SELECT COUNT(*), MIN(fetched_at), MAX(fetched_at) FROM news_items").fetchone()
    by_status = dict(store.conn.execute("SELECT status, COUNT(*) FROM news_items GROUP BY status").fetchall())
    evaluated = [r for r in rows if r[7] is not None]

    def fmt_rng(a, b):
        f = lambda t: datetime.fromtimestamp(t, IST).strftime("%Y-%m-%d %H:%M IST")
        return f"{f(a)} to {f(b)}" if a else "no data"

    L = ["## News-driven alerts (event study)", "",
         f"Generated {generated.strftime('%Y-%m-%d %H:%M UTC')} by `python -m newsalert evaluate-news`. "
         "Only measured values are reported; groups with fewer than "
         f"{min_n} usable calls are marked **too few** and shouldn't be read as evidence.", "",
         "### Data", "",
         f"- News items archived: {items[0]:,} ({fmt_rng(items[1], items[2])})"
         + ("; " + ", ".join(f"{k} {v:,}" for k, v in sorted(by_status.items())) if by_status else ""),
         f"- News alerts: {len(alerts):,}; affected-stock rows: {len(rows):,}; evaluated (session closed): {len(evaluated):,}",
         "- Prices: live Dhan LTP samples (60 s) recorded by live mode; NIFTY 50 as the benchmark.", "",
         "### Method", "",
         "- **Start (t0):** our alert time if the market was open, otherwise the next session open "
         "(news published outside market hours is measured from the next open).",
         "- **Returns:** stock minus NIFTY 50 over t0→+15 min, t0→+1 h and t0→that session's close. "
         "Horizons that would run past the close are left empty.",
         "- **Hit:** the abnormal return has the predicted sign. Ties excluded. Calls without a direction "
         "(most rule-classified NSE filings) are counted but not scored.",
         "- **Baseline:** a random up/down call hits 50% in expectation, so each hit rate is tested against 50% "
         "(exact two-sided binomial test) with a Wilson 95% CI.", ""]

    def table(title: str, groups: list[tuple[str, list]]) -> None:
        L.extend([f"### {title}", "", "| Group | Horizon | n | Hit rate (95% CI) | vs 50% random (p) | Note |",
                  "|---|---|---:|---|---:|---|"])
        any_row = False
        for name, grp in groups:
            for hz, col in (("+15 min", 3), ("+1 h", 4), ("close", 5)):
                st = hit_stats([(r[2], r[col]) for r in grp])
                if st["n"] == 0 and not grp:
                    continue
                any_row = True
                if st["n"] == 0:
                    L.append(f"| {name} | {hz} | 0 | — | — | no scored calls |")
                    continue
                note = "**too few**" if st["n"] < min_n else ""
                L.append(f"| {name} | {hz} | {st['n']} | {st['rate']:.1%} ({st['ci'][0]:.1%}–{st['ci'][1]:.1%}) "
                         f"| {st['p']:.3f} | {note} |")
        if not any_row:
            L.append("| — | — | 0 | — | — | no evaluated alerts yet |")
        L.append("")

    ev = evaluated
    table("Direction hit rate: all", [("All directional calls", ev)])
    types = sorted({r[0] for r in ev})
    table("Direction hit rate by event type", [(t, [r for r in ev if r[0] == t]) for t in types])
    table("Direction hit rate by relation",
          [("Direct", [r for r in ev if r[1] == "direct"]),
           ("Second-order (competitor/supplier/customer/peer)", [r for r in ev if r[1] != "direct"])])

    L.extend(["### Latency: publication to our alert", "",
              "| Source | n | p50 | p95 | Note |", "|---|---:|---:|---:|---|"])
    for src in sorted({a[2] for a in alerts}) or ["—"]:
        lat = [a[0] - a[1] for a in alerts if a[2] == src and a[1] is not None]
        if not lat:
            L.append(f"| {src} | 0 | — | — | no alerts yet |")
            continue
        neg = sum(1 for x in lat if x < 0)
        note = ("**too few**" if len(lat) < min_n else "") + (f" {neg} negative (feed time ahead of ours)" if neg else "")
        fm = lambda s: f"{s / 60:.1f} min" if s < 3600 else f"{s / 3600:.1f} h"
        L.append(f"| {src} | {len(lat)} | {fm(percentile(lat, .5))} | {fm(percentile(lat, .95))} | {note.strip()} |")
    L.extend(["", "Latency includes the feed poll interval (default 3 min) and, for BusinessLine, "
              "Gemini classification and any wait for free-tier quota. Items already older than 2 hours "
              "when first seen are archived without alerting, so they don't appear here.", ""])
    return "\n".join(L)
