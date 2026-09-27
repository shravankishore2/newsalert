# News Monitor (NSE)

Async market monitoring and news alerts for the Nifty 500. Every minute during the NSE
session it pulls last traded prices for all 500 stocks plus NIFTY 50 in one batched
DhanHQ request. It runs a threshold + moving-average + index-correlation alert filter,
sends alerts to Telegram with matching NSE announcements and BusinessLine headlines,
and stores every alert in SQLite. A replay mode runs the same logic over Dhan 1-minute
history and writes [`docs/RESULTS.md`](docs/RESULTS.md).

The original US version (Finnhub, S&P 500) is the first commit, `3a097e7`. Its measured
results are kept in `docs/RESULTS.md`.

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv -e '.[dev]'
cp .env.example .env   # then fill in the values below
```

`.env` (gitignored) needs:

| Variable | What it is |
|---|---|
| `DHAN_CLIENT_ID` | Your Dhan client ID |
| `DHAN_PIN` | Your 6-digit Dhan PIN |
| `DHAN_TOTP_SECRET` | The base32 secret shown when you enable TOTP in Dhan (the text form of the QR code) |
| `DHAN_ACCESS_TOKEN` | *Alternative* to PIN + TOTP: a token from web.dhan.co. Used until it expires (24 h) and not refreshed automatically |
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `TELEGRAM_CHAT_ID` | Send the bot a message, then read `getUpdates` |

Dhan account prerequisites:

- **TOTP enabled** on the account (web.dhan.co → DhanHQ APIs → Setup TOTP).
- **Data API subscription active.** Dhan's trading APIs are free, but LTP and historical
  data are "Data APIs" with extra charges. Without it, both live mode and
  `fetch-history` fail.
- A static IP is **not** needed; Dhan requires one only for order-placement APIs.

## Usage

```bash
.venv/bin/python -m newsalert token                    # generate/check the Dhan token (TOTP)
.venv/bin/python -m newsalert smoke-test               # token, LTP, 1-min history, feeds, Telegram
.venv/bin/python -m newsalert live --tickers RELIANCE,TCS,INFY --minutes 30   # small live run
.venv/bin/python -m newsalert live                     # all 500; sleeps outside market hours
.venv/bin/python -m newsalert fetch-history            # 90 days of 1-min bars from Dhan
.venv/bin/python -m newsalert replay                   # updates the NSE section of docs/RESULTS.md
.venv/bin/python -m pytest                             # offline test suite
```

`build-universe <ind_nifty500list.csv>` rebuilds `tickers.csv` (see *Universe*).

## Universe

`tickers.csv` holds the **500 Nifty 500 constituents**. Columns: symbol, name, industry,
ISIN, Dhan security ID.

- **Source:** NSE's official index file `ind_nifty500list.csv`, from
  `https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv`. The same file is
  published by NSE Indices at niftyindices.com. Downloaded **2026-09-27**; SHA-256
  `c043bdc21e6080f119a86eb28cdc9b3a6009d3baffa9b5d35e9ca41b195b4501`.
- The file lists 501 rows. `DUMMYHEG` ("Dummy HEG Ltd.", ISIN `DUM545A01024`) is a
  placeholder NSE uses around corporate actions. It has no tradable instrument and is
  excluded.
- Security IDs come from Dhan's public scrip master
  (`images.dhan.co/api-data/api-scrip-master-detailed.csv`), matched on ISIN for NSE
  cash-segment equities. All 500 matched.
- The index constituents change at NSE's semi-annual reviews. To refresh, download the
  CSV by hand (NSE's terms forbid automated collection, see below) and run
  `build-universe`.

## Architecture

```
config.yaml / .env / tickers.csv
        │
   live.py scheduler ── market closed? ──▶ sleep to 08:45 IST ─▶ refresh token ─▶ sleep to 09:15
        │ market open (09:15–15:30 IST, trading day)
        ▼ every 60 s
   DhanClient.ltp: ONE POST /v2/marketfeed/ltp {"IDX_I":[13], "NSE_EQ":[…500 ids]}
        │  request failed ──▶ skip whole cycle     ticker missing from reply ──▶ skip that ticker
        ▼
   signals.Engine.on_price (NIFTY 50 first, then each stock)   ◀── same engine as replay.py
        │ Alert
        ▼
   TelegramClient.send ──▶ Store.insert_alert (latency recorded)
        │
        └─ background: NSE announcements RSS + BusinessLine RSS (cached 5 min) ─▶ Telegram reply
