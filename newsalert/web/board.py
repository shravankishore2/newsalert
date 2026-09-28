"""Company Results board and Corporate actions board.

Results cards pick the best basis the data allows, and say which:
1. "expectations": Finnhub estimate vs actual (optional, personal-use terms) -> beat/miss verdict.
2. "yoy": figures stated in a BusinessLine headline/summary (actual vs same quarter last year).
3. "reaction": neither is available -> the gauge shows the stock's reaction vs NIFTY instead.
Analyst consensus is never invented.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from ..market import IST, MarketCalendar
from ..news.evaluate import evaluate_stock
from ..news.expectations import surprise_pct, verdict

BASIS_LABEL = {
    "expectations": "Expected vs actual (Finnhub earnings calendar)",
    "yoy": "Actual vs same quarter last year, as stated in the headline (no expectations available)",
    "reaction": "Stock reaction vs NIFTY only (no expectations or stated figures available)",
}


def reaction_label(v: float | None) -> str | None:
    if v is None:
        return None
    if v <= -2:
        return "strong negative reaction"
    if v < -0.5:
        return "negative reaction"
    if v <= 0.5:
        return "flat reaction"
    if v < 2:
        return "positive reaction"
    return "strong positive reaction"


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def results_board(conn: sqlite3.Connection, info, prices, calendar: MarketCalendar | None,
                  index_symbol: str, now_ts: float, days: int = 7) -> list[dict]:
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row          # named columns whatever the connection's own row factory is
    rows = cur.execute(
        """SELECT a.id, a.created_at, a.published_at, a.source, a.headline, a.url, a.classifier, a.item_id,
                  s.ticker, s.relation, s.direction, s.reason, s.abn_15m, s.abn_1h, s.abn_close, s.t0_rule, s.evaluated_at
           FROM news_alerts a JOIN news_alert_stocks s ON s.news_alert_id = a.id
           WHERE a.event_type = 'results' AND s.relation = 'direct' AND a.created_at >= ?
           ORDER BY a.created_at DESC""", (now_ts - days * 86400,)).fetchall()
    has_items = conn.execute("SELECT 1 FROM sqlite_master WHERE name='news_items'").fetchone() is not None
    has_exp = conn.execute("SELECT 1 FROM sqlite_master WHERE name='earnings_expectations'").fetchone() is not None
    cards: dict[tuple[str, str], dict] = {}
    for r in rows:
        day = datetime.fromtimestamp(r["created_at"], IST).date().isoformat()
        key = (r["ticker"], day)
        t = info.tickers.get(r["ticker"], {})
        c = cards.setdefault(key, {
            "ticker": r["ticker"], "name": t.get("name", ""), "sector": t.get("sector", ""), "date": day,
            "first_alert_at": r["created_at"], "sources": [], "figures": None, "expectations": None,
            "reasons": [], "reaction": None, "_alert": r})
        c["first_alert_at"] = min(c["first_alert_at"], r["created_at"])
        if r["created_at"] <= c["_alert"]["created_at"]:
            c["_alert"] = r
        c["sources"].append({"source": r["source"], "headline": r["headline"], "url": r["url"],
                             "at": r["created_at"], "classifier": r["classifier"]})
        if r["reason"] and r["classifier"] == "gemini":
            c["reasons"].append(r["reason"])
        if has_items and c["figures"] is None:
            item = conn.execute("SELECT classification FROM news_items WHERE id=?", (r["item_id"],)).fetchone()
            if item and item[0]:
                res = (json.loads(item[0]) or {}).get("results")
                if res and any(v is not None for k, v in res.items() if k != "period"):
                    c["figures"] = res
    out = []
    for (sym, day), c in cards.items():
        a = c.pop("_alert")
        # reaction: stored event study, else provisional from live prices
        if a["evaluated_at"] is not None:
            c["reaction"] = {"abn_15m": a["abn_15m"], "abn_1h": a["abn_1h"], "abn_close": a["abn_close"],
                             "t0_rule": a["t0_rule"], "provisional": False}
        elif calendar is not None:
            ev = evaluate_stock(calendar, lambda s, x, y: prices(s, x, min(y, now_ts)), index_symbol, sym, a["created_at"])
            c["reaction"] = {"abn_15m": ev.get("abn_15m"), "abn_1h": ev.get("abn_1h"), "abn_close": ev.get("abn_close"),
                             "t0_rule": ev.get("t0_rule"), "provisional": True, "note": ev.get("eval_note")}
        if has_exp:
            e = conn.execute("""SELECT report_date, eps_estimate, eps_actual, revenue_estimate, revenue_actual
                                FROM earnings_expectations WHERE symbol = ?
                                ORDER BY ABS(julianday(report_date) - julianday(?)) LIMIT 1""", (sym, day)).fetchone()
            if e and abs((datetime.fromisoformat(e[0]).date() - datetime.fromisoformat(day).date()).days) <= 3:
                eps_s, rev_s = surprise_pct(e[2], e[1]), surprise_pct(e[4], e[3])
                if eps_s is not None or rev_s is not None:
                    c["expectations"] = {"report_date": e[0], "eps_estimate": e[1], "eps_actual": e[2],
                                         "revenue_estimate": e[3], "revenue_actual": e[4],
                                         "eps_surprise_pct": eps_s, "revenue_surprise_pct": rev_s}
        # gauge on the best available basis
        rx = c["reaction"] or {}
        latest = next((rx.get(k) for k in ("abn_close", "abn_1h", "abn_15m") if rx.get(k) is not None), None)
        if c["expectations"]:
            s = c["expectations"]["eps_surprise_pct"]
            if s is None:
                s = c["expectations"]["revenue_surprise_pct"]
            c["basis"] = "expectations"
            c["verdict"] = verdict(s)
            c["gauge"] = {"mode": "expectations", "value": _clip(s, -20, 20), "min": -20, "max": 20,
                          "unit": "% surprise", "label": c["verdict"]}
        else:
            c["basis"] = "yoy" if c["figures"] else "reaction"
            c["verdict"] = None
            v = None if latest is None else latest * 100
            c["gauge"] = {"mode": "reaction", "value": None if v is None else _clip(v, -5, 5), "min": -5, "max": 5,
                          "unit": "% vs NIFTY", "label": reaction_label(v) or "reaction not available yet"}
        c["basis_label"] = BASIS_LABEL[c["basis"]]
        c["briefing"] = _briefing(c)
        out.append(c)
    return sorted(out, key=lambda c: c["first_alert_at"], reverse=True)


def _fmt_cr(v: float) -> str:
    return f"₹{v:,.0f} cr" if abs(v) >= 100 else f"₹{v:,.1f} cr"


def _briefing(c: dict) -> str:
    """The headline (or the NSE filing note). Stated figures go in the card's table, not here."""
    bl = [s for s in c["sources"] if s["source"] != "nse"]
    if bl:
        return bl[0]["headline"]
    return f"{c['name'] or c['ticker']} filed its financial results with NSE."


def actions_board(conn: sqlite3.Connection, info, now_ts: float, days: int = 30) -> list[dict]:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='news_items'").fetchone() is None:
        return []
    cols = {r[1] for r in conn.execute("PRAGMA table_info(news_items)")}
    if "action_kind" not in cols:
        return []
    rows = conn.execute(
        """SELECT symbol_hint, action_kind, action_date, label, url, published_at, fetched_at FROM news_items
           WHERE source = 'nse' AND action_kind IS NOT NULL AND fetched_at >= ?
           ORDER BY COALESCE(published_at, fetched_at) DESC""", (now_ts - days * 86400,)).fetchall()
    out, seen = [], set()
    for sym, kind, when, label, url, pub, fetched in rows:
        if sym not in info.tickers:
            continue
        key = (sym, kind, when or pub)
        if key in seen:
            continue
        seen.add(key)
        t = info.tickers[sym]
        out.append({"ticker": sym, "name": t.get("name", ""), "sector": t.get("sector", ""), "kind": kind,
                    "action_date": when, "label": label, "url": url, "filed_at": pub or fetched,
                    "upcoming": bool(when and when >= datetime.fromtimestamp(now_ts, IST).date().isoformat())})
    return out
