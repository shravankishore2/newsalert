# News Monitor

Async market monitoring and news alerts for the 503 S&P 500 tickers in
`tickers.csv`. Polls Finnhub for prices, runs a threshold + moving-average +
index-correlation alert filter, sends alerts to Telegram with headlines from
Finnhub company-news and NewsAPI, and stores every alert in SQLite. A replay
mode runs the same alert logic over 1-minute history and writes
[`docs/RESULTS.md`](docs/RESULTS.md).

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv -e '.[history,dev]'
cp .env.example .env   # then fill in the keys
```

`.env` (gitignored) needs:

| Variable | Where to get it | Needed for |
|---|---|---|
| `FINNHUB_API_KEY` | finnhub.io dashboard (free) | live |
| `TELEGRAM_BOT_TOKEN` | @BotFather | live |
| `TELEGRAM_CHAT_ID` | send the bot a message, then read `getUpdates` | live |
| `NEWSAPI_API_KEY` | newsapi.org (free Developer plan) | optional headlines |

## Usage

```bash
.venv/bin/python -m newsalert test-telegram --symbol AAPL   # smoke-test every API + send one message
.venv/bin/python -m newsalert live --tickers AAPL,MSFT,NVDA --cycles 20   # small live run
.venv/bin/python -m newsalert live                           # all tickers, runs until stopped
.venv/bin/python -m newsalert fetch-history                  # ~29 days of 1-min bars (yfinance)
.venv/bin/python -m newsalert replay                         # writes docs/RESULTS.md
.venv/bin/python -m pytest                                   # offline test suite
```

`replay --source live` replays quotes that live mode recorded, instead of yfinance bars.

## Architecture

```
tickers.csv ─┐
config.yaml ─┤                ┌──────────── live.py ────────────┐
.env ────────┘  producer: round-robin tickers, SPY every 10th
                    │
                    ▼  asyncio.Queue → 4 workers
            RateLimiter (25/s, 55/min, pause on 429)
                    │
            FinnhubClient.quote ──fail──▶ skip ticker this cycle
                    │ ok, newer trade timestamp
                    ▼
            signals.Engine.on_price  ◀── same engine used by replay.py
                    │ Alert
                    ▼
            TelegramClient.send  ──▶  Store.insert_alert (latency recorded)
                    │
                    └─ background: Finnhub company-news + NewsAPI → Telegram reply, stored
