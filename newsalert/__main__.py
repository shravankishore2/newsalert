"""CLI: python -m newsalert {build-universe,token,live,serve,demo,smoke-test,fetch-history,replay}"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .auth import AuthError, DhanAuth
from .clients import DhanClient, FetchError, Instrument, RssFeed
from .config import Secrets, Ticker, load_config, load_secrets, load_tickers
from .market import IST, MarketCalendar
from .ratelimit import RateLimiter
from .signals import Engine, Params
from .store import Store

log = logging.getLogger("newsalert")


def _tickers(cfg: dict, only: str | None) -> list[Ticker]:
    tickers = load_tickers(cfg["tickers_file"])
    if only:
        want = {s.strip().upper() for s in only.split(",") if s.strip()}
        tickers = [t for t in tickers if t.symbol in want]
        unknown = want - {t.symbol for t in tickers}
        if unknown:
            raise SystemExit(f"not in {cfg['tickers_file']}: {', '.join(sorted(unknown))}")
    return tickers


def _index(cfg: dict) -> Instrument:
    i = cfg["index"]
    return Instrument(i["symbol"], str(i["security_id"]), i["segment"], i["instrument"])


def _require(sec: Secrets, *names: str) -> None:
    missing = sec.missing(*names)
    if missing:
        raise SystemExit(f"missing in .env: {', '.join(missing)}")


def _require_dhan(sec: Secrets) -> None:
    """Client ID plus either a pasted access token or TOTP credentials."""
    _require(sec, "dhan_client_id")
    if not sec.dhan_access_token and sec.missing("dhan_pin", "dhan_totp_secret"):
        raise SystemExit("missing in .env: DHAN_ACCESS_TOKEN, or DHAN_PIN and DHAN_TOTP_SECRET")


def _auth(http: httpx.AsyncClient, cfg: dict, sec: Secrets) -> DhanAuth:
    d = cfg["dhan"]
    try:
        auth = DhanAuth(http, sec.dhan_client_id, sec.dhan_pin, sec.dhan_totp_secret, cfg["token_cache_path"],
                        auth_url=d["auth_url"], refresh_margin=timedelta(hours=d["token_refresh_margin_h"]),
                        manual_token=sec.dhan_access_token)
    except AuthError as e:
        raise SystemExit(str(e)) from None
    auth.install_redaction()
    return auth


def _dhan(http: httpx.AsyncClient, cfg: dict, auth: DhanAuth) -> DhanClient:
    d = cfg["dhan"]
    return DhanClient(http, auth, RateLimiter([(d["quote_per_second"], 1.0)]),
                      RateLimiter([(d["data_per_second"], 1.0), (d["data_per_day"], 86400.0)]), d["base_url"])


def _today_at(hhmm: str, cfg: dict) -> datetime:
    """Today's HH:MM in the market's timezone, as an aware datetime."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(cfg["market"]["timezone"])
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.now(tz).replace(hour=h, minute=m, second=0, microsecond=0)


async def cmd_news(args, cfg) -> int:
    """24/7 news service: poll feeds, classify (NSE by rules, BusinessLine by Gemini), raise news alerts."""
    from .news.gemini import GeminiClassifier
    from .news.ingest import Feed, NewsService
    sec = load_secrets()
    n, g = cfg["news"], cfg["news"]["gemini"]
    tickers = {t.symbol: t.name for t in load_tickers(cfg["tickers_file"])}
    store = Store(cfg["db_path"])
    async with httpx.AsyncClient(timeout=g["timeout_s"]) as http:
        gem = None
        if sec.gemini_api_key:
            gem = GeminiClassifier(http, sec.gemini_api_key, model=g["model"], tickers=tickers, store=store,
                                   rpm=g["requests_per_minute"], rpd=g["requests_per_day"],
                                   batch_size=g["batch_size"], base_url=g["base_url"])
        else:
            log.warning("GEMINI_API_KEY not set: BusinessLine items stay pending; NSE filings are still classified")
        exp = None
        if sec.finnhub_api_key:
            from .news.expectations import FinnhubEarnings
            e = n["expectations"]
            exp = FinnhubEarnings(http, sec.finnhub_api_key, store, base_url=e["base_url"],
                                  per_minute=e["per_minute"], symbol_suffix=e["symbol_suffix"])
        svc = NewsService(http=http, store=store, tickers=tickers, gemini=gem, expectations=exp,
                          feeds=[Feed(f["name"], f["source"], f["url"]) for f in n["feeds"]],
                          poll_s=n["poll_s"], max_age_min=n["max_age_min"], min_confidence=n["min_confidence"])
        await svc.run(max_polls=args.polls)
    return 0


