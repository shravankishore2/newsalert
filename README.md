# News Monitor (NSE)

Async market monitoring and news alerts for the Nifty 500. Every minute during the NSE
session it pulls last traded prices for all 500 stocks plus NIFTY 50 in one batched
DhanHQ request. It runs a threshold + moving-average + index-correlation alert filter,
stores every alert in SQLite with matching NSE announcements and BusinessLine headlines,
and shows them on a password-protected web dashboard that updates live. A replay mode
runs the same logic over Dhan 1-minute history and writes [`docs/RESULTS.md`](docs/RESULTS.md).
A demo mode drives the whole dashboard from replayed data, with no credentials needed.

The original US version (Finnhub, S&P 500) is the first commit, `3a097e7`. Its measured
results are kept in `docs/RESULTS.md`.

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv -e '.[dev]'
npm --prefix web install && npm --prefix web run build   # dashboard frontend (Node 20+)
cp .env.example .env   # then fill in the values below
```

`.env` (gitignored) needs:

| Variable | What it is |
|---|---|
| `DHAN_CLIENT_ID` | Your Dhan client ID |
| `DHAN_PIN` | Your 6-digit Dhan PIN |
| `DHAN_TOTP_SECRET` | The base32 secret shown when you enable TOTP in Dhan (the text form of the QR code) |
| `DHAN_ACCESS_TOKEN` | *Alternative* to PIN + TOTP: a token from web.dhan.co. Used until it expires (24 h) and not refreshed automatically |
| `DASHBOARD_PASSWORD` | Dashboard login (single user). Required for `serve`; `demo` generates and prints one if unset |

Dhan account prerequisites:

- **TOTP enabled** on the account (web.dhan.co → DhanHQ APIs → Setup TOTP).
- **Data API subscription active.** Dhan's trading APIs are free, but LTP and historical
  data are "Data APIs" with extra charges. Without it, both live mode and
  `fetch-history` fail.
- A static IP is **not** needed; Dhan requires one only for order-placement APIs.

## Usage

```bash
.venv/bin/python -m newsalert token                    # generate/check the Dhan token (TOTP)
.venv/bin/python -m newsalert demo                     # dashboard on replayed data (no credentials)
.venv/bin/python -m newsalert smoke-test               # token, LTP, 1-min history, feeds
.venv/bin/python -m newsalert live --tickers RELIANCE,TCS,INFY --minutes 30   # small live run
.venv/bin/python -m newsalert live                     # all 500; sleeps outside market hours
.venv/bin/python -m newsalert serve                    # dashboard for live mode (run beside `live`)
.venv/bin/python -m newsalert fetch-history            # 90 days of 1-min bars from Dhan
.venv/bin/python -m newsalert replay                   # updates the NSE section of docs/RESULTS.md
.venv/bin/python -m pytest                             # offline test suite
```

`build-universe <ind_nifty500list.csv>` rebuilds `tickers.csv` (see *Universe*).

## Dashboard

A FastAPI backend (`newsalert/web/`) serves the React (Vite) frontend in `web/` and a
JSON API. It only **reads** SQLite; live mode or the demo driver writes alerts and
status there. It listens on `127.0.0.1:8000` (`dashboard.host`/`port` in `config.yaml`).

What it shows:

- **Alert feed.** Ticker, company, sector, direction (▲/▼ with a label, never colour
  alone), move %, time, and *why the alert passed* each filter: the fast/slow SMA and
  how far the fast SMA moved, and the correlation, beta and index-adjusted move. New
  alerts appear at the top without a refresh.
- **Filters and search.** Ticker (with autocomplete), sector, direction, and a text
  search over past alerts (ticker, company name, news headlines), with "load older".
- **Alert detail.** The move, the price, NIFTY 50 over the same window, the filter
  reasons in plain language, and a chart of the stock and NIFTY 50 from an hour before
  the reference price. The chart shows % change on one shared axis, with a hover
  tooltip and a data-table view. Matching news shows **headline, source and link only**.
  Article text is never stored or shown, and links open on the publisher's site.
- **Status bar.** Market open/closed (next open when closed), the last price cycle and
  how many prices it got, Dhan token state and time left, news feed health (checked
  every 15 min during market hours, and on every alert), and the push connection.
- **Results page.** The false-alert tiles (rate, 95% CI, n) parsed from
  `docs/RESULTS.md`, with each market's full section rendered below.

### Demo mode

```bash
.venv/bin/python -m newsalert demo                 # picks NSE data if fetched, else the US replay
.venv/bin/python -m newsalert demo --dataset us --speed 120 --port 8001
```

- Replays stored 1-minute bars through the **real alert engine**, into a scratch
  `data/demo.db` that is rebuilt on each start.
- The first day replays instantly, so there's history to search. After that it runs
  at `--speed` market minutes per real minute (default 60×), with overnight gaps
  compressed to 3 s.
- **Clearly labelled:**
  - A "REPLAY DATA — Not live" banner with the dataset name, replay clock and progress
    stays on every page.
  - A REPLAY tag sits in the header.
  - Times say "(replayed)", and the status bar shows the token and news as "Not used".
- Replayed data has no archived news, so demo alerts have no news items, and the UI says
  so rather than showing invented headlines.
- **Datasets** (`demo.datasets` in `config.yaml`):
  - `nse` uses `data/history.db` from `fetch-history`.
  - `us` uses the US build's 19 days of Yahoo bars in `data/history_us.db`. That file is
    local-only and gitignored; Yahoo data isn't redistributed. A fresh clone has no
    demo data until `fetch-history` runs.
- If `DASHBOARD_PASSWORD` isn't set, demo prints a one-off password.

### Live mode

Run the monitor and the dashboard as two processes (they share `data/alerts.db`):

```bash
.venv/bin/python -m newsalert live      # terminal 1: polls Dhan during NSE hours
.venv/bin/python -m newsalert serve     # terminal 2: dashboard; needs DASHBOARD_PASSWORD
```

### Login

- **Single user.** The password comes from `DASHBOARD_PASSWORD` and is compared in
  constant time.
- **Session:** an HMAC-signed, HttpOnly, SameSite=Strict cookie (Secure over HTTPS)
  that expires after 12 h. It's signed with a random per-process key, so restarting
  the server logs you out.
- **Lockout:** 5 wrong passwords within 5 minutes lock logins for 60 s.
- Every data endpoint and the push stream return 401 without a valid session. Only the
  login page and `/api/health` are public.
- The server binds to localhost by default and warns if you bind it elsewhere.
- The news licences (see *News sources*) allow personal use only, so don't expose the
  dashboard publicly.

### Push channel

- **Server-sent events** at `/api/stream`. The server polls SQLite for new alert rows
  every `dashboard.push_poll_s` (1 s) and sends each as `event: alert` with its id.
  About every 5 s it also sends `event: status`, with keep-alive comments in between.
- **No alerts lost on reconnect.** Browsers reconnect by themselves and send
  `Last-Event-ID`, so the stream resumes after the last alert received.
- **Why SSE rather than WebSocket:** pushes only go server → browser, SSE works through
  the same login cookie, and polling SQLite lets `live` and `serve` run as separate
  processes.
- **Latency:** the stored `latency_ms` covers quote received → alert committed.
  Reaching the browser adds up to one poll interval.

### Frontend development

```bash
npm --prefix web run dev     # Vite on :5173, proxies /api to the Python server on :8000
npm --prefix web run lint
```

## Deployment (Oracle Cloud VM)

The production setup is an Ubuntu 24.04 VM with 1 GB RAM plus a 2 GB swap file. The
files are in [`deploy/`](deploy/).

| Unit | What it does |
|---|---|
| `newsalert-live.timer` | Fires at **09:10 IST, Monday–Friday** (`Persistent=true`, so a missed start runs at boot) |
| `newsalert-live.service` | `ExecCondition=is-trading-window --until 15:35` skips NSE holidays and late starts without marking a failure. `ExecStartPre=token` makes sure the Dhan token lasts the session. `live --until 15:35` stops the monitor, with `RuntimeMaxSec=6h30min` as a backstop |
| `newsalert-web.service` | Always-on dashboard over live data, on `127.0.0.1:8000` |
| `newsalert-demo.service` | Always-on demo (replayed NSE bars, labelled as replay) on `127.0.0.1:8001` |
| Caddy (`deploy/Caddyfile`) | HTTPS through Let's Encrypt. The live dashboard is at `https://<ip-with-dashes>.sslip.io`, the demo at `https://demo.<ip-with-dashes>.sslip.io`. sslip.io resolves those names to the IP, so no DNS is needed. SSE is streamed unbuffered (`flush_interval -1`) |

