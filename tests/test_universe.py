import httpx
import pytest

from newsalert.config import load_tickers
from newsalert.universe import NIFTY500_URL, build_universe, download_nifty500, write_tickers

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


def test_download_is_one_request_for_nses_file():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, text=NIFTY)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert download_nifty500(http) == NIFTY
    assert seen == [NIFTY500_URL]


def test_download_refuses_a_block_page():
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>Access Denied</html>"))) as http:
        with pytest.raises(ValueError, match="not NSE's Nifty 500 CSV"):
            download_nifty500(http)


def test_missing_tickers_csv_says_how_to_build_it(tmp_path):
    with pytest.raises(SystemExit, match="build-universe --download"):
        load_tickers(tmp_path / "tickers.csv")