def cmd_evaluate_news(args, cfg) -> int:
    """Event study for news alerts whose session has closed; rewrites the news section of RESULTS.md."""
    from .news.evaluate import evaluate_pending, render_news_results
    from .replay import write_results
    store = Store(cfg["db_path"])
    n = evaluate_pending(store, MarketCalendar.from_config(cfg["market"]),
                         lambda s, a, b: store.prices_between("quotes", s, a, b), cfg["index"]["symbol"])
    write_results(args.out, "news", render_news_results(store, generated=datetime.now(timezone.utc),
                                                        min_n=args.min_sample))
    print(f"evaluated {n} affected-stock rows; updated news section of {args.out}")
    return 0


def cmd_is_trading_window(args, cfg) -> int:
    """Exit 0 if today is an NSE trading day and it's before --until; 1 otherwise.
    Used as systemd ExecCondition, where exit 1 skips the run without marking it failed."""
    cal = MarketCalendar.from_config(cfg["market"])
    now = datetime.now(IST)
    if not cal.is_trading_day(now.date()):
        print(f"{now.date()} is not an NSE trading day")
        return 1
    if now >= _today_at(args.until, cfg):
        print(f"past {args.until} IST")
        return 1
    print(f"{now.date()} is a trading day; running until {args.until} IST")
    return 0


def cmd_build_universe(args, cfg) -> int:
    from .universe import SCRIP_MASTER_URL, build_universe, write_tickers
    nifty = Path(args.nifty500_csv).read_text()
    scrip = httpx.get(SCRIP_MASTER_URL, timeout=120).text
    rows, missing = build_universe(nifty, scrip)
    write_tickers(cfg["tickers_file"], rows)
    print(f"wrote {len(rows)} tickers to {cfg['tickers_file']}")
    for m in missing:
        print(f"  not mapped to a Dhan NSE equity: {m['symbol']} ({m['name']}, {m['isin']})")
    return 0


async def cmd_token(args, cfg) -> int:
    sec = load_secrets()
    _require_dhan(sec)
    async with httpx.AsyncClient(timeout=cfg["dhan"]["timeout_s"]) as http:
        auth = _auth(http, cfg, sec)
        try:
            if auth.can_generate and (args.force or auth.needs_refresh()):
                tok = await auth.generate()
            else:
                await auth.ensure()
                tok = auth.token
        except AuthError as e:
            print(f"FAIL: {e}", file=sys.stderr)
            return 1
    how = "auto-refresh via TOTP" if auth.can_generate else "pasted token; no auto-refresh without DHAN_PIN/DHAN_TOTP_SECRET"
    print(f"token valid until {tok.expiry.isoformat(timespec='minutes')} ({how})")
    return 0


async def cmd_live(args, cfg) -> int:
    from .live import LiveMonitor
    sec = load_secrets()
    _require_dhan(sec)
    store = Store(cfg["db_path"])
    index = _index(cfg)
    async with httpx.AsyncClient(timeout=cfg["dhan"]["timeout_s"]) as http:
        auth = _auth(http, cfg, sec)
        mon = LiveMonitor(
            tickers=_tickers(cfg, args.tickers), index=index,
            engine=Engine(Params.from_config(cfg["alerts"]), index.symbol), dhan=_dhan(http, cfg, auth),
            auth=auth, store=store, calendar=MarketCalendar.from_config(cfg["market"]),
            cycle_s=cfg["dhan"]["cycle_s"], token_refresh_lead_min=cfg["dhan"]["token_refresh_lead_min"],
            link_window_min=cfg["news"]["link_window_min"])
        stop = datetime.now(timezone.utc) + timedelta(minutes=args.minutes) if args.minutes else None
        if args.until:
            cut = _today_at(args.until, cfg)
            stop = min(stop, cut) if stop else cut
        await mon.run(max_cycles=args.cycles, stop_after=stop)
    store.close()
    return 0