```

| Module | Role |
|---|---|
| `newsalert/signals.py` | Pure alert logic: threshold, MA filter, correlation filter, cooldown, staleness rules. Unchanged from the US build |
| `newsalert/live.py` | Market-hours scheduler, pre-open token refresh, batched cycle, alert dispatch, news replies |
| `newsalert/auth.py` | TOTP (RFC 6238), Dhan token generation, private token cache, log redaction |
| `newsalert/market.py` | NSE calendar: IST session, weekends, trading holidays |
| `newsalert/clients.py` | Dhan (LTP, intraday), RSS feeds and news matching, Telegram. All take an injected `httpx.AsyncClient` |
| `newsalert/history.py` | Dhan 1-minute history download with ≤90-day windows and a timestamp sanity check |
| `newsalert/replay.py` | Replay, false-alert classification, latency percentiles, per-market RESULTS sections |
| `newsalert/universe.py` | Nifty 500 CSV + Dhan scrip master → `tickers.csv` |
| `newsalert/ratelimit.py`, `store.py` | Multi-window async limiter; SQLite |

### Alert logic

Unchanged from the US build. Same settings, not retuned. For each new price:

1. **Threshold.** Compare with the price 15 minutes ago. A candidate needs |move| ≥ 1.5%.
   If that reference is missing or stale (overnight gap, failed fetches), there is no alert.
2. **MA trend filter.** The 5-min SMA must be on the move's side of the 60-min SMA, and
   the 5-min SMA must itself have moved ≥ 0.5 × threshold since the reference time. This
   removes one-bar spikes. Known limit: a single print larger than about 3.75% still passes.
3. **Correlation filter.** Over the stock's last 30 returns, compute corr and beta
   against **NIFTY 50**. If corr ≥ 0.6, subtract beta × NIFTY move, and alert only if the
   residual still clears the threshold. If NIFTY data is stale (> 3 min), there is no
   alert this cycle.
4. **Cooldown.** One decided candidate per stock per 30 minutes. A filter rejection
   counts, so a rejected move can't fire a few minutes later.

## Rate limits and scheduling (Dhan)

From dhanhq.co/docs/v2, checked **2026-09-27**:

| API group | Limit | Used for |
|---|---|---|
| Quote APIs | **1 request/s**; LTP takes **up to 1000 instruments per request** | Live prices |
| Data APIs | **5 requests/s, 100,000/day**; intraday history **≤ 90 days per request**, 1/5/15/25/60-min bars, **5 years back** | Replay history |
| Non-trading APIs | 20 requests/s | (not used) |
| Rate-limit error | HTTP 429 / `DH-904` | Pauses the limiter |

What this means for the design:

- **Every ticker, every cycle.** 500 stocks + NIFTY 50 = 501 instruments fit in one LTP
  request, so a full sweep costs one call. The Quote API would allow a cycle every
  second. Live mode uses **60 s** so its samples match the 1-minute bars the filters were
  measured on in replay. With a faster cycle, "last 30 returns" would cover 30 seconds
  instead of 30 minutes and the filters would behave differently from what was measured.
  `dhan.cycle_s` changes this. Batches above 1000 instruments are split and spaced 1 s apart.
- **LTP has no trade timestamp.** Samples are stamped when the reply arrives. A halted
  stock looks like an unchanged price (no alert), never a stale jump.
- **History:** 501 instruments × one 90-day window = 501 requests. The limiter runs at
  4/s and ≤ 90,000/day (headroom under 5/s and 100k/day).
- **Failures:** a failed LTP request skips the cycle. A stock missing from the reply is
  skipped for that cycle. A 429/`DH-904` pauses the limiter. A token error
  (`DH-901`/`807`/`809`/HTTP 401) drops the token so the next request regenerates it.
  None of these feeds the engine, so no alert uses stale data.

## Dhan authentication

- Dhan access tokens expire **24 hours** after generation. The monitor generates them
  itself with Dhan's TOTP endpoint:
  `POST https://auth.dhan.co/app/generateAccessToken?dhanClientId=…&pin=…&totp=…`.
  The 6-digit TOTP is computed locally (RFC 6238, SHA-1, 30 s) from `DHAN_TOTP_SECRET`.
- **When it refreshes:**
  - Automatically **30 minutes before the open** (08:45 IST) on trading days, if the
    current token wouldn't last until after the close. Holidays and weekends are skipped.
  - Also on any request where fewer than 2 hours remain.
  - Also right after Dhan rejects a token.
- **Token cache:** `data/dhan_token.json` (gitignored, mode `600`), so restarts don't
  generate a new token each time. It's tied to the client ID.
