"""Abstract interface for market data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source for prices; it reads
    the cache. Tickers arrive already normalized by the API layer.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        # ... app runs ...
        await source.add_ticker("TSLA")
        await source.remove_ticker("GOOGL")
        # ... app shutting down ...
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.

        Starts a background task that writes to the PriceCache. Performs any
        first fetch before returning so the cache is warm. Never raises for data
        errors (logs them). Must be called exactly once.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task. Idempotent. No cache writes after it returns."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the tracked set. No-op if already present.

        Whether the price is cached on return depends on the source: the
        simulator seeds it immediately; Massive may need the next poll.
        """

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the tracked set. No-op if not present.

        Also removes it from the PriceCache. No write for this ticker may happen
        afterwards, even from a request already in flight.
        """

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return a copy of the currently tracked tickers."""
