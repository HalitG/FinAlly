"""Reconcile the data source's tracked tickers with the desired set."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from .interface import MarketDataSource

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncResult:
    added: list[str]
    removed: list[str]


class TickerTracker:
    """Keeps a MarketDataSource tracking exactly a desired ticker set.

    The desired set is watchlist ∪ open positions (PLAN.md §6); the caller
    computes it from the database. The lock serializes concurrent syncs, e.g. a
    chat-triggered trade racing a manual one.
    """

    def __init__(self, source: MarketDataSource) -> None:
        self._source = source
        self._lock = asyncio.Lock()

    async def sync(self, desired: Iterable[str]) -> SyncResult:
        async with self._lock:
            wanted = set(desired)
            current = set(self._source.get_tickers())
            added = sorted(wanted - current)
            removed = sorted(current - wanted)
            for ticker in added:
                await self._source.add_ticker(ticker)
            for ticker in removed:
                await self._source.remove_ticker(ticker)
            if added or removed:
                logger.info("Tracked tickers: +%s -%s", added, removed)
            return SyncResult(added=added, removed=removed)