async def cmd_smoke_test(args, cfg) -> int:
    """Exercise every external dependency once (token, LTP, history, feeds)."""
    sec = load_secrets()
    _require_dhan(sec)
    ok, lines = True, [f"newsalert smoke test {datetime.now(IST):%Y-%m-%d %H:%M IST}"]
    tickers = _tickers(cfg, args.symbol)
    async with httpx.AsyncClient(timeout=cfg["dhan"]["timeout_s"]) as http:
        auth = _auth(http, cfg, sec)
        dhan = _dhan(http, cfg, auth)
        ins = [_index(cfg)] + [Instrument(t.symbol, t.security_id, "NSE_EQ", "EQUITY") for t in tickers]
        checks = [
            ("dhan token", auth.ensure()),
            ("dhan LTP", dhan.ltp(ins)),
            ("dhan 1-min history (1 day)", dhan.intraday(ins[1], datetime.now(IST) - timedelta(days=3), datetime.now(IST))),
        ]
        for f in cfg["news"]["feeds"]:
            checks.append((f"feed {f['name']}", RssFeed(http, f["name"], f["url"]).items()))
        for label, coro in checks:
            try:
                res = await coro
                summary = "ok" if label == "dhan token" else (res if isinstance(res, dict) else f"{len(res)} items")
                lines.append(f"OK {label}: {summary}")
            except (FetchError, AuthError) as e:
                ok = False
                lines.append(f"FAIL {label}: {e}")
        print("\n".join(lines))
    return 0 if ok else 1


async def cmd_fetch_history(args, cfg) -> int:
    from .history import fetch_history
    sec = load_secrets()
    _require_dhan(sec)
    ins = [_index(cfg)] + [Instrument(t.symbol, t.security_id, "NSE_EQ", "EQUITY")
                           for t in _tickers(cfg, args.tickers)]
    store = Store(cfg["history_db_path"])
    async with httpx.AsyncClient(timeout=60) as http:
        auth = _auth(http, cfg, sec)
        report = await fetch_history(store, _dhan(http, cfg, auth), ins,
                                     args.days or cfg["replay"]["history_days"], datetime.now(IST))
    print(f"stored {report['bars']} bars; total {store.bar_summary('bars')}")
    for k in ("failed", "empty"):
        if report[k]:
            print(f"{k} ({len(report[k])}): {', '.join(report[k][:20])}{' ...' if len(report[k]) > 20 else ''}")
    if report["out_of_session"]:
        print("WARNING: bars outside 09:15-15:30 IST (timestamp convention?) for "
              f"{len(report['out_of_session'])} instruments, e.g. {list(report['out_of_session'].items())[:5]}")
        return 1
    return 0


def _ticker_info(path: str, sector_col: str) -> dict[str, dict]:
    import csv
    with open(path, newline="") as f:
        return {r["symbol"]: {"name": r.get("name", ""), "sector": r.get(sector_col, "")} for r in csv.DictReader(f)}


def _alert_params(cfg: dict) -> dict:
    a = cfg["alerts"]
    return {"move_threshold": a["move_threshold"], "move_window_min": a["move_window_min"],
            "ma_fast_min": a["ma_filter"]["fast_min"], "ma_slow_min": a["ma_filter"]["slow_min"],
            "ma_confirm_frac": a["ma_filter"]["confirm_frac"], "corr_min": a["corr_filter"]["min_corr"],
            "corr_returns": a["corr_filter"]["returns"], "cooldown_min": a["cooldown_min"]}


def _password(sec: Secrets, allow_generated: bool) -> str:
    if sec.dashboard_password:
        return sec.dashboard_password
    if not allow_generated:
        raise SystemExit("missing in .env: DASHBOARD_PASSWORD (the dashboard is never served without one)")
    import secrets as _s
    pw = _s.token_urlsafe(9)
    print(f"DASHBOARD_PASSWORD not set; this demo run's password is: {pw}", flush=True)
    return pw


