"""RSS parsing and matching. Fixtures are synthetic (same structure as the real feeds)."""

from newsalert.clients import NewsItem, RssFeed, match_news, short_name

NSE_XML = b"""<?xml version="1.0"?><rss><channel>
<item><title>Alpha Industries Limited</title>
<link>https://nsearchives.nseindia.com/corporate/ALPHA_27092026175829_Board.pdf</link>
<description>Alpha Industries Limited has informed the Exchange about Board Meeting</description>
<pubDate>27-Sep-2026 17:58:46</pubDate></item>
<item><title>M&amp;M Limited</title>
<link>https://nsearchives.nseindia.com/corporate/M&amp;M_27092026170000_x.pdf</link>
<description>Update</description><pubDate>27-Sep-2026 17:00:00</pubDate></item>
</channel></rss>"""

BL_XML = b"""<?xml version="1.0"?><rss><channel>
<item><title>Alpha Industries shares jump on order win</title><link>https://example.test/a</link>
<pubDate>Sun, 27 Sep 2026 16:52:08 +0530</pubDate></item>
<item><title>Alpha Power posts loss</title><link>https://example.test/b</link>
<pubDate>Sun, 27 Sep 2026 12:00:00 +0530</pubDate></item>
<item><title>Markets close lower</title><link>https://example.test/c</link>
<pubDate>Sun, 27 Sep 2026 11:00:00 +0530</pubDate></item>
</channel></rss>"""


def test_short_name_strips_legal_suffixes():
    assert short_name("Reliance Industries Ltd.") == "Reliance Industries"
    assert short_name("Tata Consultancy Services Limited") == "Tata Consultancy Services"
    assert short_name("3M India Ltd.") == "3M"
    assert short_name("ABB India Ltd.") == "ABB"


def test_parse_nse_feed_extracts_symbol_and_ist_time():
    items = RssFeed(None, "NSE", "u").parse(NSE_XML)
    assert [i.symbol_hint for i in items] == ["ALPHA", "M&M"]
    assert items[0].published == "2026-09-27T17:58:46+05:30"
    assert "Board Meeting" in items[0].headline


def test_matching_by_symbol_and_by_name():
    nse = RssFeed(None, "NSE", "u").parse(NSE_XML)
    bl = RssFeed(None, "BL", "u").parse(BL_XML)
    hits = match_news(nse + bl, "ALPHA", "Alpha Industries Ltd.")
    assert [h.url for h in hits] == [nse[0].url, "https://example.test/a"]   # not "Alpha Power"
    assert match_news(nse + bl, "M&M", "Mahindra & Mahindra Ltd.")[0].symbol_hint == "M&M"
    assert match_news(bl, "XYZ", "Xy") == []  # names under 3 chars never match free text


def test_bad_xml_raises():
    import pytest
    from newsalert.clients import FetchError
    with pytest.raises(FetchError):
        RssFeed(None, "x", "u").parse(b"<html>blocked</html")
