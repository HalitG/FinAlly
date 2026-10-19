"""MarketDataSource contract: the same suite runs against both implementations."""

import pytest

from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource
from app.market.simulator import SimulatorDataSource

from .test_massive import FakeMassiveClient


@pytest.fixture(params=["simulator", "massive-snapshot", "massive-eod"])
async def source(request):
    cache = PriceCache()
    if request.param == "simulator":
        src = SimulatorDataSource(cache, update_interval=0.05, seed=42)
    else:
        client = FakeMassiveClient(snapshot_allowed=request.param == "massive-snapshot")
        src = MassiveDataSource(
            "k", cache, poll_interval=0.05, eod_refresh_interval=0.05, client=client
        )
    await src.start(["AAPL", "MSFT"])
    yield src, cache
    await src.stop()


@pytest.mark.asyncio
class TestContract:
    async def test_start_warms_cache(self, source):
        _, cache = source
        assert cache.get_price("AAPL") is not None and cache.get_price("MSFT") is not None

    async def test_get_tickers_returns_copy(self, source):
        src, _ = source
        src.get_tickers().append("HACK")
        assert "HACK" not in src.get_tickers()

    async def test_add_is_idempotent(self, source):
        src, _ = source
        await src.add_ticker("NVDA")
        await src.add_ticker("NVDA")
        assert src.get_tickers().count("NVDA") == 1

    async def test_remove_clears_cache_and_bumps_version(self, source):
        src, cache = source
        v = cache.version
        await src.remove_ticker("AAPL")
        assert "AAPL" not in src.get_tickers()
        assert cache.get("AAPL") is None
        assert cache.version > v

    async def test_remove_unknown_is_noop(self, source):
        src, _ = source
        await src.remove_ticker("NOPE")
        assert sorted(src.get_tickers()) == ["AAPL", "MSFT"]

    async def test_no_writes_after_stop(self, source):
        import asyncio

        src, cache = source
        await src.stop()
        v = cache.version
        await asyncio.sleep(0.2)
        assert cache.version == v
