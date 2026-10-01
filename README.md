# QuantRadar

News-first market alerts for NSE (Nifty 500). The code and GitHub repo are named `newsalert`;
the app is QuantRadar.

News-first market alerts for the Nifty 500.

- **News drives alerts.** A 24/7 service reads NSE corporate announcements and
  BusinessLine RSS, classifies each item (event type, affected stocks, expected
  direction and reason), and raises a news alert that is pushed live to a
  password-protected dashboard.
- **Prices evaluate the news.** Every minute during the NSE session, one batched DhanHQ
  request fetches all 500 stocks plus NIFTY 50. An event study scores each news alert's
  calls against NIFTY-relative returns.
- **Price-move alerts are a secondary layer.** The threshold + moving-average +
  index-correlation filter still runs. A price move within 60 minutes after a news alert
  on the same stock is linked to that alert.
- **Replay and demo.** Replay mode measures the price logic over Dhan 1-minute history.
  Demo mode replays archived news and prices together.
  Results are in [`docs/RESULTS.md`](docs/RESULTS.md).

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
| `DHAN_TOKEN_AUTHORITY` | `1` on the one machine that generates tokens (the VM). Anywhere else, generation refuses unless the command line has `--i-know-this-kills-the-vm-token` — Dhan keeps one token per account, so a second generator kills the first's token |
| `DHAN_ACCESS_TOKEN` | *Alternative* to PIN + TOTP: a token from web.dhan.co. Used until it expires (24 h) and not refreshed automatically |
| `DASHBOARD_PASSWORD` | Dashboard login (single user). Required for `serve`; `demo` generates and prints one if unset |
| `GEMINI_API_KEY` | Classifies BusinessLine headlines (aistudio.google.com/apikey). Without it, BusinessLine items stay pending; NSE filings are still classified by rules |
| `FINNHUB_API_KEY` | *Optional.* Earnings expectations for the Results board (free, personal-use terms). Without it the board shows stated figures or the stock reaction |

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
.venv/bin/python -m newsalert news                     # 24/7 news ingest + classification + news alerts
.venv/bin/python -m newsalert evaluate-news            # event study; updates the news section of RESULTS.md
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

Three tabs: **Alerts**, **Results**, **Model performance**.

**Alerts: the news feed, in columns.**

- **Positive** (green) and **Negative** (red) sit side by side, with a slimmer,
  collapsible **Neutral / watch** column for alerts with no direction. Each column shows
  its count, and cards have a tinted background and border, readable in dark mode too.
  The feed uses the full screen width. On phones the columns become tabs (Positive ·
  Negative · Neutral, with counts).
- **Column placement:** an alert goes into a column by the direction of its **headline
  company**, meaning the first "direct" stock, or the first stock if there's none.