def _serve(app, cfg: dict, args) -> int:
    import uvicorn
    d = cfg["dashboard"]
    host, port = args.host or d["host"], args.port or d["port"]
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("serving on %s: anyone who can reach this address sees the login page", host)
    print(f"dashboard: http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/", flush=True)
    # Open SSE streams never finish on their own; cap the graceful stop so restarts take seconds.
    uvicorn.run(app, host=host, port=port, log_level="warning", timeout_graceful_shutdown=3)
    return 0


def cmd_serve(args, cfg) -> int:
    """Dashboard over live data (run `live` alongside it, in another terminal)."""
    from .web.app import SiteInfo, create_app
    sec = load_secrets()
    store = Store(cfg["db_path"])  # creates tables so the dashboard can start before the first alert
    store.close()
    cal = MarketCalendar.from_config(cfg["market"])
    info = SiteInfo(mode="live", dataset="NSE Nifty 500 (live Dhan LTP)", index_name=cfg["index"]["name"],
                    index_symbol=cfg["index"]["symbol"], currency="₹", timezone=cfg["market"]["timezone"],
                    tickers=_ticker_info(cfg["tickers_file"], "industry"), params=_alert_params(cfg))
    quotes = Store(cfg["db_path"])

    def market() -> dict:
        now = datetime.now(timezone.utc)
        nxt = cal.next_open(now)
        return {"open": cal.is_open(now), "now_ts": now.timestamp(), "simulated": False,
                "next_open": nxt.isoformat() if nxt > now else None, "timezone": cfg["market"]["timezone"]}

    def token_from_cache() -> dict | None:
        """Before live mode has run, report the cached token's expiry (never the token itself)."""
        import json as _json
        try:
            d = _json.loads(Path(cfg["token_cache_path"]).read_text())
        except (OSError, ValueError):
            return None
        exp = datetime.fromisoformat(d["expiry"])
        return {"value": {"state": "valid" if exp > datetime.now(timezone.utc) else "missing",
                          "expires_at": exp.isoformat(),
                          "auto_refresh": bool(sec.dhan_pin and sec.dhan_totp_secret), "error": None},
                "updated_at": Path(cfg["token_cache_path"]).stat().st_mtime}

    app = create_app(db_path=cfg["db_path"], info=info, password=_password(sec, False), token_fallback=token_from_cache,
                     calendar=cal,
                     prices=lambda s, a, b: quotes.prices_between("quotes", s, a - 1, b), market=market,
                     push_poll_s=cfg["dashboard"]["push_poll_s"], session_hours=cfg["dashboard"]["session_hours"])
    return _serve(app, cfg, args)


def cmd_demo(args, cfg) -> int:
    """Dashboard driven by replayed history, clearly labelled as replay data."""
    from .demo import DemoDriver
    from .web.app import SiteInfo, create_app
    datasets = cfg["demo"]["datasets"]
    name = args.dataset
    if name == "auto":
        name = next((k for k in ("nse", "us") if _has_bars(datasets[k]["history_db"])), None)
        if name is None:
            raise SystemExit("no replay data: run `fetch-history` (NSE) or place the US bars in data/history_us.db")
    ds = datasets[name]
    if not _has_bars(ds["history_db"]):
        raise SystemExit(f"dataset {name!r} has no bars in {ds['history_db']}")
    sec = load_secrets()
    driver = DemoDriver(history_db=ds["history_db"], demo_db=cfg["demo"]["db_path"], dataset=ds,
                        alerts_cfg=cfg["alerts"], speed=args.speed or cfg["demo"]["speed"],
                        warm_days=cfg["demo"]["warm_days"] if args.warm_days is None else args.warm_days,
                        session=_nse_session(cfg) if name == "nse" else None,
                        news_db=cfg["db_path"] if name == "nse" else None,
                        link_window_min=cfg["news"]["link_window_min"])
    info = SiteInfo(mode="demo", dataset=ds["label"], index_name=ds["index_name"], index_symbol=ds["index_symbol"],
                    currency=ds["currency"], timezone=ds["timezone"],
                    tickers=_ticker_info(ds["tickers_file"], ds["sector_column"]), params=_alert_params(cfg))
    app = create_app(db_path=cfg["demo"]["db_path"], info=info, password=_password(sec, True),
                     calendar=MarketCalendar.from_config(cfg["market"]) if name == "nse" else None,
                     prices=driver.prices, market=driver.market_state, background=driver.run,
                     push_poll_s=cfg["dashboard"]["push_poll_s"], session_hours=cfg["dashboard"]["session_hours"])
    print(f"demo: replaying {ds['label']} at {driver.speed:g}x", flush=True)
    return _serve(app, cfg, args)


