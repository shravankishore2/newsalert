"""FastAPI dashboard: alerts, detail, status, results, and a server-sent-events push channel.

The web server only reads SQLite. Whoever produces alerts (live mode or the demo replay
driver) writes them there, possibly from another process; the push channel polls for
rows with a higher id every `push_poll_s` and sends them to connected browsers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from .auth import COOKIE, PasswordCheck, SessionSigner

ALERT_COLS = ("id, mode, symbol, ts, direction, move, ref_ts, ref_price, price, index_move, corr, beta, "
              "fast_sma, slow_sma, fast_move, residual, latency_ms, news")


@dataclass
class SiteInfo:
    """What the UI needs to label and explain the data it shows."""
    mode: str                              # "live" | "demo"
    dataset: str                           # e.g. "NSE Nifty 500 (Dhan 1-minute bars)"
    index_name: str
    index_symbol: str
    currency: str
    timezone: str
    tickers: dict[str, dict] = field(default_factory=dict)   # symbol -> {"name", "sector"}
    params: dict = field(default_factory=dict)                # alert thresholds, for explanations


PriceSource = Callable[[str, int, int], list[tuple[int, float]]]   # symbol, start, end -> [(ts, px)]
MarketState = Callable[[], dict]                                   # -> {"open": bool, ...}


def _connect(path: str) -> contextlib.closing:
    """Read-only connection, closed when the `with` block ends."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return contextlib.closing(conn)


def _pct(x: float) -> str:
    """+1.23% / −1.23% with a real minus sign, matching the UI."""
    return f"{'+' if x > 0 else '−' if x < 0 else ''}{abs(x) * 100:.2f}%"


def _pair(a: float, b: float) -> tuple[str, str]:
    """Format two prices with just enough decimals (2-5) that different values look different."""
    for n in range(2, 6):
        fa, fb = f"{a:.{n}f}", f"{b:.{n}f}"
        if fa != fb:
            return fa, fb
    return fa, fb


def _explain(row: dict, p: dict) -> dict:
    """Plain-language reasons the alert passed each filter, from the stored numbers."""
    d = row["direction"]
    need = p["ma_confirm_frac"] * p["move_threshold"]
    out = {}
    if row.get("fast_sma") is not None:
        moved = "rose" if d > 0 else "fell"
        fast, slow = _pair(row["fast_sma"], row["slow_sma"])
        out["ma"] = {
            "passed": True,
            "text": (f"{p['ma_fast_min']}-min SMA {fast} is {'above' if d > 0 else 'below'} the "
                     f"{p['ma_slow_min']}-min SMA {slow}, and the fast SMA itself {moved} "
                     f"{abs(row['fast_move']) * 100:.2f}% since the reference time (needs {need * 100:.2f}%), "
                     f"so this is a sustained move rather than a one-off print."),
            "fast_sma": row["fast_sma"], "slow_sma": row["slow_sma"], "fast_move": row["fast_move"]}
    if row.get("corr") is not None:
        if row.get("residual") is not None:
            text = (f"Tracks the index (corr {row['corr']:.2f} ≥ {p['corr_min']}), so the index's share was removed: "
                    f"{_pct(row['move'])} − {row['beta']:.2f} × {_pct(row['index_move'])} = {_pct(row['residual'])}, "
                    f"still past the {p['move_threshold'] * 100:.1f}% threshold, so it isn't just the market moving.")
        else:
            text = (f"Low correlation with the index (corr {row['corr']:.2f} < {p['corr_min']}), so the move is "
                    f"treated as stock-specific. The index moved {_pct(row['index_move'])} over the same window.")
        out["corr"] = {"passed": True, "text": text, "corr": row["corr"], "beta": row["beta"],
                       "index_move": row["index_move"], "residual": row["residual"]}
    return out


def _alert_json(row: sqlite3.Row, info: SiteInfo) -> dict:
    r = dict(row)
    news = json.loads(r.pop("news") or "[]")
    t = info.tickers.get(r["symbol"], {})
    r.update(name=t.get("name", ""), sector=t.get("sector", ""),
             news=[{k: n.get(k) for k in ("headline", "source", "url", "published")} for n in news],
             reasons=_explain(r, info.params))
    return r


