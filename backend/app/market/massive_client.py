"""Massive (Polygon.io) API client for real market data."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import date, timedelta
from enum import Enum

from massive import RESTClient
from massive.exceptions import BadResponse
from massive.rest.models import GroupedDailyAgg, SnapshotMarketType, TickerSnapshot

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)

NS_PER_S = 1_000_000_000
MS_PER_S = 1_000


class MassiveMode(str, Enum):
    SNAPSHOT = "snapshot"  # Paid plans: 15-min delayed or real-time snapshots
    EOD = "eod"  # Free plan: last session's closes from grouped daily bars


def best_price(snap: TickerSnapshot) -> tuple[float, float] | None:
    """Best available (price, unix_seconds) from a snapshot, or None.

    Order: last trade (Developer+) → latest minute bar → today's bar → previous
    close. Zero/missing values are skipped (snapshots are zeroed 3:30–4:00 AM ET).
    """
    lt = snap.last_trade
    if lt and lt.price:
        return lt.price, _ns_to_s(lt.sip_timestamp or snap.updated)
    if snap.min and snap.min.close:
        ts = snap.min.timestamp / MS_PER_S if snap.min.timestamp else _ns_to_s(snap.updated)
        return snap.min.close, ts
    for bar in (snap.day, snap.prev_day):
        if bar and bar.close:
            return bar.close, _ns_to_s(snap.updated)
    return None


def _ns_to_s(ns: int | None) -> float:
    return ns / NS_PER_S if ns else time.time()


def latest_grouped_daily(
    client: RESTClient, today: date, max_calls: int = 4
) -> tuple[date | None, dict[str, GroupedDailyAgg]]:
    """Most recent trading day's bars for the whole market, keyed by ticker.

    Walks back from `today`, skipping weekends without spending a call. A day
    that is empty (holiday) or refused with NOT_AUTHORIZED (today's EOD not yet
    published on the free plan) is skipped. Stops after max_calls (free: 5/min).
    """
    d, calls = today, 0
    while calls < max_calls:
        if d.weekday() < 5:  # Mon–Fri
            calls += 1
            try:
                bars = client.get_grouped_daily_aggs(date=d.isoformat(), adjusted=True)
            except BadResponse as e:
                if "NOT_AUTHORIZED" not in str(e):
                    raise
                bars = []
            if bars:
                return d, {b.ticker: b for b in bars if b.ticker and b.close}
        d -= timedelta(days=1)
    return None, {}


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    The first poll decides the mode:
      - SNAPSHOT: GET /v2/snapshot/locale/us/markets/stocks/tickers for all
        tracked tickers in one call, every `poll_interval` seconds (paid plans
        have no per-minute cap).
      - EOD: the key has no snapshot access (free Basic plan), so prices are the
        last session's closes from grouped daily bars, refreshed hourly.
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 5.0,
        eod_refresh_interval: float = 3600.0,
        client: RESTClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._eod_interval = eod_refresh_interval
        self._client = client  # Injectable for tests
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._mode: MassiveMode | None = None  # Decided by the first poll
        self._eod_bars: dict[str, GroupedDailyAgg] = {}
        self._last_written: dict[str, tuple[float, float]] = {}

    @property
    def mode(self) -> MassiveMode | None:
        return self._mode

    async def start(self, tickers: list[str]) -> None:
        if self._client is None:
            # retries=0: urllib3's sub-second retries can't outlast a per-minute
            # rate limit and would spend extra calls. The poll loop is the retry.
            self._client = RESTClient(api_key=self._api_key, retries=0)
        self._tickers = list(dict.fromkeys(tickers))

        # Immediate first poll: warms the cache and detects the mode
        await self._poll_once()

        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers, mode=%s",
            len(self._tickers),
            self._mode.value if self._mode else "undetermined",
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        logger.info("Massive poller stopped")

    async def add_ticker(self, ticker: str) -> None:
        if ticker in self._tickers:
            return
        self._tickers.append(ticker)
        if self._mode is MassiveMode.EOD:
            self._write_eod(ticker)  # Data already in memory, no API call
        else:
            logger.info("Massive: added ticker %s (will appear on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        self._tickers = [t for t in self._tickers if t != ticker]
        self._last_written.pop(ticker, None)
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internal ---

    async def _poll_loop(self) -> None:
        """Poll on interval. First poll already happened in start()."""
        while True:
            interval = self._eod_interval if self._mode is MassiveMode.EOD else self._interval
            await asyncio.sleep(interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        """One poll cycle. Never raises: the loop retries next interval."""
        try:
            if self._mode is MassiveMode.EOD:
                await self._refresh_eod()
            elif self._tickers:
                await self._poll_snapshot()
        except Exception as e:  # 401, 429, network, timeouts...
            logger.error("Massive poll failed: %s", e)

    async def _poll_snapshot(self) -> None:
        requested = list(self._tickers)  # Copy: the list may change during the call
        try:
            snaps = await asyncio.to_thread(
                self._client.get_snapshot_all,
                market_type=SnapshotMarketType.STOCKS,
                tickers=requested,
            )
        except BadResponse as e:
            if "NOT_AUTHORIZED" not in str(e):
                raise
            logger.warning(
                "Massive key has no snapshot access (free plan?) — showing end-of-day prices"
            )
            self._mode = MassiveMode.EOD
            await self._refresh_eod()
            return

        self._mode = MassiveMode.SNAPSHOT
        for snap in snaps:
            best = best_price(snap)
            if best is None:
                logger.warning("No usable price in Massive snapshot for %s", snap.ticker)
                continue
            self._write(snap.ticker, *best)

    async def _refresh_eod(self) -> None:
        session, bars = await asyncio.to_thread(latest_grouped_daily, self._client, date.today())
        if not bars:
            logger.warning("Massive: no end-of-day data found")
            return
        self._eod_bars = bars
        logger.info("Massive EOD prices from session %s", session)
        for ticker in list(self._tickers):
            self._write_eod(ticker)

    def _write_eod(self, ticker: str) -> None:
        bar = self._eod_bars.get(ticker)
        if bar:
            ts = bar.timestamp / MS_PER_S if bar.timestamp else time.time()
            self._write(ticker, bar.close, ts)

    def _write(self, ticker: str, price: float, timestamp: float) -> None:
        if ticker not in self._tickers:  # Removed while a request was in flight
            return
        if self._last_written.get(ticker) == (price, timestamp):
            return  # Nothing new (market closed / EOD refresh): keep SSE quiet
        self._last_written[ticker] = (price, timestamp)
        self._cache.update(ticker=ticker, price=price, timestamp=timestamp)
