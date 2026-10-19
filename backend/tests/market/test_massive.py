"""Tests for MassiveDataSource using real massive model objects (no MagicMock).

MagicMock invents any attribute, which once hid a `last_trade.timestamp` bug
(the real attribute is `sip_timestamp`). Fixtures here are built from the
documented JSON with the client's own `from_dict`.
"""

import asyncio
import threading
from datetime import date

import pytest
from massive.exceptions import BadResponse
from massive.rest.models import GroupedDailyAgg, TickerSnapshot

from app.market.cache import PriceCache
from app.market.massive_client import (
    MassiveDataSource,
    MassiveMode,
    best_price,
    latest_grouped_daily,
)

NOT_AUTHORIZED = '{"status":"NOT_AUTHORIZED","message":"You are not entitled to this data."}'

# Starter plan shape: no lastTrade (trades not entitled)
STARTER_SNAP = {
    "ticker": "AAPL",
    "day": {"o": 189.0, "c": 190.40},
    "min": {"c": 190.52, "t": 1760890500000},  # ms
    "prevDay": {"c": 189.00},
    "updated": 1760890512000000000,  # ns
}


def snap(ticker: str = "AAPL", **overrides) -> TickerSnapshot:
    return TickerSnapshot.from_dict({**STARTER_SNAP, "ticker": ticker, **overrides})


def bar(ticker: str, close: float) -> GroupedDailyAgg:
    return GroupedDailyAgg.from_dict({"T": ticker, "c": close, "t": 1760731200000})


class FakeMassiveClient:
    """Stands in for massive.RESTClient. Records calls; returns real models."""

    def __init__(self, snapshot_allowed: bool = True, prices: dict[str, float] | None = None):
        self.snapshot_allowed = snapshot_allowed
        self.prices = prices or {"AAPL": 190.52, "MSFT": 420.10, "NVDA": 801.00}
        self.snapshot_calls: list[list[str]] = []
        self.grouped_calls: list[str] = []
        self.fail_with: Exception | None = None

    def get_snapshot_all(self, market_type, tickers):
        self.snapshot_calls.append(list(tickers))
        if self.fail_with:
            raise self.fail_with
        if not self.snapshot_allowed:
            raise BadResponse(NOT_AUTHORIZED)
        return [
            snap(t, min={"c": self.prices[t], "t": 1760890500000})
            for t in tickers
            if t in self.prices  # Unknown symbols are silently absent, like the API
        ]

    def get_grouped_daily_aggs(self, date, adjusted=True):
        self.grouped_calls.append(date)
        return [bar(t, p) for t, p in self.prices.items()]


def make_source(client: FakeMassiveClient, cache: PriceCache | None = None) -> MassiveDataSource:
    return MassiveDataSource(
        api_key="test-key",
        price_cache=cache if cache is not None else PriceCache(),  # Empty cache is falsy
        poll_interval=60.0,  # Long interval: tests drive polls explicitly
        client=client,
    )


class TestBestPrice:
    def test_last_trade_preferred_and_ns_converted(self):
        s = snap(lastTrade={"p": 190.60, "t": 1760890511000000000})
        assert best_price(s) == (190.60, 1760890511.0)

    def test_starter_plan_uses_minute_bar_ms(self):
        assert best_price(snap()) == (190.52, 1760890500.0)

    def test_zeroed_fields_fall_back_to_prev_day(self):
        s = snap(day={"c": 0}, min={"c": 0})
        assert best_price(s) == (189.00, 1760890512.0)

    def test_nothing_usable(self):
        assert best_price(TickerSnapshot.from_dict({"ticker": "X"})) is None


class TestLatestGroupedDaily:
    class Client:
        def __init__(self, available: dict[str, list]):
            self.available, self.calls = available, []

        def get_grouped_daily_aggs(self, date, adjusted=True):
            self.calls.append(date)
            value = self.available.get(date, [])
            if isinstance(value, Exception):
                raise value
            return value

    def test_monday_morning_walks_back_to_friday_skipping_weekend(self):
        client = self.Client(
            {
                "2026-10-19": BadResponse(NOT_AUTHORIZED),  # Today's EOD not published yet
                "2026-10-16": [bar("AAPL", 190.0)],
            }
        )
        session, bars = latest_grouped_daily(client, date(2026, 10, 19))
        assert session == date(2026, 10, 16)
        assert bars["AAPL"].close == 190.0
        assert client.calls == ["2026-10-19", "2026-10-16"]  # Sat/Sun cost nothing

    def test_holiday_returns_empty_and_is_skipped(self):
        client = self.Client({"2026-10-15": [bar("AAPL", 188.0)]})  # 16th: holiday → []
        session, _ = latest_grouped_daily(client, date(2026, 10, 16))
        assert session == date(2026, 10, 15)

    def test_gives_up_after_max_calls(self):
        client = self.Client({})
        assert latest_grouped_daily(client, date(2026, 10, 16), max_calls=4) == (None, {})
        assert len(client.calls) == 4

    def test_other_errors_propagate(self):
        client = self.Client(
            {"2026-10-16": BadResponse('{"status":"ERROR","error":"Unknown API Key"}')}
        )
        with pytest.raises(BadResponse):
            latest_grouped_daily(client, date(2026, 10, 16))


