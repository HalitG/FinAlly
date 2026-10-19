# Market Data Backend: Detailed Design

The implementation-ready design for FinAlly's market data subsystem. It covers the unified price API, the shared price cache, the GBM simulator, the Massive (Polygon.io) client, ticker tracking, SSE streaming, and how the rest of the backend uses them.

It consolidates every planning document: `PLAN.md` (§3, §5–§8, §12–§13), `REVIEW.md` (C2–C4, B2–B4, M1–M4, T1), `MASSIVE_API.md`, `MARKET_INTERFACE.md` and `MARKET_SIMULATOR.md`. Where those documents explain *why*, this one gives the finished *what*: complete module code, tests, and integration examples.

> **Verified, not sketched.** Every code block in §4–§13 and Appendix A is copied verbatim from a working build of this design. It was built against the repo's locked dependencies (`massive` 2.2.0, FastAPI 0.128) and checked on 2026-10-19:
> - **139 tests pass** at 97% line coverage of `app/`: the 49 existing tests that carry over unchanged, plus the 90 rewritten and new tests in Appendix A.
> - `ruff check` is clean.
> - The example app was run under uvicorn and its live SSE stream read with `curl` (§11.3).
>
> Code blocks are generated from the source files, so they cannot drift from what was tested.

---

## Contents