```

| Module | Role |
|---|---|
| `newsalert/signals.py` | Pure alert logic (no I/O): threshold, MA filter, correlation filter, cooldown, staleness rules |
| `newsalert/live.py` | Scheduler, fetch workers, alert dispatch, news follow-ups |
| `newsalert/clients.py` | Finnhub, NewsAPI and Telegram clients; each takes an injected `httpx.AsyncClient` |
| `newsalert/ratelimit.py` | Multi-window async sliding-window limiter |
| `newsalert/store.py` | SQLite: alerts, live quotes, API usage counters, history bars |
| `newsalert/history.py` | yfinance 1-minute bar download |
| `newsalert/replay.py` | Replay, false-alert classification, latency percentiles, RESULTS.md |

### Alert logic

For each new price (details in `signals.py`):

1. **Threshold.** Compare with the price `move_window_min` (15) minutes ago. A candidate
   needs |move| ≥ 1.5%. If that reference is missing or more than 10 minutes staler
   than it should be (overnight gap, failed fetches), there is no alert.
2. **MA trend filter.** The 5-min SMA must be on the move's side of the 60-min SMA, and
   the 5-min SMA must itself have moved ≥ 0.5 × threshold between the reference time
   and now. This removes one-bar spikes, including a bad print that becomes the
   reference 15 minutes later. Known limit: a single print larger than about 3.75% still
   drags the 5-min SMA far enough to pass.
3. **Correlation filter.** Over the ticker's last 30 returns, compute corr and beta
   against SPY. If corr ≥ 0.6, subtract beta × SPY move from the move, and alert only if
   the residual still clears the threshold. This removes market-wide moves. If the SPY
   quote is stale (> 3 min), there is no alert this cycle.
4. **Cooldown.** One alert per ticker per 30 minutes.

### Failure handling

- A failed quote fetch (HTTP error, timeout, 429, Finnhub's all-zero reply for unknown
  symbols) skips that ticker for the cycle. Nothing reaches the engine, so no alert can
  use stale data. A later successful fetch is checked against the staleness rule above.
- A quote whose trade timestamp hasn't advanced (market closed) is ignored.
- A 429 pauses the shared limiter for `Retry-After` / `X-Ratelimit-Reset` (60 s default).
- If Telegram fails, the alert is still stored, with `delivered = 0`.
- Tests use `httpx.MockTransport`, and `tests/conftest.py` blocks all sockets, so any
  real network call fails the suite.

## Rate limits and scheduling

Checked 2026-09-27:

| API | Free-tier limit | Source | Confirmed? |
|---|---|---|---|
| Finnhub | 30 calls/second hard cap on every plan; HTTP 429 when exceeded | finnhub.io/docs/api/rate-limit (API spec JSON embedded in the page) | Yes |
| Finnhub | 60 calls/minute on the free plan | Third-party summaries (freeapi.watch, apicostcalc.com, GitHub issue); finnhub.io/pricing is JS-rendered and couldn't be read | **Not confirmed from Finnhub itself** |
| Finnhub | `/quote` and `/company-news` free; `/stock/candle` "Premium Access Required" | Finnhub API spec | Yes |
| NewsAPI | Developer plan: 100 requests/day, articles delayed 24 h, search up to 1 month back, development use only (not staging/production) | newsapi.org/pricing | Yes |

What this means for the design:

- **Prices:** polling `/quote` at 55/min with SPY every 10th call gives 50 ticker quotes
  a minute, so **each of the 503 tickers is refreshed roughly every 10 minutes**. That's
  the best the free tier allows; the 500-ticker design works, just at coarse cadence. A
  smaller `--tickers` list refreshes proportionally faster. Raise `calls_per_minute` in
  `config.yaml` on a paid plan.
- **Websocket:** Finnhub offers a trade websocket but its free symbol limit couldn't be
  confirmed from the docs, so live mode polls. Swapping in a websocket feed would only
  touch `live.py`.
- **News:** fetched only after an alert fires, never per cycle. Finnhub company-news
  shares the Finnhub limiter. NewsAPI is capped at 90/day by a counter persisted in
  SQLite (survives restarts). Its headlines are at least 24 h old on the free plan, and
  its licence covers development only.
- **History:** Finnhub candles are premium-only, so replay uses Yahoo 1-minute bars via
  yfinance. Yahoo keeps about 30 days of them, at most 8 days per request.

## Results

See [`docs/RESULTS.md`](docs/RESULTS.md). Summary:

Replay over 19 trading days (2026-08-31 to 2026-09-25), 503 tickers + SPY, 3.52M one-minute bars:

| | Alerts | False-alert rate (reverses > 50% within 30 min) |
|---|---:|---|
| Without filters | 2780 | 28.0% (95% CI 26.4–29.7%, n = 2715) |
| With MA + correlation filters | 2453 | 26.5% (95% CI 24.8–28.3%, n = 2402) |

- The filters reject about 12% of alerts, and the rejected ones are clearly worse: 39.6%
  false (CI 34.4–45.1%) against 26.5% for the ones kept. Because they remove so few, the
  overall rate only drops 1.5 points, and the two CIs overlap. The filter parameters were
  set before looking at this data and haven't been tuned.
- Replay latency (in-process, no network): p50 0.25 ms, p95 0.41 ms with filters.
- **End-to-end live latency (including Telegram) has not been measured yet.** It needs
  API keys and a live run during market hours; `replay` picks it up from `alerts.db`
  automatically once live alerts exist.

## Known limits

- **Live cadence at 500 tickers.** Each ticker is quoted about every 10 minutes, so:
  the 5-min fast SMA holds a single point, which makes the MA filter's "fast SMA moved"
  test nearly the same as the raw move; the 30-return correlation window spans about
  5 hours; and the 30-sample warm-up means no alerts for roughly the first 5 hours of a
  fresh process. On a small `--tickers` list, cadence is seconds and none of this applies.
- A single bad print larger than about 3.75% passes the MA filter (see *Alert logic*).
- NewsAPI free-plan headlines are at least 24 h old, and the plan's licence covers
  development only, not a deployed monitor.
