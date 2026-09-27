import httpx
import pytest


async def test_real_network_is_blocked():
    """Proves the conftest guard is active: an unmocked request cannot go out."""
    async with httpx.AsyncClient() as http:
        with pytest.raises(Exception, match="network access attempted"):
            await http.get("https://api.dhan.co/v2/marketfeed/ltp")
