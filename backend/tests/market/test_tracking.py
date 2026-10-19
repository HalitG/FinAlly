"""TickerTracker: watchlist ∪ positions reconciliation."""

import asyncio

import pytest

from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource
from app.market.tracking import TickerTracker


@pytest.fixture
async def setup():
    cache = PriceCache()
    source = SimulatorDataSource(cache, update_interval=60.0, seed=1)
    await source.start(["AAPL", "MSFT"])
    yield source, cache, TickerTracker(source)
    await source.stop()


@pytest.mark.asyncio
class TestTickerTracker:
    async def test_adds_and_removes_difference(self, setup):
        source, cache, tracker = setup
        result = await tracker.sync({"AAPL", "TSLA"})
        assert result.added == ["TSLA"] and result.removed == ["MSFT"]
        assert sorted(source.get_tickers()) == ["AAPL", "TSLA"]
        assert cache.get("MSFT") is None
        assert cache.get_price("TSLA") is not None  # Simulator seeds immediately

    async def test_noop_when_in_sync(self, setup):
        _, cache, tracker = setup
        v = cache.version
        result = await tracker.sync(["AAPL", "MSFT"])
        assert result.added == [] and result.removed == []
        assert cache.version == v

    async def test_held_ticker_removed_from_watchlist_stays_priced(self, setup):
        _, cache, tracker = setup
        watchlist, held = {"AAPL"}, {"MSFT"}  # User removed MSFT but still holds it
        await tracker.sync(watchlist | held)
        assert cache.get_price("MSFT") is not None

    async def test_full_sell_of_unwatched_ticker_unsubscribes(self, setup):
        source, cache, tracker = setup
        await tracker.sync({"AAPL"})  # MSFT was held-only, now sold
        assert "MSFT" not in source.get_tickers()
        assert cache.get("MSFT") is None

    async def test_concurrent_syncs_serialize(self, setup):
        source, _, tracker = setup
        await asyncio.gather(tracker.sync({"AAPL", "NVDA"}), tracker.sync({"AAPL", "NVDA", "V"}))
        assert sorted(source.get_tickers()) == ["AAPL", "NVDA", "V"]
