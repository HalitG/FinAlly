"""SSE streaming endpoint for live price updates."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Create the SSE streaming router with a reference to the price cache.

    The router is built per call, so creating the app twice (e.g. in tests)
    never registers /prices twice on a shared router.
    """
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint for live price updates.

        Each event is a full snapshot of every tracked ticker, keyed by ticker:

            data: {"AAPL": {"ticker": "AAPL", "price": 190.50, ...}, ...}

        Events are sent only when the cache version changes. Includes a retry
        directive so the browser auto-reconnects (EventSource built-in behavior).
        """
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering if proxied
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    """Async generator that yields SSE-formatted price snapshots.

    Checks the cache every `interval` seconds and emits only when its version
    has changed. The first snapshot goes out immediately. An empty cache is sent
    as `{}`: the frontend replaces its state, so silence would leave stale prices.
    """
    # Tell the client to retry after 1 second if the connection drops
    yield "retry: 1000\n\n"

    last_version = -1
    client_ip = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client_ip)

    try:
        while not await request.is_disconnected():
            current_version = price_cache.version
            if current_version != last_version:
                last_version = current_version
                data = {ticker: u.to_dict() for ticker, u in price_cache.get_all().items()}
                yield f"data: {json.dumps(data)}\n\n"
            await asyncio.sleep(interval)
        logger.info("SSE client disconnected: %s", client_ip)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
