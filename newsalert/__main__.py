"""CLI: python -m newsalert {live,test-telegram,fetch-history,replay}"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone

import httpx

from .clients import FetchError, FinnhubClient, NewsApiClient, TelegramClient
from .config import Ticker, load_config, load_secrets, load_tickers
from .live import LiveMonitor
from .ratelimit import RateLimiter
from .signals import Engine, Params
from .store import Store


def _tickers(cfg: dict, only: str | None) -> list[Ticker]:
    tickers = load_tickers(cfg["tickers_file"])
    if only:
        want = [s.strip().upper() for s in only.split(",") if s.strip()]
        names = {t.symbol: t.name for t in tickers}
        tickers = [Ticker(s, names.get(s, "")) for s in want]
    return tickers


async def cmd_live(args, cfg) -> int:
    sec = load_secrets()
    missing = sec.missing("finnhub_api_key", "telegram_bot_token", "telegram_chat_id")
    if missing:
        print(f"missing in .env: {', '.join(missing)}", file=sys.stderr)
        return 2
    fh, tg, na = cfg["finnhub"], cfg["telegram"], cfg["newsapi"]
    store = Store(cfg["db_path"])
    limiter = RateLimiter([(fh["calls_per_second"], 1.0), (fh["calls_per_minute"], 60.0)])
    async with httpx.AsyncClient(timeout=fh["timeout_s"]) as http:
        newsapi = None
        if na["enabled"] and sec.newsapi_api_key:
            newsapi = NewsApiClient(http, sec.newsapi_api_key, store, na["daily_budget"], na["base_url"])
        mon = LiveMonitor(
            tickers=_tickers(cfg, args.tickers), index_symbol=cfg["index_symbol"],
            engine=Engine(Params.from_config(cfg["alerts"]), cfg["index_symbol"]),
            finnhub=FinnhubClient(http, sec.finnhub_api_key, limiter, fh["base_url"]),
            telegram=TelegramClient(http, sec.telegram_bot_token, sec.telegram_chat_id, tg["base_url"]),
            newsapi=newsapi, store=store, workers=fh["workers"], index_every=fh["index_every"],
            news_lookback_days=fh["news_lookback_days"], newsapi_lookback_days=na["lookback_days"])
        await mon.run(cycles=args.cycles)
    store.close()
    return 0


async def cmd_test_telegram(args, cfg) -> int:
    """End-to-end smoke test of every external API without waiting for a real alert."""
    sec = load_secrets()
    fh, na = cfg["finnhub"], cfg["newsapi"]
    store = Store(cfg["db_path"])
    ok = True
    async with httpx.AsyncClient(timeout=10) as http:
        limiter = RateLimiter([(fh["calls_per_second"], 1.0), (fh["calls_per_minute"], 60.0)])
        fin = FinnhubClient(http, sec.finnhub_api_key, limiter, fh["base_url"])
        today = datetime.now(timezone.utc).date()
        sym = args.symbol
        lines = [f"newsalert smoke test {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}"]
        for label, coro in [("finnhub quote", fin.quote(sym)),
                            ("finnhub company-news", fin.company_news(sym, today, 2))]:
            try:
                res = await coro
                lines.append(f"OK {label}: {res if label.endswith('quote') else len(res)}")
            except FetchError as e:
                ok = False
                lines.append(f"FAIL {label}: {e}")
        if sec.newsapi_api_key:
            try:
                res = await NewsApiClient(http, sec.newsapi_api_key, store, na["daily_budget"],
                                          na["base_url"]).headlines(sym, "", today, na["lookback_days"])
                lines.append(f"OK newsapi: {len(res)} articles")
            except FetchError as e:
                ok = False
                lines.append(f"FAIL newsapi: {e}")
        else:
            lines.append("SKIP newsapi: no NEWSAPI_API_KEY")
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


def cmd_fetch_history(args, cfg) -> int:
    from .history import fetch_history
    symbols = [t.symbol for t in _tickers(cfg, args.tickers)]
    if cfg["index_symbol"] not in symbols:
        symbols.insert(0, cfg["index_symbol"])
    store = Store(cfg["history_db_path"])
    n = fetch_history(store, symbols, args.days or cfg["replay"]["history_days"])
    print(f"stored {n} bars; total {store.bar_summary('bars')}")
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
    t0 = time.monotonic()
    run_id, results = replay(src, table, cfg["alerts"], cfg["replay"], cfg["index_symbol"], sink)
    elapsed = time.monotonic() - t0
    live = Store(cfg["db_path"])
    desc = ("Yahoo Finance 1-minute bars via yfinance (Finnhub candles are premium-only)"
            if table == "bars" else "quotes recorded by live mode (Finnhub /quote)")
    text = render_results(source_desc=desc, summary=summary, trading_days=src.trading_days(table), results=results, alerts_cfg=cfg["alerts"],
                          replay_cfg=cfg["replay"], live_latencies=live.live_latencies(),
                          min_sample=args.min_sample, generated=datetime.now(timezone.utc),
                          run_id=run_id, elapsed_s=elapsed)
    write_results(args.out, text)
    for v, r in results.items():
        print(f"{v}: {len(r.alerts)} alerts, {r.evaluable} evaluable, {r.false} false")
    print(f"wrote {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="newsalert")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("live", help="poll Finnhub and send alerts")
    p.add_argument("--tickers", help="comma-separated subset, e.g. AAPL,MSFT")
    p.add_argument("--cycles", type=int, help="stop after N passes over the ticker list")
    p = sub.add_parser("test-telegram", help="smoke-test all external APIs and send a Telegram message")
    p.add_argument("--symbol", default="AAPL")
    p = sub.add_parser("fetch-history", help="download 1-minute bars for replay (yfinance)")
    p.add_argument("--tickers")
    p.add_argument("--days", type=int)
    p = sub.add_parser("replay", help="run alert logic over stored data and write docs/RESULTS.md")
    p.add_argument("--source", choices=["history", "live"], default="history")
    p.add_argument("--out", default="docs/RESULTS.md")
    p.add_argument("--replay-db", default="data/replay.db")
    p.add_argument("--min-sample", type=int, default=100)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log URLs carrying tokens
    cfg = load_config(args.config)
    if args.cmd == "live":
        return asyncio.run(cmd_live(args, cfg))
    if args.cmd == "test-telegram":
        return asyncio.run(cmd_test_telegram(args, cfg))
    if args.cmd == "fetch-history":
        return cmd_fetch_history(args, cfg)
    return cmd_replay(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