def parse_results(text: str) -> list[dict]:
    """Split docs/RESULTS.md into market sections and pull out the false-alert table rows."""
    out = []
    for m in re.finditer(r"<!-- results:(\w+):start -->\n(.*?)\n<!-- results:\1:end -->", text, re.S):
        key, body = m.group(1), m.group(2)
        title = re.search(r"^## (.+)$", body, re.M)
        rows = []
        for r in re.finditer(r"^\| (With[^|]*) \| (\d+) \| (\d+) \| (\d+)/(\d+) = ([\d.]+)% "
                             r"\(95% CI ([\d.]+)%–([\d.]+)%\) \|$", body, re.M):
            rows.append({"variant": r.group(1).strip(), "alerts": int(r.group(2)), "evaluable": int(r.group(3)),
                         "false": int(r.group(4)), "n": int(r.group(5)), "rate": float(r.group(6)),
                         "ci_low": float(r.group(7)), "ci_high": float(r.group(8))})
        dr = re.search(r"^- Date range: (.+)$", body, re.M)
        out.append({"key": key, "title": title.group(1) if title else key, "markdown": body,
                    "false_alert_rows": rows, "date_range": dr.group(1) if dr else None,
                    "has_numbers": bool(rows)})
    return out


def create_app(*, db_path: str, info: SiteInfo, password: str, prices: PriceSource,
               market: MarketState, results_path: str = "docs/RESULTS.md",
               static_dir: str | None = "web/dist", push_poll_s: float = 1.0,
               session_hours: float = 12, secret: bytes | None = None,
               clock: Callable[[], float] = time.time, context_min: int = 60,
               background: Callable[[], Awaitable[None]] | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(background()) if background else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="newsalert dashboard", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    signer = SessionSigner(secret, ttl_s=session_hours * 3600, clock=clock)
    checker = PasswordCheck(password, clock=clock)
    app.state.info, app.state.signer, app.state.checker = info, signer, checker

    def authed(request: Request) -> None:
        if not signer.valid(request.cookies.get(COOKIE)):
            raise HTTPException(401, "login required")

    def db() -> contextlib.closing:
        if not Path(db_path).exists():
            raise HTTPException(503, "no alert database yet")
        return _connect(db_path)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api") else \
            resp.headers.get("Cache-Control", "no-cache")
        return resp

    # -- auth -------------------------------------------------------------------------
    @app.post("/api/login")
    async def login(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        wait = checker.locked_for()
        if wait > 0:
            return JSONResponse({"error": "too many attempts", "retry_after": round(wait)}, 429,
                                headers={"Retry-After": str(int(wait) + 1)})
        if not checker.check(str(body.get("password", ""))):
            return JSONResponse({"error": "wrong password"}, 401)
        resp = Response(status_code=204)
        resp.set_cookie(COOKIE, signer.issue(), max_age=int(session_hours * 3600), httponly=True,
                        samesite="strict", secure=request.url.scheme == "https", path="/")
        return resp

    @app.post("/api/logout")
    async def logout() -> Response:
        resp = Response(status_code=204)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True}

    # -- data ---------------------------------------------------------------------------
    @app.get("/api/me")
    async def me(request: Request) -> dict:
        authed(request)
        return {"mode": info.mode, "dataset": info.dataset, "index_name": info.index_name,
                "currency": info.currency, "timezone": info.timezone, "params": info.params,
                "sectors": sorted({t["sector"] for t in info.tickers.values() if t.get("sector")}),
                "symbols": sorted(info.tickers)}

    @app.get("/api/alerts")
    async def alerts(request: Request, symbol: str = "", sector: str = "", direction: str = "",
                     q: str = "", before_id: int | None = None, limit: int = 50) -> dict:
        authed(request)
        where, args = ["variant = 'filtered'"], []
        if symbol:
            where.append("symbol = ?")
            args.append(symbol.upper())
        if sector:
            syms = [s for s, t in info.tickers.items() if t.get("sector") == sector]
            where.append(f"symbol IN ({','.join('?' * len(syms)) or 'NULL'})")
            args += syms
        if direction in ("up", "down"):
            where.append("direction = ?")
            args.append(1 if direction == "up" else -1)
        if q:
            ql = q.strip().lower()
            name_hits = [s for s, t in info.tickers.items() if ql in t.get("name", "").lower() or ql in s.lower()]
            clause = "LOWER(COALESCE(news, '')) LIKE ?"
            args_q = [f"%{ql}%"]
            if name_hits:
                clause = f"({clause} OR symbol IN ({','.join('?' * len(name_hits))}))"
                args_q += name_hits
            where.append(clause)
            args += args_q
        if before_id:
            where.append("id < ?")
            args.append(before_id)
        limit = max(1, min(limit, 200))
        with db() as conn:
            rows = conn.execute(f"SELECT {ALERT_COLS} FROM alerts WHERE {' AND '.join(where)} "
                                f"ORDER BY id DESC LIMIT ?", (*args, limit + 1)).fetchall()
        items = [_alert_json(r, info) for r in rows[:limit]]
        return {"items": items, "more": len(rows) > limit}

    @app.get("/api/alerts/{alert_id}")
    async def alert_detail(request: Request, alert_id: int) -> dict:
        authed(request)
        with db() as conn:
            row = conn.execute(f"SELECT {ALERT_COLS} FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such alert")
        a = _alert_json(row, info)
        now_ts = market().get("now_ts") or clock()
        start, end = a["ref_ts"] - 60 * context_min, min(a["ts"] + 60 * context_min, int(now_ts))
        a["context"] = {"start": start, "end": end, "prices": prices(a["symbol"], start, end),
                        "index": prices(info.index_symbol, start, end)}
        return a

    def status_payload() -> dict:
        st = {}
        if Path(db_path).exists():
            with _connect(db_path) as conn:
                st = {r["key"]: {"value": json.loads(r["value"]), "updated_at": r["updated_at"]}
                      for r in conn.execute("SELECT key, value, updated_at FROM status")}
        return {"mode": info.mode, "dataset": info.dataset, "market": market(),
                "cycle": st.get("cycle"), "token": st.get("token"), "feeds": st.get("feeds"),
                "replay": st.get("replay"), "server_time": clock()}

    @app.get("/api/status")
    async def status(request: Request) -> dict:
        authed(request)
        return status_payload()

    @app.get("/api/results")
    async def results(request: Request) -> dict:
        authed(request)
        p = Path(results_path)
        return {"sections": parse_results(p.read_text()) if p.exists() else []}

    # -- push channel -------------------------------------------------------------------
    def _max_id() -> int:
        if not Path(db_path).exists():
            return 0
        with _connect(db_path) as conn:
            return conn.execute("SELECT COALESCE(MAX(id), 0) FROM alerts").fetchone()[0]

    def _new_alerts(after: int) -> list[dict]:
        if not Path(db_path).exists():
            return []
        with _connect(db_path) as conn:
            rows = conn.execute(f"SELECT {ALERT_COLS} FROM alerts WHERE variant='filtered' AND id > ? "
                                f"ORDER BY id LIMIT 100", (after,)).fetchall()
        return [_alert_json(r, info) for r in rows]

    async def event_stream(request: Request, last_id: int, max_events: int | None):
        """Alerts as `event: alert` (with `id:` so browsers resume via Last-Event-ID), a
        status snapshot about every 5 s, keep-alive comments in between. `max_events`
        (any type) ends the stream early; it exists for tests and debugging."""
        sent = 0
        yield "retry: 3000\n\n"
        status_every = max(1, round(5 / push_poll_s)) if push_poll_s > 0 else 5
        tick = 0
        while True:
            if await request.is_disconnected():
                return
            frames = [f"id: {a['id']}\nevent: alert\ndata: {json.dumps(a)}\n\n" for a in _new_alerts(last_id)]
            if frames:
                last_id = int(frames[-1].split("\n", 1)[0][4:])
            if tick % status_every == 0:
                frames.append(f"event: status\ndata: {json.dumps(status_payload())}\n\n")
            for f in frames:
                yield f
                sent += 1
                if max_events and sent >= max_events:
                    return
            if not frames:
                yield ": keep-alive\n\n"
            tick += 1
            await asyncio.sleep(push_poll_s)

    @app.get("/api/stream")
    async def stream(request: Request, since: int | None = None, max_events: int | None = None):
        authed(request)
        header = request.headers.get("last-event-id")
        last = int(header) if header and header.isdigit() else (since if since is not None else _max_id())
        return StreamingResponse(event_stream(request, last, max_events), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # -- frontend -----------------------------------------------------------------------
    dist = Path(static_dir) if static_dir else None

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        if dist is None or not (dist / "index.html").exists():
            return HTMLResponse("<p>Frontend not built. Run <code>npm --prefix web run build</code>.</p>", 503)
        f = (dist / path).resolve()
        if path and f.is_file() and dist.resolve() in f.parents:
            return FileResponse(f)
        return FileResponse(dist / "index.html")

    return app
