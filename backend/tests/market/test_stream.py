"""SSE stream: event format, change gating, empty snapshots, router factory."""

import json

import pytest
from fastapi import FastAPI

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router


class FakeRequest:
    """Minimal Request stand-in: reports disconnected after `polls` checks."""

    client = None

    def __init__(self, polls: int) -> None:
        self._remaining = polls

    async def is_disconnected(self) -> bool:
        self._remaining -= 1
        return self._remaining < 0


async def collect(cache: PriceCache, polls: int, between=None) -> list[str]:
    events = []
    async for chunk in _generate_events(cache, FakeRequest(polls), interval=0):
        events.append(chunk)
        if between and chunk.startswith("data:"):
            between()
    return events


def payload(event: str) -> dict:
    assert event.startswith("data: ") and event.endswith("\n\n")
    return json.loads(event[len("data: ") :])


@pytest.mark.asyncio
class TestStream:
    async def test_retry_then_full_snapshot(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        cache.update("MSFT", 420.0)
        events = await collect(cache, polls=1)
        assert events[0] == "retry: 1000\n\n"
        data = payload(events[1])
        assert set(data) == {"AAPL", "MSFT"}
        assert data["AAPL"]["open_price"] == 190.0
        assert set(data["AAPL"]) == {
            "ticker",
            "price",
            "previous_price",
            "open_price",
            "timestamp",
            "change",
            "change_percent",
            "direction",
        }

    async def test_only_emits_when_version_changes(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        events = await collect(cache, polls=5)  # Nothing changes after the first
        assert len([e for e in events if e.startswith("data:")]) == 1

    async def test_emits_again_after_update(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        prices = iter([191.0, 192.0])
        events = await collect(
            cache, polls=3, between=lambda: cache.update("AAPL", next(prices, 192.0))
        )
        data_events = [payload(e) for e in events if e.startswith("data:")]
        assert [d["AAPL"]["price"] for d in data_events][:2] == [190.0, 191.0]

    async def test_empty_snapshot_sent_after_last_removal(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        events = await collect(cache, polls=2, between=lambda: cache.remove("AAPL"))
        data_events = [payload(e) for e in events if e.startswith("data:")]
        assert data_events[-1] == {}

    async def test_router_factory_can_build_two_apps(self):
        for _ in range(2):
            app = FastAPI()
            app.include_router(create_stream_router(PriceCache()))
            paths = [r.path for r in app.routes]
            assert paths.count("/api/stream/prices") == 1