Setup outline, in the order used:

1. Add a read-only GitHub deploy key on the VM, then clone to `~/newsalert`.
2. Install `python3.12-venv`, create `.venv`, and run `pip install -e '.[dev]'`.
3. Install Node 22 from the official, checksum-verified tarball into `~/.local/node`, then
   `npm ci && npm run build` in `web/`.
4. Copy `.env` with `scp`, then `chmod 600`.
5. Open ports 80/443 in iptables (inserted before Oracle's default REJECT rule, then
   `netfilter-persistent save`) **and** in the Oracle VCN security list; the console
   step is below.
6. Install the units, run `systemctl enable --now newsalert-web newsalert-live.timer
   newsalert-demo`, and install the Caddyfile with the host filled in.

**Oracle console step.** The VM's firewall isn't enough; Oracle also filters at the
subnet. Go to Networking → Virtual Cloud Networks → your VCN → the instance's subnet →
Security List. Add two ingress rules: source `0.0.0.0/0`, TCP, destination ports `80`
and `443`. Port 80 is needed for Let's Encrypt's HTTP challenge and the redirect to
HTTPS.

Operations:

```bash
systemctl list-timers newsalert-live.timer            # next run
journalctl -u newsalert-live -f                       # today's session
.venv/bin/python -m newsalert is-trading-window       # would today's run start?
cd ~/newsalert && git pull && .venv/bin/pip install -e . && npm --prefix web ci && npm --prefix web run build && sudo systemctl restart newsalert-web newsalert-demo
```