def _has_bars(path: str) -> bool:
    import sqlite3
    if not Path(path).exists():
        return False
    try:
        with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as c:
            return c.execute("SELECT 1 FROM bars LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


def _nse_session(cfg: dict) -> tuple[int, int, int] | None:
    """(UTC offset, open, close) in seconds for the fixed-offset NSE session; None elsewhere."""
    m = cfg["market"]
    if m["timezone"] != "Asia/Kolkata":
        return None
    hm = lambda t: int(t[:2]) * 3600 + int(t[3:5]) * 60
    return (5 * 3600 + 1800, hm(m["open"]), hm(m["close"]))


def _data_gaps(src: Store, index: str, session: tuple[int, int, int]) -> list[str]:
    """Measured gaps in the stored bars, stated in RESULTS.md rather than hidden."""
    off, o, c = session
    out_of_hours = src.conn.execute(
        "SELECT COUNT(*) FROM bars WHERE ((ts+?)%86400) < ? OR ((ts+?)%86400) >= ?", (off, o, off, c)).fetchone()[0]
    short = src.conn.execute(
        """SELECT COUNT(*), SUM(last < ?), MIN(CASE WHEN last < ? THEN d END) FROM (
             SELECT symbol, date(ts+?, 'unixepoch') d, MAX((ts+?)%86400) last FROM bars
             WHERE symbol != ? AND ((ts+?)%86400) >= ? AND ((ts+?)%86400) < ? GROUP BY symbol, d)""",
        (c - 60, c - 60, off, off, index, off, o, off, c)).fetchone()
    notes = []
    if short[1]:
        notes.append(f"Dhan's 1-minute stock history ends before 15:29 IST on {short[1]:,} of {short[0]:,} "
                     f"stock-days ({short[1] / short[0]:.1%}), all from {short[2]} on; the missing minutes are "
                     "the end of the session (most such days stop at 15:14). NIFTY 50 bars are complete. Alerts "
                     "whose 30-minute follow-up falls into that gap are counted as not evaluable and left out, so "
                     "late-session alerts are under-represented in the rate (whether they differ was not measured). "
                     "Live LTP polling is not affected.")
    if out_of_hours:
        notes.append(f"{out_of_hours:,} bars stamped outside 09:15-15:30 IST were dropped, as live mode "
                     "only polls during the session.")
    return notes


def cmd_replay(args, cfg) -> int:
    from .replay import render_results, replay, write_results
    table = "bars" if args.source == "history" else "quotes"
    src = Store(cfg["history_db_path"] if table == "bars" else cfg["db_path"])
    summary = src.bar_summary(table)
    if not summary[0]:
        print(f"no data in {table}; run fetch-history (or live mode) first", file=sys.stderr)
        return 2
    sink = Store(args.replay_db)
    index = cfg["index"]["symbol"]
    t0 = time.monotonic()
    session = _nse_session(cfg)
    run_id, results = replay(src, table, cfg["alerts"], cfg["replay"], index, sink, session)
    elapsed = time.monotonic() - t0
    live = Store(cfg["db_path"])
    desc = ("Dhan intraday 1-minute bars (`/v2/charts/intraday`)" if table == "bars"
            else "LTP samples recorded by live mode")
    text = render_results(
        title="NSE (Nifty 500, index NIFTY 50)", source_desc=desc, summary=summary,
        trading_days=src.trading_days(table), results=results, alerts_cfg=cfg["alerts"],
        replay_cfg=cfg["replay"], live_latencies=live.live_latencies(), min_sample=args.min_sample,
        generated=datetime.now(timezone.utc), run_id=run_id, elapsed_s=elapsed, tz=IST, index_name="NIFTY 50",
        caveats=["Live mode checks every ticker once per 60 s cycle with one batched Dhan LTP request, the "
                 "same cadence as the 1-minute bars used here, so the filters behave as measured. LTP "
                 "has no trade timestamp; live samples are stamped when the reply is received."]
                + (_data_gaps(src, index, session) if session and table == "bars" else []))
    write_results(args.out, "nse", text)
    for v, r in results.items():
        print(f"{v}: {len(r.alerts)} alerts, {r.evaluable} evaluable, {r.false} false")
    print(f"updated NSE section of {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    from .auth import OVERRIDE_FLAG, allow_generation_here
    argv = list(sys.argv[1:] if argv is None else argv)
    if OVERRIDE_FLAG in argv:                     # accepted anywhere on the command line
        argv.remove(OVERRIDE_FLAG)
        allow_generation_here()
        print(f"⚠️  {OVERRIDE_FLAG}: a token generated here kills the VM's token", file=sys.stderr)
    ap = argparse.ArgumentParser(prog="newsalert")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("build-universe", help="rebuild tickers.csv from NSE's Nifty 500 CSV + Dhan scrip master")
    p.add_argument("nifty500_csv", help="ind_nifty500list.csv downloaded by hand from NSE")
    p = sub.add_parser("token", help="generate/refresh the Dhan access token via TOTP")
    p.add_argument("--force", action="store_true", help="generate even if the cached token is still valid")
    p = sub.add_parser("live", help="poll Dhan LTP during NSE hours and send alerts")
    p.add_argument("--tickers", help="comma-separated subset, e.g. RELIANCE,TCS,INFY")
    p.add_argument("--cycles", type=int, help="stop after N polling cycles")
    p.add_argument("--minutes", type=float, help="stop after N minutes")
    p.add_argument("--until", help="stop at this time today, market timezone (e.g. 15:35)")
    p = sub.add_parser("news", help="24/7 news service: ingest, classify, raise news alerts")
    p.add_argument("--polls", type=int, help="stop after N polls (default: run forever)")
    p = sub.add_parser("evaluate-news", help="event study for news alerts; updates docs/RESULTS.md")
    p.add_argument("--out", default="docs/RESULTS.md")
    p.add_argument("--min-sample", type=int, default=30)
    p = sub.add_parser("is-trading-window", help="exit 0 on an NSE trading day before --until (for systemd)")
    p.add_argument("--until", default="15:35")
    p = sub.add_parser("serve", help="dashboard over live data (run `live` alongside)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p = sub.add_parser("demo", help="dashboard driven by replayed history; no credentials needed")
    p.add_argument("--dataset", choices=["auto", "nse", "us"], default="auto")
    p.add_argument("--speed", type=float, help="replayed market minutes per real minute (default 60)")
    p.add_argument("--warm-days", type=int, help="days replayed instantly before pacing starts")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p = sub.add_parser("smoke-test", help="check Dhan token, LTP, history and news feeds once")
    p.add_argument("--symbol", default="RELIANCE")
    p = sub.add_parser("fetch-history", help="download 1-minute bars from Dhan for replay")
    p.add_argument("--tickers")
    p.add_argument("--days", type=int)
    p = sub.add_parser("replay", help="run alert logic over stored data and update docs/RESULTS.md")
    p.add_argument("--source", choices=["history", "live"], default="history")
    p.add_argument("--out", default="docs/RESULTS.md")
    p.add_argument("--replay-db", default="data/replay.db")
    p.add_argument("--min-sample", type=int, default=100)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full URLs at INFO; the token URL carries the PIN and TOTP.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    cfg = load_config(args.config)
    if args.cmd == "build-universe":
        return cmd_build_universe(args, cfg)
    if args.cmd == "replay":
        return cmd_replay(args, cfg)
    if args.cmd == "is-trading-window":
        return cmd_is_trading_window(args, cfg)
    if args.cmd == "evaluate-news":
        return cmd_evaluate_news(args, cfg)
    if args.cmd == "serve":
        return cmd_serve(args, cfg)
    if args.cmd == "demo":
        return cmd_demo(args, cfg)
    handler = {"token": cmd_token, "live": cmd_live, "smoke-test": cmd_smoke_test, "news": cmd_news,
               "fetch-history": cmd_fetch_history}[args.cmd]
    return asyncio.run(handler(args, cfg))


if __name__ == "__main__":
    sys.exit(main())