- **Secrets hygiene:**
  - Client ID, PIN, TOTP secret, the one-time TOTP code and the access token are never
    logged or committed.
  - Error messages are built without the request URL, which carries the PIN and TOTP.
  - httpx/httpcore logging is capped at WARNING.
  - A logging filter masks those values in any log line as a backstop.
  - `Secrets` prints as set/unset only.
  - Tests check each of these.
- Dhan's `RenewToken` endpoint only works for tokens generated on Dhan Web, so the TOTP
  endpoint is used instead.

## Market hours and holidays

- Session **09:15–15:30 IST**, Monday–Friday, minus NSE trading holidays.
- The 2026 equity-segment (CM) holiday list is in `config.yaml`, copied from NSE's
  holiday calendar on 2026-09-27. It's stored as static config because NSE's terms
  forbid automated collection from its website (below).
- **Update it once a year.** A year missing from the list logs a warning and treats every
  weekday as a trading day.
- Special sessions (Diwali Muhurat trading) aren't modelled.

## News sources

News is fetched only when an alert fires, each feed at most once per 5 minutes, with a
`newsalert/0.2 (personal, non-commercial)` User-Agent. Headlines go only to your own
Telegram chat.

Terms reviewed **2026-09-27**. robots.txt allows every feed URL below for a generic bot,
but the terms of use are stricter, so they decided the selection:

| Source | Used? | What the terms say |
|---|---|---|
| NSE corporate announcements, official RSS `nsearchives.nseindia.com/content/RSS/Online_announcements.xml` | **Yes** | NSE's website terms (`nseindia.com/static/nse-terms-of-use`): "User is prohibited to conduct any systematic or automated data collection activities (including scraping, data mining, data extraction and data harvesting) on or in relation to our Website / Mobile Application." NSE also publishes these RSS feeds for subscription ("RSS subscriptions enable users to automatically receive content"), so reading the official feed is treated as allowed. The nseindia.com JSON API (`/api/corporate-announcements`) is **not** used. You approved this interpretation. |
| The Hindu BusinessLine RSS (markets, companies) | **Yes** | "offers RSS feeds for personal and non-commercial use by the user." Site licence: "personal use of this site but not for commercial purposes." **Restricts commercial use.** |
| Economic Times RSS | No | "ET grants you permission to only access and make personal use of its RSS feeds and you agree not to, directly or indirectly, download, … copy, publish, distribute…" and "TIL forbids … aggregating … TIL's RSS feeds". Aggregating feeds is exactly what this monitor does. |
| Business Standard | No | Without prior written consent, you may not "use robots, spiders, scripts … or otherwise use, access, or collect the Content … using automated means", and its "robots.txt notice does not constitute … prior written consent". **Forbids automated access.** |
| Mint (livemint.com) | No | Terms cover RSS feeds: "you must not use robots, spiders, crawlers, scrapers … Automated scraping/crawling/bulk downloading is prohibited." **Forbids automated access.** |
| Moneycontrol | No | Terms page (`moneycontrol.com/terms-use/`) returned HTTP 503 (bot wall) and couldn't be read. Left out rather than assumed. |

News matching: NSE items carry the NSE symbol in their link (`/corporate/SYMBOL_…`), so
they match exactly. BusinessLine headlines match on the company name with legal suffixes
removed ("Reliance Industries Ltd." → "Reliance Industries"), on word boundaries.

## Results

See [`docs/RESULTS.md`](docs/RESULTS.md).

- **NSE: not run yet.** It needs Dhan credentials and a Data API subscription; see
  *Setup*. The NSE section of RESULTS.md says so and has no numbers.
- **US** (kept from the original build): replay over 19 trading days (2026-08-31 to
  2026-09-25), 503 tickers + SPY, 3.52M one-minute bars:

| | Alerts | False-alert rate (reverses > 50% within 30 min) |
|---|---:|---|
| Without filters | 2780 | 28.0% (95% CI 26.4–29.7%, n = 2715) |
| With MA + correlation filters | 2453 | 26.5% (95% CI 24.8–28.3%, n = 2402) |

## Known limits

- A single bad print larger than about 3.75% passes the MA filter.
- The 30-sample warm-up means the first ~30 minutes after a fresh start produce no
  alerts. At a 60 s cycle that's half an hour; it doesn't carry over between restarts.
- Holiday list is static and needs a yearly update; Muhurat sessions are not modelled.
- The Dhan timestamp convention for intraday bars ("Epoch timestamp" in the docs) hasn't
  been checked against real data yet. `fetch-history` checks that bars fall inside
  09:15–15:30 IST and exits non-zero if they don't.
