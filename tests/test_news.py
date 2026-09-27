"""RSS parsing and matching. Fixtures are synthetic (same structure as the real feeds)."""

from newsalert.clients import RssFeed

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


def test_parse_nse_feed_extracts_symbol_and_ist_time():
    items = RssFeed(None, "NSE", "u").parse(NSE_XML)
    assert [i.symbol_hint for i in items] == ["ALPHA", "M&M"]
    assert items[0].published == "2026-09-27T17:58:46+05:30"
    assert "Board Meeting" in items[0].headline


def test_bad_xml_raises():
    import pytest
    from newsalert.clients import FetchError
    with pytest.raises(FetchError):
        RssFeed(None, "x", "u").parse(b"<html>blocked</html")