@pytest.mark.asyncio
class TestSnapshotMode:
    async def test_start_polls_immediately_and_detects_snapshot_mode(self):
        cache = PriceCache()
        source = make_source(FakeMassiveClient(), cache)
        await source.start(["AAPL", "MSFT"])
        assert source.mode is MassiveMode.SNAPSHOT
        assert cache.get_price("AAPL") == 190.52
        assert cache.get("AAPL").timestamp == 1760890500.0
        await source.stop()

    async def test_add_ticker_waits_for_next_poll(self):
        cache = PriceCache()
        source = make_source(FakeMassiveClient(), cache)
        await source.start(["AAPL"])
        await source.add_ticker("NVDA")
        assert "NVDA" in source.get_tickers()
        assert cache.get_price("NVDA") is None  # PLAN §6: Massive does not seed
        await source._poll_once()
        assert cache.get_price("NVDA") == 801.00
        await source.stop()

    async def test_unknown_symbol_stays_unpriced(self):
        cache = PriceCache()
        source = make_source(FakeMassiveClient(), cache)
        await source.start(["AAPL", "ZZZZ"])
        assert "ZZZZ" in source.get_tickers()
        assert cache.get_price("ZZZZ") is None
        await source.stop()

    async def test_unchanged_data_does_not_bump_version(self):
        cache = PriceCache()
        source = make_source(FakeMassiveClient(), cache)
        await source.start(["AAPL"])
        v = cache.version
        await source._poll_once()  # Same price and timestamp (market closed)
        assert cache.version == v
        await source.stop()

    async def test_api_error_does_not_crash_or_change_mode(self):
        client = FakeMassiveClient()
        cache = PriceCache()
        source = make_source(client, cache)
        await source.start(["AAPL"])
        client.fail_with = BadResponse('{"status":"ERROR","error":"Unknown API Key"}')
        await source._poll_once()  # Logged, not raised
        assert source.mode is MassiveMode.SNAPSHOT
        assert cache.get_price("AAPL") == 190.52  # Last known price kept
        await source.stop()

    async def test_ticker_removed_during_request_is_not_written_back(self):
        client = FakeMassiveClient()
        cache = PriceCache()
        source = make_source(client, cache)
        await source.start(["AAPL", "MSFT"])

        release = threading.Event()
        real_call = client.get_snapshot_all

        def slow_call(market_type, tickers):
            release.wait(timeout=5)  # Hold the request "in flight"
            return real_call(market_type, tickers)

        client.get_snapshot_all = slow_call
        client.prices["MSFT"] = 999.0  # New data in flight
        poll = asyncio.create_task(source._poll_once())
        await asyncio.sleep(0.05)  # Request is now running in its thread
        await source.remove_ticker("MSFT")
        release.set()
        await poll
        assert cache.get("MSFT") is None
        await source.stop()

    async def test_stop_cancels_task_and_is_idempotent(self):
        source = make_source(FakeMassiveClient())
        await source.start(["AAPL"])
        assert source._task is not None and not source._task.done()
        await source.stop()
        assert source._task is None
        await source.stop()

    async def test_empty_tickers_skips_api_call(self):
        client = FakeMassiveClient()
        source = make_source(client)
        await source.start([])
        assert client.snapshot_calls == []
        assert source.mode is None
        await source.stop()


@pytest.mark.asyncio
class TestEodMode:
    async def test_free_plan_falls_back_to_eod(self):
        client = FakeMassiveClient(snapshot_allowed=False)
        cache = PriceCache()
        source = make_source(client, cache)
        await source.start(["AAPL", "MSFT"])
        assert source.mode is MassiveMode.EOD
        assert cache.get_price("AAPL") == 190.52
        assert cache.get("AAPL").timestamp == 1760731200.0
        assert len(client.snapshot_calls) == 1  # Probe once, never again
        await source._poll_once()
        assert len(client.snapshot_calls) == 1
        await source.stop()

    async def test_add_ticker_in_eod_mode_is_priced_immediately_without_api_call(self):
        client = FakeMassiveClient(snapshot_allowed=False)
        cache = PriceCache()
        source = make_source(client, cache)
        await source.start(["AAPL"])
        calls = len(client.grouped_calls)
        await source.add_ticker("NVDA")
        assert cache.get_price("NVDA") == 801.00
        assert len(client.grouped_calls) == calls
        await source.stop()

    async def test_remove_then_readd_rewrites_price(self):
        client = FakeMassiveClient(snapshot_allowed=False)
        cache = PriceCache()
        source = make_source(client, cache)
        await source.start(["AAPL"])
        await source.remove_ticker("AAPL")
        assert cache.get("AAPL") is None
        await source.add_ticker("AAPL")
        assert cache.get_price("AAPL") == 190.52
        await source.stop()


@pytest.mark.asyncio
class TestTickerSet:
    async def test_add_is_idempotent_and_no_normalization(self):
        source = make_source(FakeMassiveClient())
        await source.add_ticker("AAPL")
        await source.add_ticker("AAPL")
        assert source.get_tickers() == ["AAPL"]

    async def test_start_dedupes_preserving_order(self):
        source = make_source(FakeMassiveClient())
        await source.start(["MSFT", "AAPL", "MSFT"])
        assert source.get_tickers() == ["MSFT", "AAPL"]
        await source.stop()

    async def test_remove_clears_cache_and_bumps_version(self):
        cache = PriceCache()
        source = make_source(FakeMassiveClient(), cache)
        await source.start(["AAPL", "MSFT"])
        v = cache.version
        await source.remove_ticker("AAPL")
        assert source.get_tickers() == ["MSFT"]
        assert cache.get("AAPL") is None
        assert cache.version > v
        await source.stop()