- **Every card has the same anatomy:** the event badge and the impact badge (▲ Up /
  ▼ Down / • Neutral, plus strength) side by side at the top, then the company as
  "Full name · TICKER", the headline, and a summary clamped to two lines (click "Show
  more" to expand). The summary is the BusinessLine feed summary, or else Gemini's
  reason. NSE filings show a label only when it adds information beyond the badge,
  e.g. "Bonus issue".
- **A small footer holds the metadata:** time, source, "alerted X after publication",
  and "rule-based" (NSE) or "N% confidence" (Gemini), plus any linked price moves.
- **Duplicates are threaded.** Alerts for the same headline company and event type
  within 60 minutes become one card (newest on top) with "N updates · show all".
- **Chips appear only for the other affected stocks** (competitors, suppliers,
  customers, sector peers), never repeating the headline company.
- **Navigation** goes through the app's hash router: the headline covers the whole
  card as one link, while buttons inside the card stay clickable.
- **Layers:** **News**, **News + price moves** (price moves drop into the matching
  column as smaller cards) or **Price moves** only (the filterable price-alert feed).
  The switch is labelled **View**, and the page title follows it. Filters sit in one
  row of equal-height controls that wraps on phones: a single search (ticker, company
  or headline), sector, event type (and direction on the price view). The view and the
  Neutral column's collapsed state are remembered.
- **Theme:** the dashboard opens in a white light theme whatever the device prefers; the
  header's Light/Dark button switches it and each browser remembers its choice. Cards
  use a soft shadow and a 4px coloured left edge rather than a heavy outline, impact
  badges are tinted pills, and hover/expand states ease in (off under reduced motion).
- **Contrast** is computed, not eyeballed: `web/scripts/contrast.py` checks every text
  pair at WCAG AA 4.5:1 and the cards' coloured edges against the page at 3:1 in both themes, and
  `tests/test_contrast.py` fails the suite if any pair drops below.
- **News detail** and **price-alert detail** pages are unchanged: classification and
  reasons, the event study once the session closes, linked moves, a chart vs NIFTY 50,
  and headline, source and link only.

**Results: a company events board.**

- **Earnings results.** One card per company that reported (merging its BusinessLine
  headline and NSE filing), with:
  - A short briefing.
  - A speedometer gauge from strong miss to strong beat.
  - The stock's reaction vs NIFTY 50 at +15 min, +1 h and close. During the session
    these are computed live and marked "so far"; after the event study runs they're
    final.
  - A **Basis** line on every card saying exactly what the gauge shows:
    1. **Expected vs actual:** EPS and revenue estimates from Finnhub's free earnings
       calendar, with the surprise % and a verdict. Only when `FINNHUB_API_KEY` is set
       (see *Earnings data* below).
    2. **Actual vs same quarter last year:** profit, revenue and EPS as **stated in the
       BusinessLine headline or summary**, extracted by Gemini only when written there
       (growth figures not found in the text are dropped). The gauge then shows the
       stock reaction and says so.
    3. **Stock reaction only:** when neither is available. The gauge is labelled
       "Stock reaction vs NIFTY 50 (not an earnings verdict)".
  - **Expectations are never invented.** When none are configured, a notice at the top
    explains why.
- **Corporate actions.** Dividends, bonus issues, splits, buybacks and record dates from
  NSE filings (last 30 days), classified by rules, with upcoming dates first. A date is
  shown only when the filing states one; only that date is stored, not NSE's text. Debt
  housekeeping (commercial paper, NCD interest) is excluded.

**Model performance.** The previous Results page, unchanged: the news event study and
the price-alert replay results (false-alert rates, CIs) from `docs/RESULTS.md`.

**Status bar.** Market open/closed is shown prominently. Everything else sits behind
one pill ("System OK", or "System: N warnings/problems"), which opens a popover with:
- The last price cycle.
- The Dhan token state.
- The news service: last poll, feeds, alerts, pending items, Gemini's usage.
- The push connection.

### Earnings data: what's allowed (checked 2026-09-28)

Analyst consensus estimates are usually paid data. What was found:

| Source | Estimates for Nifty 500? | Terms / cost |
|---|---|---|
| **Finnhub**, `/calendar/earnings?international=true` | EPS and revenue estimate + actual. Not marked premium in Finnhub's API spec (free tier: "1 month of historical earnings and new updates"). Whether Indian symbols are returned on the free tier is **unverified** until a key is configured | Free; "strictly for personal use", no sharing of "data or derived results" with third parties. A single-user, password-protected dashboard fits |
| Finnhub `/stock/eps-estimate`, `/stock/revenue-estimate` | Yes | "Premium Access Required" |
| Twelve Data free (Basic) | 3 markets only | Licence: "internally for testing, evaluation, or development purposes only". Not permitted |
| Alpha Vantage, Financial Modeling Prep | Not assessed: their sites couldn't be reached from this machine on 2026-09-28 | — |

So QuantRadar supports Finnhub **optionally**:
- **Setup:** set `FINNHUB_API_KEY` (free signup at finnhub.io) and restart the news
  service. When a results alert fires, the service looks up that company
  (`SYMBOL.NS`) in the earnings calendar, rate-limited to 30/min (the free tier allows
  60).
- **Without a key:** cards fall back to stated year-on-year figures, then to the
  reaction, as described above.
- **Don't share the dashboard password** if expectations are on: Finnhub's terms
  forbid sharing derived results.

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
- **News:** with the NSE dataset, demo replays **archived news alerts together with
  prices**. The archive is the news service's store in `data/alerts.db`.
  - It replays only days where both exist, plus one warm-up day before.
  - Each news alert appears when the replay clock reaches its original alert time. News
    from outside market hours appears at the next replayed open.
  - Replayed price moves link to it exactly as in live mode.
  - Days without archived news replay prices only, and the status bar says so.
  - The NSE announcements feed empties at midnight IST, so the archive starts on the
    day the news service first runs; nothing earlier can be recovered.
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

## News pipeline

`newsalert news` runs 24/7 as `newsalert-news.service`:

1. **Ingest.** It polls each feed every 3 minutes with conditional GET (ETag /
   If-Modified-Since), so an unchanged feed costs one small `304`. Every item is stored
   once, with source, link, published time and fetched time.
   - **BusinessLine:** the headline and the RSS summary. Never article text.
   - **NSE:** only the link, the symbol (from the filing link, or else from the item's
     company-name title), a derived label and the times, because NSE's terms forbid
     storing its content.
   - Items already older than 2 hours when first seen are archived but don't alert and
     aren't sent to Gemini. Otherwise the first poll would flood alerts with stale news.
2. **Classify.** Each item gets an event type from a fixed list: results, guidance,
   merger/acquisition, order/contract win, rating change, regulatory action,
   fraud/legal, management change, capital raise, dividend/buyback, other. It also gets
   its affected stocks (ticker, relation, direction, strength, reason) and an overall
   confidence.
   - **NSE filings: local rules, no LLM.** The filing subject maps to an event type,
     and the only affected stock is the announcing company. Confidence is fixed at 0.5.
     - **Routine filings are "other" and don't alert:** trading window, AGM
       proceedings, analyst-meet schedules, staff share-option allotments, and
       commercial-paper or NCD housekeeping.
     - **Directions are given only where the subject makes them clear.** Refined on
       2026-09-28 against that day's filings, which were analysed in memory and not
       stored:
       - Up: order and contract wins (press releases included), buybacks, bonus issues,
         splits, dividends, and credit rating upgrades or positive outlooks.
       - Down: fraud, default or insolvency; regulatory actions, penalties and exchange
         fines; QIP, preferential or rights issues (dilution); CEO, CFO or auditor
         resignations; and rating downgrades or negative outlooks.
       - Left as no direction: results, acquisitions, other appointments or
         resignations, and reaffirmed ratings.
     - **Corporate actions:** the kind (dividend, bonus, split, buyback, record date)
       and the date the filing states are kept as derived fields for the Results board.
   - **BusinessLine: Gemini** (`gemini-3.5-flash-lite`, structured JSON output).
     - **Validation:** Pydantic checks every reply; tickers not in `tickers.csv` are
       dropped and recorded.
     - **Invalid JSON:** retried once for that item alone; after that the item is
       stored as `unclassified`.
     - **No double classification:** results are cached by headline+summary hash.
   - **Free-tier limits:** Google no longer publishes them (they're per project, in AI
     Studio). The service stays inside conservative caps: 5 requests/min, 100 per
     Pacific-time day (when Google resets) and 10 headlines per request. On an HTTP 429
     it pauses for Google's `retryDelay`; the quota Google reports shows in the status
     bar.
3. **Alert.** A classified item whose event type isn't "other" and which has at least
   one affected stock in the universe becomes a **news alert**, pushed to the dashboard
   over SSE (`event: news`). The live monitor links any price-move alert on that stock
   in the next 60 minutes.
4. **Evaluate.** `newsalert evaluate-news` runs at 15:50 IST on weekdays
   (`newsalert-evaluate.timer`). The method is in RESULTS.md:
   - **Start:** each affected stock's return vs NIFTY 50 is measured from the alert
     time, or from the next open for news outside market hours.
   - **Horizons:** +15 min, +1 h and the close, using the live 60-second LTP samples.
   - **Scoring:** hit rates by event type and by relation (direct vs second-order),
     against a random-direction baseline of 50% (exact binomial test, Wilson 95% CI).
   - **Latency:** publication to alert.
   - **Small samples:** groups under 30 are marked too few.

## Deployment (Oracle Cloud VM)

The production setup is an Ubuntu 24.04 VM with 1 GB RAM plus a 2 GB swap file. The
files are in [`deploy/`](deploy/).

| Unit | What it does |
|---|---|
| `newsalert-live.timer` | Fires at **09:10 IST, Monday–Friday** (`Persistent=true`, so a missed start runs at boot) |
| `newsalert-live.service` | `ExecCondition=is-trading-window --until 15:35` skips NSE holidays and late starts without marking a failure. `ExecStartPre=token` makes sure the Dhan token lasts the session. `live --until 15:35` stops the monitor (no fixed runtime limit: the monitor also starts at boot, so on 2026-09-28 a 6.5 h limit killed it mid-session). On any start it replays today's stored quotes into the engine, so a restart doesn't lose the 30-sample warm-up |
| `newsalert-news.service` | Always on: news ingest, classification and news alerts (see *News pipeline*) |
| `newsalert-evaluate.timer` | 15:50 IST on weekdays: event study, rewrites the news section of `docs/RESULTS.md` |
| `newsalert-web.service` | Always-on dashboard over live data, on `127.0.0.1:8000` |
| `newsalert-demo.service` | Always-on demo (replayed NSE bars, labelled as replay) on `127.0.0.1:8001` |
| Caddy (`deploy/Caddyfile`) | HTTPS through Let's Encrypt. QuantRadar has its own subdomain so other apps can share the VM: live at `https://quantradar.<ip-with-dashes>.sslip.io` (now https://quantradar.68-233-96-25.sslip.io), demo at `https://demo.quantradar.<ip-with-dashes>.sslip.io` (now https://demo.quantradar.68-233-96-25.sslip.io). The old addresses (`https://<ip-with-dashes>.sslip.io`, `https://demo.<ip-with-dashes>.sslip.io`) 302-redirect there, keeping the path, query and `#/` route; a login doesn't carry across hosts, so log in once on the new address. sslip.io resolves any of these names to the IP, so no DNS is needed. The VM's `/etc/caddy/Caddyfile` is this file with the host filled in, plus other apps' blocks appended. SSE is streamed unbuffered (`flush_interval -1`) |

Setup outline, in the order used:

1. Add a read-only GitHub deploy key on the VM, then clone to `~/newsalert`.
2. Install `python3.12-venv`, create `.venv`, and run `pip install -e '.[dev]'`.
3. Install Node 22 from the official, checksum-verified tarball into `~/.local/node`, then
   `npm ci && npm run build` in `web/`.
4. Copy `.env` with `scp`, then `chmod 600`.
5. Open ports 80/443 in iptables (inserted before Oracle's default REJECT rule, then
   `netfilter-persistent save`) **and** in the Oracle VCN security list; the console
   step is below.
6. Install the units, run `systemctl enable --now newsalert-web newsalert-news
   newsalert-live.timer newsalert-evaluate.timer newsalert-demo`, and install the
   Caddyfile with the host filled in.

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
cd ~/newsalert && git pull --rebase --autostash && .venv/bin/pip install -e . && npm --prefix web ci && npm --prefix web run build && sudo systemctl restart newsalert-web newsalert-news newsalert-demo
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
| `newsalert/clients.py` | Dhan (LTP, intraday) and an RSS reader for the smoke test. All take an injected `httpx.AsyncClient` |
| `newsalert/news/` | News-first pipeline: `ingest.py` (feeds, storage, alerts), `rules.py` (NSE rule classifier, corporate actions), `gemini.py` (BusinessLine classifier, stated results figures, quota), `expectations.py` (optional Finnhub earnings expectations), `models.py` (Pydantic schemas), `evaluate.py` (event study, RESULTS section) |
| `newsalert/web/board.py` | Results board (earnings cards, gauge basis) and corporate-actions board |
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

The news service polls each feed every 3 minutes, around the clock, using conditional
GET so unchanged feeds cost a `304`. It sends a `newsalert/0.3 (personal, non-commercial)`
User-Agent. Results are shown only on your password-protected dashboard.

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

### Sending news to an LLM (reviewed 2026-09-28)

| Source | Sent to Gemini? | Stored | Why |
|---|---|---|---|
| NSE announcements | **No**, local rules only | Link, symbol, derived label, times; **none of NSE's text** | NSE's terms: content "shall not be copied, modified… uploaded, transmitted, posted, stored (either in hardcopy or in an electronic retrieval system)… without prior written permission of NSE." Sending it to an LLM is uploading and transmitting it, and keeping its text is storing it. The owner chose rules-only on 2026-09-28. |
| BusinessLine | **Yes**, headline + RSS summary, on the **free tier** | Headline, RSS summary, link, times | Its terms prohibit "transmitting… or using any Content… for **commercial or public** purposes" and allow RSS "for personal and non-commercial use". A personal classifier fits that. However, on Gemini's free tier **Google uses submitted content "to provide, improve, and develop Google products and services" and human reviewers may read it**; the paid tier doesn't. The owner chose the free tier anyway on 2026-09-28, accepting that trade-off. To change it, enable billing on the key's Google Cloud project, and the paid-tier data terms then apply. |

Most NSE items carry the symbol in their filing link (`/corporate/SYMBOL_…`). About
four in ten don't (XBRL data files, debt-market PDFs, attachments named after a person);
for those the item's title, which is the company name, is matched exactly (case,
punctuation and "Limited/Ltd." ignored) against the names in `tickers.csv`. Nothing is
fuzzy-matched, so an unmatched name is skipped rather than guessed. NSE often posts the
same announcement twice (PDF plus an XBRL copy), so a second NSE alert for the same
company and event type within 30 minutes is suppressed. BusinessLine items name their stocks through
Gemini, restricted to `tickers.csv`.

## Results

See [`docs/RESULTS.md`](docs/RESULTS.md).

**News-driven alerts:** no measurements yet. The news archive starts when the news
service first runs (2026-09-28), and the event study needs sessions with news alerts and
live prices behind them. RESULTS.md is rewritten after each session, marks groups under
30 calls as too few, and reports only what the data shows.

**Price-move alerts** (replay):

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
