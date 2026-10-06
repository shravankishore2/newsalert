"""Build tickers.csv: Nifty 500 constituents (NSE's official list) joined to Dhan security IDs.

tickers.csv is not in the repo (it is derived from NSE's file); build it once after cloning:

    python -m newsalert build-universe --download          # NSE's published CSV, one request
    python -m newsalert build-universe ind_nifty500list.csv # or a copy you downloaded by hand

NSE's website terms prohibit "systematic or automated data collection", but exempt content
"available for download". --download is one request for that published file, run by hand
when the index is reviewed, never on a timer. Dhan's scrip master is a public file Dhan
documents for API users.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

NIFTY500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
NIFTY500_COLUMNS = {"Company Name", "Industry", "Symbol", "Series", "ISIN Code"}
MISSING_HELP = "run `python -m newsalert build-universe --download` once (tickers.csv is not in the repo)"


def check_nifty500(text: str) -> str:
    """The NSE file, or a clear error if NSE answered with something else (an HTML block page)."""
    head = next(csv.reader(io.StringIO(text)), [])
    if not NIFTY500_COLUMNS <= {h.strip() for h in head}:
        raise ValueError(f"not NSE's Nifty 500 CSV (header: {', '.join(head[:6]) or 'empty'})")
    return text


def download_nifty500(http) -> str:
    """One GET for NSE's published index file (browser-like headers: NSE refuses bare clients)."""
    r = http.get(NIFTY500_URL, timeout=60, follow_redirects=True, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
        "Accept": "text/csv,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"})
    r.raise_for_status()
    return check_nifty500(r.text)


def nse_equity_ids(scrip_master_csv: str) -> dict[str, tuple[str, str]]:
    """ISIN -> (Dhan security ID, NSE symbol) for NSE cash-segment equities."""
    out = {}
    for r in csv.DictReader(io.StringIO(scrip_master_csv)):
        if r["EXCH_ID"] == "NSE" and r["SEGMENT"] == "E" and r["INSTRUMENT"] == "EQUITY":
            out[r["ISIN"]] = (r["SECURITY_ID"], r["UNDERLYING_SYMBOL"])
    return out


def build_universe(nifty500_csv: str, scrip_master_csv: str) -> tuple[list[dict], list[dict]]:
    """Returns (rows for tickers.csv, constituents that could not be mapped)."""
    ids = nse_equity_ids(scrip_master_csv)
    rows, missing = [], []
    for r in csv.DictReader(io.StringIO(nifty500_csv)):
        isin = r["ISIN Code"].strip()
        row = {"symbol": r["Symbol"].strip(), "name": r["Company Name"].strip(),
               "industry": r["Industry"].strip(), "isin": isin}
        if isin in ids:
            row["security_id"] = ids[isin][0]
            rows.append(row)
        else:
            missing.append(row)
    return rows, missing


def write_tickers(path: str | Path, rows: list[dict]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "name", "industry", "isin", "security_id"])
        w.writeheader()
        w.writerows(rows)
