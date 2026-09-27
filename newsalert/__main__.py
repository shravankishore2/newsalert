"""CLI: python -m newsalert {build-universe,token,live,smoke-test,fetch-history,replay}"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .auth import AuthError, DhanAuth
from .clients import DhanClient, FetchError, Instrument, RssFeed, TelegramClient, match_news
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


def _auth(http: httpx.AsyncClient, cfg: dict, sec: Secrets) -> DhanAuth:
    d = cfg["dhan"]
    auth = DhanAuth(http, sec.dhan_client_id, sec.dhan_pin, sec.dhan_totp_secret, cfg["token_cache_path"],
                    auth_url=d["auth_url"], refresh_margin=timedelta(hours=d["token_refresh_margin_h"]))
    auth.install_redaction()
    return auth


def _dhan(http: httpx.AsyncClient, cfg: dict, auth: DhanAuth) -> DhanClient:
    d = cfg["dhan"]
    return DhanClient(http, auth, RateLimiter([(d["quote_per_second"], 1.0)]),
                      RateLimiter([(d["data_per_second"], 1.0), (d["data_per_day"], 86400.0)]), d["base_url"])


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
    _require(sec, "dhan_client_id", "dhan_pin", "dhan_totp_secret")
    async with httpx.AsyncClient(timeout=cfg["dhan"]["timeout_s"]) as http:
        auth = _auth(http, cfg, sec)
        try:
            tok = await auth.generate() if args.force or auth.needs_refresh() else auth.token
        except AuthError as e:
            print(f"FAIL: {e}", file=sys.stderr)
            return 1
    print(f"token valid until {tok.expiry.isoformat(timespec='minutes')} (cached in {cfg['token_cache_path']})")
    return 0


async def cmd_live(args, cfg) -> int:
    from .live import LiveMonitor
    sec = load_secrets()
    _require(sec, "dhan_client_id", "dhan_pin", "dhan_totp_secret", "telegram_bot_token", "telegram_chat_id")
    store = Store(cfg["db_path"])
    index = _index(cfg)
    async with httpx.AsyncClient(timeout=cfg["dhan"]["timeout_s"]) as http:
        auth = _auth(http, cfg, sec)
        feeds = [RssFeed(http, f["name"], f["url"], cfg["news"]["cache_ttl_s"]) for f in cfg["news"]["feeds"]]
        mon = LiveMonitor(
            tickers=_tickers(cfg, args.tickers), index=index,
            engine=Engine(Params.from_config(cfg["alerts"]), index.symbol), dhan=_dhan(http, cfg, auth),
            auth=auth, telegram=TelegramClient(http, sec.telegram_bot_token, sec.telegram_chat_id,
                                               cfg["telegram"]["base_url"]),
            feeds=feeds, store=store, calendar=MarketCalendar.from_config(cfg["market"]),
            cycle_s=cfg["dhan"]["cycle_s"], token_refresh_lead_min=cfg["dhan"]["token_refresh_lead_min"])
        stop = datetime.now(timezone.utc) + timedelta(minutes=args.minutes) if args.minutes else None
        await mon.run(max_cycles=args.cycles, stop_after=stop)
    store.close()
    return 0


async def cmd_smoke_test(args, cfg) -> int:
    """Exercise every external dependency once (token, LTP, history, feeds, Telegram)."""
    sec = load_secrets()
    _require(sec, "dhan_client_id", "dhan_pin", "dhan_totp_secret", "telegram_bot_token", "telegram_chat_id")
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
        text = "\n".join(lines)
        print(text)
        try:
            t0 = time.perf_counter()
            await TelegramClient(http, sec.telegram_bot_token, sec.telegram_chat_id,
                                 cfg["telegram"]["base_url"]).send(text)
            print(f"OK telegram send ({(time.perf_counter() - t0) * 1000:.0f} ms)")
        except FetchError as e:
            ok = False
            print(f"FAIL telegram: {e}")
    return 0 if ok else 1


async def cmd_fetch_history(args, cfg) -> int:
    from .history import fetch_history
    sec = load_secrets()
    _require(sec, "dhan_client_id", "dhan_pin", "dhan_totp_secret")
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
    run_id, results = replay(src, table, cfg["alerts"], cfg["replay"], index, sink)
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
                 "has no trade timestamp; live samples are stamped when the reply is received."])
    write_results(args.out, "nse", text)
    for v, r in results.items():
        print(f"{v}: {len(r.alerts)} alerts, {r.evaluable} evaluable, {r.false} false")
    print(f"updated NSE section of {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
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
    p = sub.add_parser("smoke-test", help="check Dhan token, LTP, history, feeds and Telegram once")
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
    handler = {"token": cmd_token, "live": cmd_live, "smoke-test": cmd_smoke_test,
               "fetch-history": cmd_fetch_history}[args.cmd]
    return asyncio.run(handler(args, cfg))


if __name__ == "__main__":
    sys.exit(main())
