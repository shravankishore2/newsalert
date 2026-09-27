import csv

from newsalert.config import load_tickers
from newsalert.universe import build_universe, write_tickers

NIFTY = """Company Name,Industry,Symbol,Series,ISIN Code
Reliance Industries Ltd.,Oil Gas & Consumable Fuels,RELIANCE,EQ,INE002A01018
Dummy HEG Ltd.,Metals & Mining,DUMMYHEG,EQ,DUM545A01024
"""
HEAD = "EXCH_ID,SEGMENT,SECURITY_ID,ISIN,INSTRUMENT,UNDERLYING_SECURITY_ID,UNDERLYING_SYMBOL,SYMBOL_NAME"
SCRIP = f"""{HEAD}
NSE,E,2885,INE002A01018,EQUITY,2885,RELIANCE,RELIANCE INDUSTRIES LTD
BSE,E,500325,INE002A01018,EQUITY,500325,RELIANCE,RELIANCE INDUSTRIES LTD
NSE,D,35000,NA,FUTSTK,2885,RELIANCE,RELIANCE-FUT
"""


def test_maps_nse_equity_ids_and_reports_unmapped(tmp_path):
    rows, missing = build_universe(NIFTY, SCRIP)
    assert rows == [{"symbol": "RELIANCE", "name": "Reliance Industries Ltd.",
                     "industry": "Oil Gas & Consumable Fuels", "isin": "INE002A01018", "security_id": "2885"}]
    assert [m["symbol"] for m in missing] == ["DUMMYHEG"]
    write_tickers(tmp_path / "t.csv", rows)
    assert load_tickers(tmp_path / "t.csv")[0].security_id == "2885"


def test_shipped_tickers_csv_is_complete():
    rows = list(csv.DictReader(open("tickers.csv")))
    assert len(rows) == 500
    assert all(r["security_id"].isdigit() for r in rows)
    assert len({r["symbol"] for r in rows}) == 500 and len({r["security_id"] for r in rows}) == 500