1. [Requirements](#1-requirements)
2. [Architecture](#2-architecture)
3. [Files and changes](#3-files-and-changes)
4. [Data model: `models.py`](#4-data-model-modelspy)
5. [Price cache: `cache.py`](#5-price-cache-cachepy)
6. [Source interface: `interface.py`](#6-source-interface-interfacepy)
7. [Simulator: `seed_prices.py`, `simulator.py`](#7-simulator-seed_pricespy-simulatorpy)
8. [Massive client: `massive_client.py`](#8-massive-client-massive_clientpy)
9. [Factory: `factory.py`](#9-factory-factorypy)
10. [Ticker tracking: `tracking.py`](#10-ticker-tracking-trackingpy)
11. [SSE stream: `stream.py`](#11-sse-stream-streampy)
12. [Package API: `__init__.py`](#12-package-api-__init__py)
13. [Using it from the API layer](#13-using-it-from-the-api-layer)
14. [Examples](#14-examples)
15. [Failure modes](#15-failure-modes)
16. [Configuration](#16-configuration)
17. [Testing](#17-testing)
18. [Implementation plan](#18-implementation-plan)
19. [Decisions beyond PLAN.md](#19-decisions-beyond-planmd)
- [Appendix A: test code](#appendix-a-test-code)

---

## 1. Requirements

| # | Requirement | Source | Where |
|---|---|---|---|
| R1 | One interface; simulator by default, Massive if `MASSIVE_API_KEY` is non-blank | PLAN §5, §6 | §6, §9 |
| R2 | Simulator: GBM, ~500 ms ticks, correlated sectors, random 2–5% events, realistic seeds, unknown tickers $50–$300 | PLAN §6 | §7 |
| R3 | Simulator `add_ticker()` seeds the cache immediately | PLAN §6 | §7.2 |
| R4 | `SIM_SEED` makes the simulator reproducible | PLAN §5, §13 | §7.2, §9 |
| R5 | Massive: REST polling, multi-ticker, same output format; `add_ticker()` does not seed | PLAN §6 | §8 |
| R6 | Massive free tier verified. **Finding: the free tier has no snapshot access**, so it needs an end-of-day mode. | PLAN §6, REVIEW M1 | §8.1 |
| R7 | Tracked tickers = watchlist ∪ open positions, reconciled by one helper | PLAN §6 | §10, §13 |
| R8 | Cache holds price, previous price, `open_price`, timestamp; `version` bumps on write **and removal** | PLAN §6, §13 | §5 |
| R9 | SSE `GET /api/stream/prices`: `retry: 1000`, full snapshot per event, sent only on version change | PLAN §6 | §11 |
| R10 | Router built inside `create_stream_router()` | PLAN §13 | §11 |
| R11 | Tickers normalized and validated once at the API layer; sources do no normalization | PLAN §8, §13 | §13.1 |
| R12 | Trading an untracked ticker auto-adds it; with no price → `400 No price yet for XYZ, try again shortly` | PLAN §8 | §13.1 |
| R13 | Valuation falls back to `avg_cost` when there is no cached price | PLAN §7 | §13.3 |
| R14 | Single process, single uvicorn worker; start and stop in the FastAPI lifespan | PLAN §3, §7 | §13.2 |

## 2. Architecture

```
                         create_market_data_source(cache)        env: MASSIVE_API_KEY, SIM_SEED,
                                       │                              MASSIVE_POLL_INTERVAL
                     ┌─────────────────┴─────────────────┐
                     ▼                                   ▼
          SimulatorDataSource                  MassiveDataSource
          asyncio task, step every 0.5 s       asyncio task; REST calls via asyncio.to_thread
          GBMSimulator (pure, sync)            mode set by first poll:
                     │                           SNAPSHOT (paid)  every 5 s, 1 call
                     │                           EOD (free)       hourly, ≤ 4 calls
                     └──────────── writes ───────────────┘
                                       ▼
              PriceCache   ticker → PriceUpdate(price, previous_price, open_price, timestamp)
              threading.Lock · version++ on update and on remove
                                       │ reads
     ┌───────────────┬─────────────────┼──────────────────┬───────────────────┐
     ▼               ▼                 ▼                  ▼                   ▼
 SSE generator   GET /api/watchlist  POST trade        record_snapshot()   chat context
 (per client,    GET /api/portfolio  (fill price)      (every 30 s)        (LLM prompt)
  0.5 s check)
                                       ▲ add/remove tickers
                     TickerTracker.sync(watchlist ∪ held)   ← after every watchlist change & trade
```

**Concurrency model.** Everything runs on one asyncio event loop in one process. There is one background task for the market source and one generator per SSE client. The only other threads are the short-lived `asyncio.to_thread` workers that run Massive's synchronous HTTP calls. `PriceCache` uses a `threading.Lock` so it is safe from both. Each cache operation holds the lock for a dict lookup and assignment only.

**Why push to a cache rather than pull from the source?** The two sources have different cadences: 0.5 s for the simulator, 5 s for Massive snapshots, 1 h for Massive EOD. Consumers never care which source runs or when it last updated. A trade reads the latest price in O(1), and SSE emits only when `version` moves.

**Timing at a glance**

| Event | Simulator | Massive SNAPSHOT | Massive EOD |
|---|---|---|---|
| Cache warm after `start()` | immediately (seed prices) | after first poll (≤ ~1 s typical) | after probe + walk-back |
| Price changes | every 0.5 s | every 5 s while the market moves | never intraday |
| New ticker priced | on `add_ticker()` return | next poll (≤ 5 s) | on `add_ticker()` return (from memory) |
| SSE events | ~2/s | ≤ 1 per poll, none when unchanged | ~none |

## 3. Files and changes

```
backend/app/market/
├── __init__.py         package exports                                   changed (+TickerTracker)
├── models.py           PriceUpdate                                       changed (+open_price)
├── cache.py            PriceCache                                        changed
├── interface.py        MarketDataSource ABC                              docstrings only
├── seed_prices.py      seed prices, GBM params, correlation constants   unchanged
├── simulator.py        GBMSimulator, SimulatorDataSource                 changed (seeding)
├── massive_client.py   MassiveDataSource, best_price, latest_grouped_daily  rewritten
├── factory.py          create_market_data_source                         changed (env vars)
├── tracking.py         TickerTracker, SyncResult                         NEW
└── stream.py           create_stream_router, _generate_events            changed
```

| Change | Why |
|---|---|
| `open_price` on `PriceUpdate`, carried by the cache | PLAN §6/§13: change-since-open in the UI |
| `remove()` bumps `version` | PLAN §13: SSE must drop removed tickers promptly |
| Simulator `seed` → private RNG instances | PLAN §5/§13: `SIM_SEED` for E2E |
| Massive: `best_price()` fallback chain | Current code reads `last_trade.timestamp`, which doesn't exist (`AttributeError` on every ticker); `last_trade` is absent on Starter (MASSIVE_API.md §10) |
| Massive: EOD mode | The free plan has no snapshot access (MASSIVE_API.md §2) |
| Massive: `retries=0`, guard writes for removed tickers, skip unchanged writes, injectable client, no uppercasing | Rate-limit budget, race safety, quiet SSE, testability, PLAN §13 |
| `TickerTracker` | PLAN §6: one place that reconciles watchlist ∪ positions |
| SSE router built per call; empty snapshots emitted | PLAN §13; replace-semantics clients need `{}` |

## 4. Data model: `models.py`

`PriceUpdate` is the only type that leaves the market package. Derived fields are properties, so they can never be inconsistent with `price` and `previous_price`. `to_dict()` is the single serialization point for SSE and REST.

`backend/app/market/models.py`

```python
"""Data models for market data."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float  # Price at the previous cache write (tick-to-tick)
    open_price: float  # First price the cache saw for this ticker this session
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        """Absolute price change from previous update."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from previous update."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat'."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission (shape fixed by PLAN.md §6)."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "open_price": self.open_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
        }
```

- `open_price` has **no default**: forgetting it is a `TypeError`, not a silent wrong value. Construct with keywords.
- `change` and `change_percent` are **tick-to-tick** (used for flash direction). Change since open is computed by the frontend: `(price - open_price) / open_price * 100` (PLAN §6).

## 5. Price cache: `cache.py`

`backend/app/market/cache.py`

```python
"""Thread-safe in-memory price cache."""

from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker.

    Writer: the one active MarketDataSource.
    Readers: SSE stream, portfolio valuation, trade execution, snapshots, LLM context.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Monotonic; bumped on every update and every removal

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        The first update for a ticker sets previous_price == open_price == price
        (direction 'flat'). Later updates carry open_price forward.
        """
        with self._lock:
            price = round(price, 2)
            prev = self._prices.get(ticker)
            update = PriceUpdate(
                ticker=ticker,
                price=price,
                previous_price=prev.price if prev else price,
                open_price=prev.open_price if prev else price,
                timestamp=time.time() if timestamp is None else timestamp,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        """Get the latest price for a single ticker, or None if unknown."""
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices. Returns a shallow copy."""
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: get just the price float, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def remove(self, ticker: str) -> None:
        """Remove a ticker. Bumps version (if it was present) so SSE emits a snapshot
        without it. A re-added ticker starts a fresh session (new open_price)."""
        with self._lock:
            if self._prices.pop(ticker, None) is not None:
                self._version += 1

    @property
    def version(self) -> int:
        """Current version counter, for SSE change detection."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

| Behavior | Detail |
|---|---|
| First write | `previous_price = open_price = price`, `direction = "flat"` |
| Later writes | `open_price` carried forward from the previous entry, so no second dict is needed |
| `remove()` | Drops the entry **and its `open_price`**, and bumps `version` only if something was removed |
| Rounding | Prices stored to cents. Sources keep full precision internally. |
| `timestamp=None` | Means "now". Checked with `is None`, so `0.0` is preserved. |
| `version` | Read without the lock: a single int read, polled every 500 ms by SSE |

> **Gotcha:** `PriceCache` defines `__len__`, so an **empty cache is falsy**. Never write `cache or PriceCache()` or `if not cache:`; use `is None`. This bit a test helper during verification.

## 6. Source interface: `interface.py`

`backend/app/market/interface.py`

```python
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
```

| Method | Simulator | Massive SNAPSHOT | Massive EOD |
|---|---|---|---|
| `start(tickers)` | Builds the simulator, writes seed prices, starts the loop | Probe poll (decides the mode), starts the loop | Probe → walk-back, writes closes, starts the loop |
| `add_ticker(t)` | Price cached on return | Price on next poll | Price cached on return if `t` is in the stored bars |
| `remove_ticker(t)` | Removed from the simulator and the cache | Removed from the set and the cache; in-flight results for `t` dropped | Same |
| `get_tickers()` | Copy, insertion order | Copy, insertion order | Copy |
| `stop()` | Cancel and await the task; idempotent | Same | Same |

Input tickers are already normalized (§13.1). Sources never uppercase or validate.

## 7. Simulator: `seed_prices.py`, `simulator.py`

The model and its numbers are in MARKET_SIMULATOR.md. In summary: an exact GBM step with Δt = 0.5 s of a 252 × 6.5 h trading year, so wall-clock time equals market time. Shocks are correlated through the Cholesky factor of a sector matrix (tech 0.6, finance 0.5, otherwise 0.3; positive-definite for any mix, min eigenvalue ≥ 0.4). Events add a 2–5% jump with p = 0.001 per ticker per tick. Events dominate visible volatility: about 9.7% σ per hour versus 0.55% from GBM alone.

### 7.1 `seed_prices.py` (unchanged)

`backend/app/market/seed_prices.py`

```python
"""Seed prices and per-ticker parameters for the market simulator."""

# Realistic starting prices for the default watchlist (as of project creation)
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00,
    "GOOGL": 175.00,
    "MSFT": 420.00,
    "AMZN": 185.00,
    "TSLA": 250.00,
    "NVDA": 800.00,
    "META": 500.00,
    "JPM": 195.00,
    "V": 280.00,
    "NFLX": 600.00,
}

# Per-ticker GBM parameters
# sigma: annualized volatility (higher = more price movement)
# mu: annualized drift / expected return
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL": {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT": {"sigma": 0.20, "mu": 0.05},
    "AMZN": {"sigma": 0.28, "mu": 0.05},
    "TSLA": {"sigma": 0.50, "mu": 0.03},  # High volatility
    "NVDA": {"sigma": 0.40, "mu": 0.08},  # High volatility, strong drift
    "META": {"sigma": 0.30, "mu": 0.05},
    "JPM": {"sigma": 0.18, "mu": 0.04},  # Low volatility (bank)
    "V": {"sigma": 0.17, "mu": 0.04},  # Low volatility (payments)
    "NFLX": {"sigma": 0.35, "mu": 0.05},
}

# Default parameters for tickers not in the list above (dynamically added)
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Correlation groups for the simulator's Cholesky decomposition
# Tickers in the same group have higher intra-group correlation
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

# Correlation coefficients
INTRA_TECH_CORR = 0.6  # Tech stocks move together
INTRA_FINANCE_CORR = 0.5  # Finance stocks move together
CROSS_GROUP_CORR = 0.3  # Between sectors / unknown tickers
TSLA_CORR = 0.3  # TSLA does its own thing
```

### 7.2 `simulator.py`

`backend/app/market/simulator.py`

```python
"""GBM-based market simulator."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random

import numpy as np

from .cache import PriceCache
from .interface import MarketDataSource
from .seed_prices import (
    CORRELATION_GROUPS,
    CROSS_GROUP_CORR,
    DEFAULT_PARAMS,
    INTRA_FINANCE_CORR,
    INTRA_TECH_CORR,
    SEED_PRICES,
    TICKER_PARAMS,
    TSLA_CORR,
)

logger = logging.getLogger(__name__)


class GBMSimulator:
    """Geometric Brownian Motion simulator for correlated stock prices.

    Math:
        S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)

    Where:
        S(t)   = current price
        mu     = annualized drift (expected return)
        sigma  = annualized volatility
        dt     = time step as fraction of a trading year
        Z      = correlated standard normal random variable

    The tiny dt (~8.5e-8 for 500ms ticks over 252 trading days * 6.5h/day)
    produces sub-cent moves per tick that accumulate naturally over time.
    All randomness comes from two private generators seeded by `seed`, so a
    seeded run is reproducible and isolated from the global RNGs.
    """

    # 500ms expressed as a fraction of a trading year
    # 252 trading days * 6.5 hours/day * 3600 seconds/hour = 5,896,800 seconds
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
        seed: int | None = None,
    ) -> None:
        self._dt = dt
        self._event_prob = event_probability
        self._rng = random.Random(seed)  # Events, unknown-ticker start prices
        self._np_rng = np.random.default_rng(seed)  # Correlated normal draws

        # Per-ticker state
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}

        # Cholesky decomposition of the correlation matrix (for correlated moves)
        self._cholesky: np.ndarray | None = None

        # Initialize all starting tickers
        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    # --- Public API ---

    def step(self) -> dict[str, float]:
        """Advance all tickers by one time step. Returns {ticker: new_price}.

        This is the hot path — called every 500ms. Keep it fast.
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        # Generate n independent standard normal draws, then correlate them
        z = self._np_rng.standard_normal(n)
        if self._cholesky is not None:
            z = self._cholesky @ z

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu = params["mu"]
            sigma = params["sigma"]

            # GBM: S(t+dt) = S(t) * exp((mu - 0.5*sigma^2)*dt + sigma*sqrt(dt)*Z)
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random event: ~0.1% chance per tick per ticker
            # With 10 tickers at 2 ticks/sec, expect an event ~every 50 seconds
            if self._rng.random() < self._event_prob:
                shock = self._rng.uniform(0.02, 0.05) * self._rng.choice((-1, 1))
                self._prices[ticker] *= 1 + shock
                logger.debug("Random event on %s: %+.1f%%", ticker, shock * 100)

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the simulation. Rebuilds the correlation matrix."""
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the simulation. Rebuilds the correlation matrix."""
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        """Current price for a ticker, or None if not tracked."""
        return self._prices.get(ticker)

    def get_tickers(self) -> list[str]:
        """Return the list of currently tracked tickers."""
        return list(self._tickers)

    # --- Internals ---

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add a ticker without rebuilding Cholesky (for batch initialization)."""
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        # Only unknown tickers consume a random number, keeping seeded runs stable
        self._prices[ticker] = SEED_PRICES.get(ticker) or self._rng.uniform(50.0, 300.0)
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """Rebuild the Cholesky decomposition of the ticker correlation matrix.

        Called whenever tickers are added or removed. Fine for n <= 50.
        """
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return

        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho

        self._cholesky = np.linalg.cholesky(corr)

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        """Correlation between two tickers based on sector grouping.

        - Same tech sector:    0.6
        - Same finance sector: 0.5
        - TSLA with anything:  0.3 (it does its own thing)
        - Cross-sector:        0.3
        - Unknown tickers:     0.3
        """
        tech = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]

        # TSLA is in the tech set but behaves independently
        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR

        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR

        return CROSS_GROUP_CORR


class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by the GBM simulator.

    Runs a background asyncio task that calls GBMSimulator.step() every
    `update_interval` seconds and writes results to the PriceCache.
    """

    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,
        event_probability: float = 0.001,
        seed: int | None = None,
    ) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._seed = seed
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(
            tickers=tickers,
            event_probability=self._event_prob,
            seed=self._seed,
        )
        # Seed the cache with initial prices so SSE has data immediately
        for ticker in self._sim.get_tickers():
            self._cache.update(ticker=ticker, price=self._sim.get_price(ticker))
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("Simulator started with %d tickers", len(self._sim.get_tickers()))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        logger.info("Simulator stopped")

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            # Seed cache immediately so the ticker has a price right away
            self._cache.update(ticker=ticker, price=self._sim.get_price(ticker))
            logger.info("Simulator: added ticker %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        logger.info("Simulator: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        """Core loop: step the simulation, write to cache, sleep.

        step() and the cache writes run with no await in between, so a
        remove_ticker() can never interleave and resurrect a removed ticker.
        """
        while True:
            try:
                if self._sim:
                    for ticker, price in self._sim.step().items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

Design points:

- **Two classes.** `GBMSimulator` is the pure, synchronous math and is unit-testable without an event loop. `SimulatorDataSource` adapts it to the interface and owns the asyncio loop and cache writes.
- **Seeding.** `random.Random(seed)` and `np.random.default_rng(seed)` are private instances, so other code that uses the global RNGs can't perturb a seeded run (tested). `SEED_PRICES.get(t) or rng.uniform(...)` draws randomness only for unknown tickers. With the same seed and the same add/remove sequence, the k-th tick is identical. Wall-clock timing is not, so E2E tests assert behavior, not values.
- **No removal race.** `step()` and its cache writes run with no `await` in between.
- **Full precision inside, cents outside.** `_prices` is never rounded, so rounding never accumulates.

## 8. Massive client: `massive_client.py`

### 8.1 Mode state machine

```
                 start() → first poll (get_snapshot_all)
                                │
          ┌─────────── success ─┴─ BadResponse NOT_AUTHORIZED ───────────┐
          ▼                                                              ▼
   SNAPSHOT (paid plan)                                       EOD (free Basic plan)
   poll every poll_interval (5 s)                             refresh every eod_refresh_interval (1 h)
   1 call/poll, prices from best_price()                      latest_grouped_daily(): ≤ 4 calls,
   stays SNAPSHOT on other errors                             closes for the whole market in memory
                                                               (sticky until restart)
   (no tickers at start → mode undetermined until the first poll that has tickers)
```

**Free-plan call budget at startup:** 1 probe + ≤ 4 walk-back calls = **≤ 5**, exactly the 5/minute limit. The walk-back skips weekends without calling. A Monday-morning start costs 2 calls (today refused, then Friday).

### 8.2 Code

`backend/app/market/massive_client.py`

```python
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
```

### 8.3 Behavior notes

| Topic | Behavior |
|---|---|
| Price source per plan | Developer+: `last_trade.price`. Starter: `min.close`. Pre-market reset window (3:30–4:00 ET): falls back to `prev_day.close`. |
| Timestamps | `sip_timestamp` and `updated` are **ns**; `min.timestamp` and bar `t` are **ms**; the cache stores **s** |
| Unchanged data | `(price, ts)` identical to the last write → no cache write, so `version` stays still and SSE is quiet, as PLAN §6 expects |
| Unknown symbols | Silently absent from snapshot and grouped-daily responses. The ticker stays tracked and unpriced, and a trade returns 400 "No price yet". |
| Removal during a request | `_write()` drops results for tickers no longer tracked. This runs on the loop thread, so it is race-free (tested). |
| Retries | `RESTClient(retries=0)`: urllib3's 0.2–1.6 s retries can't outlast a per-minute limit and would spend quota |
| Startup latency | Bounded by the client's 10 s connect + 10 s read timeouts. Errors are logged and the app starts without prices. |
| Client | Built in `start()` (or injected), so constructing the source makes no network calls |

## 9. Factory: `factory.py`

`backend/app/market/factory.py`

```python
"""Factory for creating market data sources."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import TypeVar

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)

T = TypeVar("T", int, float)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the appropriate market data source based on environment variables.

    - MASSIVE_API_KEY set and non-blank → MassiveDataSource (real market data);
      MASSIVE_POLL_INTERVAL (seconds, default 5) sets the snapshot poll interval
    - Otherwise → SimulatorDataSource; SIM_SEED (integer) makes it reproducible

    This is the only place that reads market-data environment variables.
    Returns an unstarted source. Caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        interval = _env_number("MASSIVE_POLL_INTERVAL", float, 5.0)
        logger.info("Market data source: Massive API (poll %.1fs)", interval)
        return MassiveDataSource(api_key=api_key, price_cache=price_cache, poll_interval=interval)

    seed = _env_number("SIM_SEED", int, None)
    logger.info(
        "Market data source: GBM Simulator%s", f" (seed={seed})" if seed is not None else ""
    )
    return SimulatorDataSource(price_cache=price_cache, seed=seed)


def _env_number(name: str, cast: Callable[[str], T], default: T | None) -> T | None:
    """Read a numeric env var. Blank → default. Invalid → ValueError (fail fast)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
```

- This is the only reader of market-data environment variables. Sources take plain arguments.
- An invalid `SIM_SEED` or `MASSIVE_POLL_INTERVAL` raises at startup. If a typo silently fell back to an unseeded run, E2E determinism would break without anyone noticing.
- `load_dotenv(find_dotenv())` must run before this is called (§13.2).

## 10. Ticker tracking: `tracking.py`

PLAN §6 wants one helper that makes the source track exactly watchlist ∪ open positions. The market package owns the reconciliation; the caller supplies the desired set from the database. That keeps the market package DB-agnostic and the logic unit-testable.

`backend/app/market/tracking.py`

```python
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
```

- The `asyncio.Lock` serializes concurrent syncs, e.g. a chat-triggered trade and a manual trade (tested with `asyncio.gather`).
- Create one tracker per app, in the lifespan, not at import time. The lock then belongs to the running loop.
- Call it **after** the DB transaction commits, so it reads committed state.

## 11. SSE stream: `stream.py`

### 11.1 Code

`backend/app/market/stream.py`

```python
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
```

### 11.2 Wire format

```
retry: 1000

data: {"AAPL": {"ticker": "AAPL", "price": 190.01, "previous_price": 190.0, "open_price": 190.0, "timestamp": 1792419923.094037, "change": 0.01, "change_percent": 0.0053, "direction": "up"}, "AMZN": {...}, ...}

data: {...}
```

- The first snapshot is sent on connect (`last_version = -1`).
- After that, an event is sent only when `version` changes: about every 0.5 s with the simulator, at most once per poll with Massive.
- The payload includes held-but-unwatched tickers. The watchlist panel filters by `GET /api/watchlist` (PLAN §6).
- `{}` is sent when the cache becomes empty.
- Each event is one `yield` with no per-event flush cost. One JSON encode serves one client; with many clients a shared encoded payload per version would be the next optimization, but it is unnecessary for a single-user app.

### 11.3 Observed live output

Running the example app (§13.1) under uvicorn with `SIM_SEED=42` and reading the stream for 1.6 s gave the `retry` line followed by three snapshots about 0.5 s apart:

```
$ curl -s -N --max-time 1.6 localhost:8765/api/stream/prices
retry: 1000

data: {"AAPL": {"ticker": "AAPL", "price": 190.01, "previous_price": 190.0, "open_price": 190.0, "timestamp": 1792419923.094037, "change": 0.01, "change_percent": 0.0053, "direction": "up"}, ...
data: {"AAPL": {"ticker": "AAPL", "price": 190.01, "previous_price": 190.01, "open_price": 190.0, "timestamp": 1792419923.6013556, "change": 0.0, "change_percent": 0.0, "direction": "flat"}, ...
data: {"AAPL": {"ticker": "AAPL", "price": 190.04, "previous_price": 190.01, "open_price": 190.0, "timestamp": 1792419924.1128705, "change": 0.03, "change_percent": 0.0158, "direction": "up"}, ...
```

## 12. Package API: `__init__.py`

`backend/app/market/__init__.py`

```python
"""Market data subsystem for FinAlly.

Public API:
    PriceUpdate         - Immutable price snapshot dataclass
    PriceCache          - Thread-safe in-memory price store
    MarketDataSource    - Abstract interface for data providers
    TickerTracker       - Reconciles tracked tickers with watchlist ∪ positions
    create_market_data_source - Factory that selects simulator or Massive
    create_stream_router - FastAPI router factory for SSE endpoint
"""

from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate
from .stream import create_stream_router
from .tracking import SyncResult, TickerTracker

__all__ = [
    "PriceUpdate",
    "PriceCache",
    "MarketDataSource",
    "TickerTracker",
    "SyncResult",
    "create_market_data_source",
    "create_stream_router",
]
```

## 13. Using it from the API layer

### 13.1 Routes, dependencies and validation (runnable example)

The module below is the reference for how routes use the market package. Its in-memory store stands in for the SQLite layer; replace it with real repository calls. It is exercised by `tests/test_example_api.py` (Appendix A) and was run under uvicorn. It shows:

- `normalize_ticker()`: strip, uppercase and regex, once, at the edge (PLAN §8)
- dependencies that read `app.state`
- watchlist add (idempotent, capped at 30), remove (404 if absent), and tracker sync after each change
- `resolve_fill_price()`: PLAN §8's "trading a ticker with no cached price" in both modes
- an app factory that builds a fresh cache and router per app

`backend/app/example_api.py`

```python
"""Integration example: how the API layer uses the market data package.

The in-memory store stands in for the SQLite layer the backend agent will build.
"""

from __future__ import annotations

import contextlib
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from app.market import (
    MarketDataSource,
    PriceCache,
    TickerTracker,
    create_market_data_source,
    create_stream_router,
)

# --- Ticker normalization (PLAN §8): once, at the API layer ---------------------

TICKER_RE = re.compile(r"^[A-Z][A-Z.]{0,5}$")
WATCHLIST_CAP = 30


def normalize_ticker(raw: str) -> str:
    ticker = raw.strip().upper()
    if not TICKER_RE.fullmatch(ticker):
        raise HTTPException(400, f"Invalid ticker: {raw!r}")
    return ticker


# --- Stand-in for the DB layer ------------------------------------------------


@dataclass
class InMemoryStore:
    watchlist: list[str] = field(
        default_factory=lambda: [
            "AAPL",
            "GOOGL",
            "MSFT",
            "AMZN",
            "TSLA",
            "NVDA",
            "META",
            "JPM",
            "V",
            "NFLX",
        ]  # fmt: skip
    )
    held: dict[str, float] = field(default_factory=dict)  # ticker -> quantity

    def tracked_tickers(self) -> set[str]:
        return set(self.watchlist) | set(self.held)


# --- Dependencies ---------------------------------------------------------------


def get_cache(request: Request) -> PriceCache:
    return request.app.state.price_cache


def get_source(request: Request) -> MarketDataSource:
    return request.app.state.market_source


def get_tracker(request: Request) -> TickerTracker:
    return request.app.state.tracker


def get_store(request: Request) -> InMemoryStore:
    return request.app.state.store


async def sync_tracked_tickers(
    tracker: TickerTracker = Depends(get_tracker), store: InMemoryStore = Depends(get_store)
) -> None:
    """The one helper PLAN §6 asks for: call after every watchlist change and trade."""
    await tracker.sync(store.tracked_tickers())


# --- Routes ---------------------------------------------------------------------

router = APIRouter(prefix="/api")


class WatchlistAdd(BaseModel):
    ticker: str


class TradeRequest(BaseModel):
    ticker: str
    side: str
    quantity: float


def watchlist_view(store: InMemoryStore, cache: PriceCache) -> list[dict]:
    return [{"ticker": t, "price": cache.get_price(t)} for t in store.watchlist]


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/watchlist")
async def list_watchlist(
    store: InMemoryStore = Depends(get_store), cache: PriceCache = Depends(get_cache)
) -> list[dict]:
    return watchlist_view(store, cache)


@router.post("/watchlist")
async def add_watchlist(
    body: WatchlistAdd,
    store: InMemoryStore = Depends(get_store),
    cache: PriceCache = Depends(get_cache),
    tracker: TickerTracker = Depends(get_tracker),
) -> list[dict]:
    ticker = normalize_ticker(body.ticker)
    if ticker not in store.watchlist:  # Idempotent
        if len(store.watchlist) >= WATCHLIST_CAP:
            raise HTTPException(400, f"Watchlist is full ({WATCHLIST_CAP} tickers)")
        store.watchlist.append(ticker)
        await tracker.sync(store.tracked_tickers())
    return watchlist_view(store, cache)


@router.delete("/watchlist/{ticker}")
async def remove_watchlist(
    ticker: str,
    store: InMemoryStore = Depends(get_store),
    cache: PriceCache = Depends(get_cache),
    tracker: TickerTracker = Depends(get_tracker),
) -> list[dict]:
    ticker = normalize_ticker(ticker)
    if ticker not in store.watchlist:
        raise HTTPException(404, f"{ticker} is not on the watchlist")
    store.watchlist.remove(ticker)
    await tracker.sync(store.tracked_tickers())  # Held tickers stay tracked
    return watchlist_view(store, cache)


async def resolve_fill_price(
    ticker: str,
    store: InMemoryStore,
    cache: PriceCache,
    source: MarketDataSource,
    tracker: TickerTracker,
) -> float:
    """Price for a market order (PLAN §8 'Trading a ticker with no cached price')."""
    price = cache.get_price(ticker)
    if price is None:
        if ticker not in source.get_tickers():
            if len(store.watchlist) >= WATCHLIST_CAP:
                raise HTTPException(400, f"Watchlist is full ({WATCHLIST_CAP} tickers)")
            store.watchlist.append(ticker)  # Same path as POST /api/watchlist
            await tracker.sync(store.tracked_tickers())  # Simulator seeds the price here
            price = cache.get_price(ticker)
        if price is None:  # Massive: arrives on the next poll
            raise HTTPException(400, f"No price yet for {ticker}, try again shortly")
    return price


@router.post("/portfolio/trade")
async def trade(
    body: TradeRequest,
    store: InMemoryStore = Depends(get_store),
    cache: PriceCache = Depends(get_cache),
    source: MarketDataSource = Depends(get_source),
    tracker: TickerTracker = Depends(get_tracker),
) -> dict:
    ticker = normalize_ticker(body.ticker)
    quantity = round(body.quantity, 4)
    if quantity <= 0:
        raise HTTPException(400, "Quantity must be greater than 0")
    price = await resolve_fill_price(ticker, store, cache, source, tracker)

    # Cash checks, avg_cost and the BEGIN IMMEDIATE transaction live in the
    # portfolio service; only the held-quantity effect is modelled here.
    held = store.held.get(ticker, 0.0)
    if body.side == "sell":
        if quantity > held + 1e-9:
            raise HTTPException(400, f"Insufficient shares: you hold {held:g}")
        held -= quantity
    else:
        held += quantity
    if held < 1e-6:
        store.held.pop(ticker, None)
    else:
        store.held[ticker] = held

    await tracker.sync(store.tracked_tickers())  # After commit: may unsubscribe
    return {"trade": {"ticker": ticker, "side": body.side, "quantity": quantity, "price": price}}


# --- App factory ------------------------------------------------------------------


def create_app(store: InMemoryStore | None = None) -> FastAPI:
    cache = PriceCache()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = store if store is not None else InMemoryStore()
        source = create_market_data_source(cache)
        await source.start(sorted(app.state.store.tracked_tickers()))
        app.state.price_cache = cache
        app.state.market_source = source
        app.state.tracker = TickerTracker(source)
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                await source.stop()

    app = FastAPI(title="FinAlly", lifespan=lifespan)
    app.include_router(create_stream_router(cache))
    app.include_router(router)
    return app
```

### 13.2 Production `app/main.py` shape

With the real DB layer, the lifespan follows PLAN §7's order. DB function names are placeholders for the backend agent's module:

```python
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())  # before anything reads os.environ (no-op in Docker)


def create_app() -> FastAPI:
    cache = PriceCache()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = open_database(db_path())                                 # 1-3: open, create tables, seed
        source = create_market_data_source(cache)
        await source.start(sorted(db.watchlist_tickers() | db.held_tickers()))  # 4
        snapshots = asyncio.create_task(snapshot_loop(db, cache), name="portfolio-snapshots")  # 5
        app.state.db, app.state.price_cache = db, cache
        app.state.market_source, app.state.tracker = source, TickerTracker(source)
        try:
            yield
        finally:
            snapshots.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await snapshots
            await source.stop()

    app = FastAPI(title="FinAlly", lifespan=lifespan)
    app.include_router(create_stream_router(cache))
    app.include_router(api_router)                                     # portfolio, watchlist, chat, health
    app.mount("/", StaticFiles(directory="static", html=True), name="static")  # LAST (PLAN §11)
    return app


app = create_app()   # uvicorn app.main:app --host 0.0.0.0 --port 8000   (one worker)
```

```python
async def sync_tracked_tickers(app) -> None:
    """PLAN §6 helper. Call after every watchlist change and every executed trade."""
    db = app.state.db
    await app.state.tracker.sync(db.watchlist_tickers() | db.held_tickers())
```

### 13.3 Reading prices elsewhere

```python
cache: PriceCache = request.app.state.price_cache

# Portfolio valuation (PLAN §7): no cached price → value at avg_cost
current = cache.get_price(pos.ticker) or pos.avg_cost        # safe: real prices are never 0.0

# record_snapshot(): cash + Σ quantity × current price
total = cash + sum(p.quantity * (cache.get_price(p.ticker) or p.avg_cost) for p in positions)

# Chat context: watchlist with live prices
context = [{"ticker": t, "price": cache.get_price(t)} for t in db.watchlist_tickers_ordered()]

# Full detail (open_price, direction, timestamp) when needed
update = cache.get("AAPL")                                   # PriceUpdate | None
```

Read a trade's fill price **once**, then validate and execute with that same value inside the `BEGIN IMMEDIATE` transaction. Don't re-read the cache mid-transaction.

## 14. Examples

### 14.1 Use the package from a script

```python
import asyncio
from app.market import PriceCache, create_market_data_source

async def main():
    cache = PriceCache()
    source = create_market_data_source(cache)        # simulator unless MASSIVE_API_KEY is set
    await source.start(["AAPL", "NVDA"])
    await source.add_ticker("PYPL")                  # simulator: priced immediately
    print(cache.get("PYPL"))                         # PriceUpdate(ticker='PYPL', price=..., open_price=...)
    await asyncio.sleep(2)
    for t, u in sorted(cache.get_all().items()):
        print(f"{t:5} {u.price:9.2f} {u.direction:4}  since open {(u.price/u.open_price-1)*100:+.2f}%")
    await source.stop()

asyncio.run(main())
```

### 14.2 Reproducible simulator run

```bash
cd backend
SIM_SEED=42 uv run uvicorn app.main:app --port 8000          # same tick sequence every run
```

```python
from app.market.simulator import GBMSimulator
a, b = GBMSimulator(["AAPL", "PYPL"], seed=7), GBMSimulator(["AAPL", "PYPL"], seed=7)
assert [a.step() for _ in range(100)] == [b.step() for _ in range(100)]
```

### 14.3 Real data

```bash
# .env
MASSIVE_API_KEY=your-key
MASSIVE_POLL_INTERVAL=5      # optional; paid plans only poll snapshots
```

The startup log shows which mode was detected:

```
INFO  app.market.factory: Market data source: Massive API (poll 5.0s)
WARN  app.market.massive_client: Massive key has no snapshot access (free plan?) — showing end-of-day prices
INFO  app.market.massive_client: Massive EOD prices from session 2026-10-16
INFO  app.market.massive_client: Massive poller started: 10 tickers, mode=eod
```

### 14.4 Watch the stream

```bash
curl -N http://localhost:8000/api/stream/prices
```

### 14.5 Frontend consumer (TypeScript sketch for the frontend agent)

```ts
type PriceUpdate = {
  ticker: string; price: number; previous_price: number; open_price: number;
  timestamp: number; change: number; change_percent: number; direction: "up" | "down" | "flat";
};

const es = new EventSource("/api/stream/prices");
es.onmessage = (e) => {
  const snapshot: Record<string, PriceUpdate> = JSON.parse(e.data);
  setPrices(snapshot);                       // REPLACE, don't merge (PLAN §6)
};
// Status dot from readyState, not message frequency: OPEN=green, CONNECTING=yellow, CLOSED=red
const status = () => (["yellow", "green", "red"] as const)[es.readyState];
```

## 15. Failure modes

| Situation | What happens | User-visible |
|---|---|---|
| Simulator step raises | Logged with traceback; loop continues on the next tick | Nothing (one missed tick) |
| Massive bad key (401) | Every poll logs `Massive poll failed: … Unknown API Key` | No prices; connection dot green (SSE is fine) |
| Massive free key | One warning, then EOD mode | Static last-session closes, no flashes |
| Massive 429 / 5xx / timeout | Logged; next interval retries; last prices kept | Prices pause |
| Market closed (paid key) | Polls return identical data → no writes | No flashes, quiet stream (expected, PLAN §6) |
| Unknown symbol on Massive | Tracked, never priced | Watchlist shows `null`; trade → 400 "No price yet" |
| Ticker removed mid-request | Result discarded | None |
| Last ticker removed | SSE sends `{}` | Panels empty, as they should be |
| Invalid `SIM_SEED` / `MASSIVE_POLL_INTERVAL` | `ValueError` at startup | Container fails fast with a clear message |
| Two app instances in one process (tests) | Independent caches and routers | None |

## 16. Configuration

| Setting | Where | Default | Notes |
|---|---|---|---|
| `MASSIVE_API_KEY` | env | blank | Non-blank → Massive |
| `MASSIVE_POLL_INTERVAL` | env | `5` | Seconds, snapshot mode. **New** (add to PLAN §5 and `.env.example`). |
| `SIM_SEED` | env | blank | Integer; reproducible simulator |
| `update_interval` | `SimulatorDataSource` | `0.5` s | Change `dt` with it to keep market time = wall time |
| `event_probability` | `SimulatorDataSource` / `GBMSimulator` | `0.001` | `0.0002` for a calmer market |
| `dt` | `GBMSimulator` | `0.5 / 5,896,800` | Multiply by k to compress time (√k livelier ticks) |
| `eod_refresh_interval` | `MassiveDataSource` | `3600` s | Free plan |
| SSE check interval | `_generate_events` | `0.5` s | |
| SSE `retry` | `_generate_events` | `1000` ms | |
| Watchlist cap | API layer | `30` | Simulator rebuilds Cholesky on each add |

## 17. Testing

| File | Tests | Covers |
|---|---|---|
| `test_models.py` | 12 | **Rewritten**: `open_price` required and serialized |
| `test_cache.py` | 13 | Existing, unchanged |
| `test_cache_session.py` | 6 | **New**: open_price sessions, removal version, rounding, 4-thread write test |
| `test_simulator.py` | 19 | Existing, unchanged |
| `test_simulator_seed.py` | 8 | **New**: same seed same path, global-RNG isolation, PD at 30 tickers, σ ≈ 0.22, tech ρ ≈ 0.6, event size |
| `test_simulator_source.py` | 10 | Existing, unchanged |
| `test_massive.py` | 22 | **Rewritten**: real `massive` models; `best_price` levels and units; walk-back; snapshot and EOD modes; in-flight removal; quiet writes |
| `test_contract.py` | 18 | **New**: one suite × {simulator, massive-snapshot, massive-eod} |
| `test_tracking.py` | 5 | **New**: diff, no-op, held-but-unwatched, full-sell unsubscribe, concurrent syncs |
| `test_stream.py` | 5 | **New**: retry + snapshot shape, version gating, `{}` on empty, router factory twice |
| `test_factory.py` | 7 | Existing, unchanged |
| `test_factory_env.py` | 6 | **New**: `SIM_SEED`, `MASSIVE_POLL_INTERVAL`, fail-fast |
| `tests/test_example_api.py` | 8 | **New**: routes through `TestClient` with lifespan |

**139 tests, 97% line coverage of `app/`.** In the old `test_massive.py`, every `MagicMock` snapshot hid the `timestamp` bug; the new file builds fixtures with `TickerSnapshot.from_dict()` from documented JSON. `test_stream.py` drives the generator directly with a fake request, so it needs no ASGI server. `test_example_api.py` needs **`httpx`** (FastAPI's `TestClient`); add it to the `dev` extra.

Run:

```bash
cd backend
uv sync --extra dev
uv run --extra dev pytest -q
uv run --extra dev ruff check app tests
```

## 18. Implementation plan

Work in this order; each step leaves the suite green.

1. **Housekeeping:**
   - Add `httpx` to the `dev` extra.
   - Per PLAN §13, move `rich` to `dev` and add `python-dotenv` (and `litellm`, pinned, for the chat agent).
   - Add `MASSIVE_POLL_INTERVAL=` to `.env.example`.
2. **`models.py` + `cache.py`** with `test_models.py` (rewritten) and `test_cache_session.py`.
3. **`stream.py`** with `test_stream.py`.
4. **`simulator.py`** with `test_simulator_seed.py`.
5. **`massive_client.py`** with the rewritten `test_massive.py`.
6. **`factory.py`** with `test_factory_env.py`.
7. **`tracking.py`, `__init__.py`** with `test_tracking.py` and `test_contract.py`.
8. **API layer**: adapt §13.1 to the real DB layer. Keep `test_example_api.py` as the template for the route tests.
9. Update `backend/CLAUDE.md` (Core Types: `open_price`, `TickerTracker`, env vars) and `planning/MARKET_DATA_SUMMARY.md`.

`market_data_demo.py` uses only `PriceCache` and `SimulatorDataSource` through their public methods, so it needs no change. It gains `SIM_SEED` support for free.

> **Environment note:** in this workspace, `typing_extensions.py` was missing from installed venvs even though its metadata was present, so importing FastAPI failed. That points to a damaged uv cache entry. If the suite fails to collect with `No module named 'typing_extensions'`, run `uv cache clean typing-extensions` and then `uv sync --extra dev --reinstall-package typing-extensions`.

## 19. Decisions beyond PLAN.md

Proposed here; fold them into PLAN.md when accepted:

1. **Massive free plan → EOD mode** (closing prices, hourly refresh), detected automatically. PLAN §6's "free tier: poll every 15 s" is impossible because the free tier has no snapshot access.
2. **Snapshot poll defaults to 5 s**, configurable via the new `MASSIVE_POLL_INTERVAL`. Snapshot access implies a paid plan with no per-minute cap.
3. **`open_price` resets when a ticker is removed and re-added.** Otherwise a re-added simulator ticker (fresh start price) would show a meaningless change-%.
4. **SSE sends `{}`** when nothing is tracked, consistent with replace semantics.
5. **`TickerTracker` in the market package** implements PLAN's `sync_tracked_tickers()`, with the desired set supplied by the caller.
6. **Unchanged Massive data is not rewritten**, so a closed market produces a quiet stream rather than a flood of `flat` events.

---

## Appendix A: test code

All test files that are new or rewritten, verbatim.

<details><summary><code>tests/market/test_models.py</code> (rewritten)</summary>

`backend/tests/market/test_models.py`

```python
"""Tests for PriceUpdate dataclass."""

import pytest

from app.market.models import PriceUpdate


def _update(price: float, previous: float, open_price: float = 190.00) -> PriceUpdate:
    return PriceUpdate(
        ticker="AAPL",
        price=price,
        previous_price=previous,
        open_price=open_price,
        timestamp=1234567890.0,
    )


class TestPriceUpdate:
    """Unit tests for the PriceUpdate model."""

    def test_price_update_creation(self):
        update = _update(190.50, 190.00, open_price=189.00)
        assert update.ticker == "AAPL"
        assert update.price == 190.50
        assert update.previous_price == 190.00
        assert update.open_price == 189.00
        assert update.timestamp == 1234567890.0

    def test_open_price_is_required(self):
        with pytest.raises(TypeError):
            PriceUpdate(ticker="AAPL", price=1.0, previous_price=1.0)  # type: ignore[call-arg]

    def test_change_calculation(self):
        assert _update(190.50, 190.00).change == 0.50

    def test_change_negative(self):
        assert _update(189.50, 190.00).change == -0.50

    def test_change_percent_up(self):
        assert _update(190.00, 100.00).change_percent == 90.0

    def test_change_percent_down(self):
        assert _update(100.00, 200.00).change_percent == -50.0

    def test_change_percent_zero_previous(self):
        assert _update(100.00, 0.00).change_percent == 0.0

    def test_direction_up(self):
        assert _update(191.00, 190.00).direction == "up"

    def test_direction_down(self):
        assert _update(189.00, 190.00).direction == "down"

    def test_direction_flat(self):
        assert _update(190.00, 190.00).direction == "flat"

    def test_to_dict(self):
        result = _update(190.50, 190.00, open_price=189.00).to_dict()
        assert result == {
            "ticker": "AAPL",
            "price": 190.50,
            "previous_price": 190.00,
            "open_price": 189.00,
            "timestamp": 1234567890.0,
            "change": 0.50,
            "change_percent": 0.2632,  # (0.50 / 190.00) * 100
            "direction": "up",
        }

    def test_immutability(self):
        update = _update(190.50, 190.00)
        with pytest.raises(AttributeError):
            update.price = 200.00  # type: ignore[misc]
```

</details>

<details><summary><code>tests/market/test_cache_session.py</code></summary>

`backend/tests/market/test_cache_session.py`

```python
"""PriceCache: open_price sessions and version semantics."""

import threading

from app.market.cache import PriceCache


def test_open_price_set_on_first_update_and_carried_forward():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    cache.update("AAPL", 191.25)
    update = cache.update("AAPL", 189.50)
    assert update.open_price == 190.00
    assert update.previous_price == 191.25


def test_remove_resets_session():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    cache.update("AAPL", 195.00)
    cache.remove("AAPL")
    assert cache.update("AAPL", 250.00).open_price == 250.00


def test_remove_bumps_version_only_when_present():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    v = cache.version
    cache.remove("AAPL")
    assert cache.version == v + 1
    cache.remove("AAPL")  # Already gone: no-op
    assert cache.version == v + 1


def test_explicit_zero_timestamp_is_kept():
    assert PriceCache().update("AAPL", 1.0, timestamp=0.0).timestamp == 0.0


def test_prices_rounded_to_cents():
    assert PriceCache().update("AAPL", 190.12345).price == 190.12


def test_concurrent_writers_never_lose_a_version():
    cache = PriceCache()

    def writer(ticker: str) -> None:
        for i in range(2_000):
            cache.update(ticker, 100 + i / 100)

    threads = [threading.Thread(target=writer, args=(t,)) for t in ("A", "B", "C", "D")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert cache.version == 8_000
    assert len(cache) == 4
```

</details>

<details><summary><code>tests/market/test_simulator_seed.py</code></summary>

`backend/tests/market/test_simulator_seed.py`

```python
"""GBMSimulator: SIM_SEED reproducibility and statistical properties."""

import math
import random

import numpy as np

from app.market.seed_prices import SEED_PRICES
from app.market.simulator import GBMSimulator


def _log_returns(sim: GBMSimulator, tickers: list[str], steps: int) -> np.ndarray:
    """Unrounded log-returns (cent rounding would distort tiny per-tick moves)."""
    path = [[sim._prices[t] for t in tickers]]
    for _ in range(steps):
        sim.step()
        path.append([sim._prices[t] for t in tickers])
    return np.diff(np.log(np.array(path)), axis=0)


def test_same_seed_same_path():
    a = GBMSimulator(["AAPL", "PYPL"], seed=7)
    b = GBMSimulator(["AAPL", "PYPL"], seed=7)
    assert a.get_price("PYPL") == b.get_price("PYPL")  # Random start price reproducible
    assert [a.step() for _ in range(100)] == [b.step() for _ in range(100)]


def test_different_seed_different_path():
    a, b = GBMSimulator(["AAPL"], seed=1), GBMSimulator(["AAPL"], seed=2)
    assert [a.step() for _ in range(20)] != [b.step() for _ in range(20)]


def test_seed_isolated_from_global_random():
    a = GBMSimulator(["AAPL"], seed=7)
    random.random()
    np.random.standard_normal(5)  # Other code using the global RNGs
    b = GBMSimulator(["AAPL"], seed=7)
    assert a.step() == b.step()


def test_known_tickers_consume_no_randomness_on_add():
    a = GBMSimulator(["AAPL"], seed=7)
    b = GBMSimulator(["AAPL"], seed=7)
    b.add_ticker("MSFT")  # Seed price: no RNG draw
    b.remove_ticker("MSFT")
    assert a.step() == b.step()


def test_correlation_matrix_pd_at_watchlist_cap():
    sim = GBMSimulator(list(SEED_PRICES) + [f"X{i}" for i in range(20)])  # 30 tickers
    assert sim._cholesky is not None  # np.linalg.cholesky would have raised


def test_log_return_volatility_matches_sigma():
    sim = GBMSimulator(["AAPL"], event_probability=0.0, seed=3)
    r = _log_returns(sim, ["AAPL"], 20_000)[:, 0]
    assert abs(r.std() / math.sqrt(sim._dt) - 0.22) < 0.01  # ≈ annual sigma


def test_tech_stocks_correlate():
    sim = GBMSimulator(["AAPL", "MSFT", "JPM"], event_probability=0.0, seed=5)
    c = np.corrcoef(_log_returns(sim, ["AAPL", "MSFT", "JPM"], 5_000).T)
    assert 0.5 < c[0, 1] < 0.7  # Tech pair: 0.6
    assert 0.2 < c[0, 2] < 0.4  # Cross-sector: 0.3


def test_events_fire_at_configured_rate():
    sim = GBMSimulator(["AAPL"], event_probability=1.0, seed=9)
    before = sim._prices["AAPL"]
    sim.step()
    move = abs(sim._prices["AAPL"] / before - 1)
    assert 0.019 < move < 0.051  # One 2–5% shock plus a tiny GBM move
```

</details>

<details><summary><code>tests/market/test_massive.py</code> (rewritten)</summary>

`backend/tests/market/test_massive.py`

```python
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
```

</details>

<details><summary><code>tests/market/test_contract.py</code></summary>

`backend/tests/market/test_contract.py`

```python
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
```

</details>

<details><summary><code>tests/market/test_tracking.py</code></summary>

`backend/tests/market/test_tracking.py`

```python
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
```

</details>

<details><summary><code>tests/market/test_stream.py</code></summary>

`backend/tests/market/test_stream.py`

```python
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
```

</details>

<details><summary><code>tests/market/test_factory_env.py</code></summary>

`backend/tests/market/test_factory_env.py`

```python
"""Factory: SIM_SEED and MASSIVE_POLL_INTERVAL handling."""

import os
from unittest.mock import patch

import pytest

from app.market.cache import PriceCache
from app.market.factory import create_market_data_source
from app.market.massive_client import MassiveDataSource
from app.market.simulator import SimulatorDataSource


def build(env: dict[str, str]):
    with patch.dict(os.environ, env, clear=True):
        return create_market_data_source(PriceCache())


def test_sim_seed_passed_to_simulator():
    source = build({"SIM_SEED": "42"})
    assert isinstance(source, SimulatorDataSource) and source._seed == 42


def test_blank_sim_seed_is_unseeded():
    assert build({"SIM_SEED": "  "})._seed is None


def test_invalid_sim_seed_fails_fast():
    with pytest.raises(ValueError, match="SIM_SEED"):
        build({"SIM_SEED": "abc"})


def test_massive_default_poll_interval():
    source = build({"MASSIVE_API_KEY": "k"})
    assert isinstance(source, MassiveDataSource) and source._interval == 5.0


def test_massive_poll_interval_override():
    assert build({"MASSIVE_API_KEY": "k", "MASSIVE_POLL_INTERVAL": "2.5"})._interval == 2.5


def test_sim_seed_ignored_when_massive_selected():
    assert isinstance(build({"MASSIVE_API_KEY": "k", "SIM_SEED": "abc"}), MassiveDataSource)
```

</details>

<details><summary><code>tests/test_example_api.py</code></summary>

`backend/tests/test_example_api.py`

```python
"""End-to-end check of the integration example through FastAPI's TestClient."""

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.example_api import InMemoryStore, create_app


@pytest.fixture
def client():
    with patch.dict(os.environ, {"SIM_SEED": "42"}, clear=True):  # Simulator
        with TestClient(create_app(InMemoryStore(watchlist=["AAPL", "MSFT"]))) as c:
            yield c


def test_watchlist_has_prices_on_first_render(client):
    body = client.get("/api/watchlist").json()
    assert [w["ticker"] for w in body] == ["AAPL", "MSFT"]
    assert all(isinstance(w["price"], float) for w in body)


def test_add_is_normalized_idempotent_and_priced(client):
    client.post("/api/watchlist", json={"ticker": " pypl "})
    body = client.post("/api/watchlist", json={"ticker": "PYPL"}).json()
    assert [w["ticker"] for w in body].count("PYPL") == 1
    assert body[-1]["price"] is not None


def test_invalid_ticker_is_400(client):
    r = client.post("/api/watchlist", json={"ticker": "123!"})
    assert r.status_code == 400 and "Invalid ticker" in r.json()["detail"]


def test_remove_unknown_is_404(client):
    assert client.delete("/api/watchlist/NFLX").status_code == 404


def test_trade_unwatched_ticker_auto_adds_and_fills(client):
    r = client.post("/api/portfolio/trade", json={"ticker": "nvda", "side": "buy", "quantity": 2})
    assert r.status_code == 200 and r.json()["trade"]["price"] > 0
    assert "NVDA" in [w["ticker"] for w in client.get("/api/watchlist").json()]


def test_held_ticker_stays_priced_after_watchlist_removal(client):
    client.post("/api/portfolio/trade", json={"ticker": "MSFT", "side": "buy", "quantity": 1})
    client.delete("/api/watchlist/MSFT")
    cache = client.app.state.price_cache
    assert cache.get_price("MSFT") is not None  # Still tracked: held
    client.post("/api/portfolio/trade", json={"ticker": "MSFT", "side": "sell", "quantity": 1})
    assert cache.get("MSFT") is None  # Full sell of unwatched ticker unsubscribes


def test_oversell_is_400(client):
    r = client.post("/api/portfolio/trade", json={"ticker": "AAPL", "side": "sell", "quantity": 1})
    assert r.status_code == 400 and r.json()["detail"] == "Insufficient shares: you hold 0"


def test_routes_registered_once_per_app():
    for _ in range(2):
        paths = [r.path for r in create_app().routes]
        assert paths.count("/api/stream/prices") == 1
```

</details>