### Can a password-protected demo show Dhan data?

**Unconfirmed.** As of 2026-09-27, neither Dhan's terms (`dhan.co/terms`) nor the DhanHQ
v2 docs have a clause specifically about data fetched through the APIs. The general
terms say: "Except as expressly authorized by Dhan Platform, You agree not to sell,
license, distribute, copy, modify, publicly perform or display, transmit, publish…
the materials." That rules out public display and redistribution. It doesn't clearly
say whether a single-user, password-protected dashboard that shows the subscriber
their own Data API results counts. NSE's own market-data licensing may also apply.

Until Dhan confirms in writing:

- Treat the dashboard, demo included, as **personal use only**.
- Don't share the password or make the demo public.
- Ask Dhan support (dhan.co/support) before showing it to anyone else.

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
   Store.insert_alert (filter reasons + latency) ──▶ SQLite ◀── dashboard (FastAPI) ──SSE──▶ browser
        │
        └─ background: NSE announcements RSS + BusinessLine RSS (cached 5 min) ─▶ alert's news
```

| Module | Role |
|---|---|
| `newsalert/signals.py` | Pure alert logic: threshold, MA filter, correlation filter, cooldown, staleness rules. Unchanged from the US build |
| `newsalert/live.py` | Market-hours scheduler, pre-open token refresh, batched cycle, alert storage, news lookup, status for the dashboard |
| `newsalert/web/` | FastAPI dashboard API, password login, SSE push channel, static frontend |
| `newsalert/demo.py` | Demo driver: replays stored bars through the engine at a chosen speed |
| `web/` | React + Vite frontend (feed, detail, status bar, results) |
| `newsalert/auth.py` | TOTP (RFC 6238), Dhan token generation, private token cache, log redaction |
| `newsalert/market.py` | NSE calendar: IST session, weekends, trading holidays |
| `newsalert/clients.py` | Dhan (LTP, intraday), RSS feeds and news matching. All take an injected `httpx.AsyncClient` |
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

News is fetched when an alert fires, plus a health check at most every 15 minutes during
market hours. Each feed is fetched at most once per 5 minutes, with a
`newsalert/0.2 (personal, non-commercial)` User-Agent. Only headline, source, link and
time are kept, and they're shown only on your password-protected dashboard.

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

Both markets use the same false-alert definition (price gives back more than half the
move within 30 minutes) and the same filter settings; nothing was retuned for NSE.

| Market | Data | Without filters | With MA + correlation filters |
|---|---|---|---|
| **NSE** | 63 trading days (2026-06-30 to 2026-09-25), 500 stocks + NIFTY 50, 11.7M Dhan 1-min bars | 26.1% (95% CI 25.3–27.0%, n = 9,555; 10,120 alerts) | 25.0% (95% CI 24.1–26.0%, n = 8,190; 8,631 alerts) |
| **US** (original build) | 19 trading days (2026-08-31 to 2026-09-25), 503 stocks + SPY, 3.52M Yahoo 1-min bars | 28.0% (95% CI 26.4–29.7%, n = 2,715; 2,780 alerts) | 26.5% (95% CI 24.8–28.3%, n = 2,402; 2,453 alerts) |

In both markets the filters reject roughly 12–14% of alerts, and those are clearly
worse. On NSE, rejected alerts were 32.7% false (CI 30.2–35.2%) against 25.0% for kept
ones; the US figures were 39.6% vs 26.5%. Because they remove so few, the overall
false-alert rate drops only about 1–1.5 points, and the two confidence intervals
overlap in both markets.

The NSE data has a known gap. From 2026-08-03, about 210 stocks' Dhan histories stop
at 15:14 instead of 15:29 (26% of stock-days overall). Alerts whose 30-minute follow-up
falls into that gap are left out of the rate, so late-session alerts are
under-represented. Whether they behave differently wasn't measured. RESULTS.md states
this, along with the 7,914 out-of-hours bars dropped.

## Known limits

- A single bad print larger than about 3.75% passes the MA filter.
- The 30-sample warm-up means the first ~30 minutes after a fresh start produce no
  alerts. At a 60 s cycle that's half an hour; it doesn't carry over between restarts.
- Holiday list is static and needs a yearly update; Muhurat sessions are not modelled.
- Dhan intraday timestamps are true UTC epoch seconds. This was checked on real data:
  bars start at 09:15 IST. `fetch-history` still checks that bars fall inside
  09:15–15:30 IST and exits non-zero if they don't.
- Dhan's stock history misses the last 15 minutes of the session for about 210 stocks
  from 2026-08-03 on, and has a few bars stamped after the close. Replay and demo drop
  out-of-session bars, the same as live mode.
