"""Build tickers.csv: Nifty 500 constituents (NSE's official list) joined to Dhan security IDs.

The NSE list is read from a file you download by hand (NSE's website terms prohibit
automated data collection). Dhan's scrip master is a public file Dhan documents for
API users, so it can be fetched directly.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

NIFTY500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"


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
