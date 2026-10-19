# Market Data Backend — Summary

**Status:** Complete, tested, reviewed. Updated 2026-10-20 with the PLAN.md §13 / REVIEW.md fixes (see "Second Round" below). Full design and code: `planning/MARKET_DATA_DESIGN.md`.

## What Was Built

A complete market data subsystem in `backend/app/market/` (9 modules, ~450 statements) providing live price simulation and real market data via a unified interface.

### Architecture

```
MarketDataSource (ABC)
├── SimulatorDataSource  →  GBM simulator (default, no API key needed)
└── MassiveDataSource    →  Massive (Polygon.io) REST poller (when MASSIVE_API_KEY set)
                             snapshot mode (paid) or end-of-day mode (free), auto-detected
        │
        ▼
   PriceCache (thread-safe, in-memory)
        │
        ├──→ SSE stream endpoint (/api/stream/prices)
        ├──→ Portfolio valuation
        └──→ Trade execution
```

### Modules

| File | Purpose |
|------|---------|
| `models.py` | `PriceUpdate` — immutable frozen dataclass (ticker, price, previous_price, open_price, timestamp, change, direction) |
| `interface.py` | `MarketDataSource` — abstract base class defining `start/stop/add_ticker/remove_ticker/get_tickers` |
| `cache.py` | `PriceCache` — thread-safe price store with version counter (bumped on update and removal) for SSE change detection |
| `seed_prices.py` | Realistic seed prices, per-ticker GBM params (drift/volatility), correlation groups |
| `simulator.py` | `GBMSimulator` (Geometric Brownian Motion with Cholesky-correlated moves) + `SimulatorDataSource` |
| `massive_client.py` | `MassiveDataSource` — REST polling client via the `massive` package; snapshot or EOD (grouped daily) mode |
| `factory.py` | `create_market_data_source()` — selects simulator or Massive from `MASSIVE_API_KEY`; reads `SIM_SEED`, `MASSIVE_POLL_INTERVAL` |
| `tracking.py` | `TickerTracker` — `sync(watchlist ∪ held)` adds/removes the difference (PLAN's `sync_tracked_tickers()`) |
| `stream.py` | `create_stream_router()` — FastAPI SSE endpoint factory (new router per call) using version-based change detection |

`backend/app/example_api.py` is a tested integration example of the API layer (validation, watchlist, trades, tracking).

### Key Design Decisions

- **Strategy pattern** — both data sources implement the same ABC; downstream code is source-agnostic
- **PriceCache as single point of truth** — producers write, consumers read; no direct coupling
- **GBM with correlated moves** — Cholesky decomposition of sector-based correlation matrix; tech stocks correlate at 0.6, finance at 0.5, cross-sector at 0.3
- **Random shock events** — ~0.1% chance per tick per ticker of a 2-5% move for visual drama
- **SSE over WebSockets** — simpler, one-way push, universal browser support

## Test Suite

**139 tests, all passing.** 13 modules in `backend/tests/`. Overall coverage 97%; ruff clean.

| Module | Tests | Covers |
|--------|-------|--------|
| market/test_models.py | 12 | `PriceUpdate`, `open_price`, `to_dict()` |
| market/test_cache.py | 13 | `PriceCache` basics |
| market/test_cache_session.py | 6 | `open_price` lifetime, version on removal |
| market/test_simulator.py | 19 | GBM math, correlation, events |
| market/test_simulator_source.py | 10 | `SimulatorDataSource` integration |
| market/test_simulator_seed.py | 8 | `SIM_SEED` reproducibility |
| market/test_factory.py | 7 | source selection |
| market/test_factory_env.py | 6 | env parsing, fail-fast on bad values |
| market/test_massive.py | 22 | real `massive` models: snapshot/EOD modes, walk-back, races |
| market/test_tracking.py | 5 | `TickerTracker.sync` |
| market/test_stream.py | 5 | SSE wire format, router per call |
| market/test_contract.py | 18 | both sources against the `MarketDataSource` contract |
| test_example_api.py | 8 | API-layer integration via `TestClient` |

## Code Review & Fixes Applied

A comprehensive code review identified 7 issues. All were resolved:

1. **pyproject.toml build config** — added `[tool.hatch.build.targets.wheel] packages = ["app"]`
2. **Lazy imports removed** — `massive` is a core dependency; imports moved to top level
3. **SSE return type fixed** — `_generate_events` annotated as `AsyncGenerator[str, None]`
4. **Public `get_tickers()`** — added to `GBMSimulator` to avoid private attribute access
5. **Correlation constants cleaned up** — removed unused `DEFAULT_CORR`, consolidated into `CROSS_GROUP_CORR`
6. **Unused test imports removed** — `pytest`, `math`, `asyncio` cleaned from 4 test files
7. **Massive test mocks fixed** — `source._client` set in tests, patches target correct names

## Second Round (2026-10-20)

Implements the market data items from `REVIEW.md` / PLAN.md §13 and the findings in `MARKET_DATA_DESIGN.md`:

1. **`open_price`** in the cache and `to_dict()` (B2); resets when a ticker is removed and re-added
2. **`PriceCache.remove()` bumps `version`** (M2)
3. **`create_stream_router()` builds a new router per call** (M3); empty cache streams `{}`
4. **`SIM_SEED`** seeds private `random.Random` / `numpy` generators (T1)
5. **Watchlist ∪ positions tracking** via `TickerTracker` (C3)
6. **Massive `last_trade.timestamp` bug fixed** — the real attribute is `sip_timestamp` (ns); the old code raised for every ticker. Tests now use real `massive` models instead of `MagicMock`
7. **Massive free plan supported** — it has no snapshot access, so the source falls back to end-of-day prices (grouped daily, hourly refresh, weekend/holiday walk-back) (M1)
8. **No ticker uppercasing in sources** — normalization is the API layer's job
9. **Dependencies** — `litellm` (pinned) and `python-dotenv` added; `rich` and `httpx` in the `dev` extra (M4, L2); `db/.gitkeep` and `db/*.db` ignores (K2)

## Demo

A Rich terminal demo is available at `backend/market_data_demo.py`:

```bash
cd backend
uv run --extra dev market_data_demo.py
```

Displays a live-updating dashboard with all 10 tickers, sparklines, color-coded direction arrows, and an event log for notable price moves. Runs 60 seconds or until Ctrl+C.

## Usage for Downstream Code

```python
from app.market import PriceCache, TickerTracker, create_market_data_source

# Startup
cache = PriceCache()
source = create_market_data_source(cache)  # Reads MASSIVE_API_KEY
await source.start(["AAPL", "GOOGL", "MSFT", ...])

# Read prices
update = cache.get("AAPL")          # PriceUpdate or None
price = cache.get_price("AAPL")     # float or None
all_prices = cache.get_all()        # dict[str, PriceUpdate]

# Dynamic watchlist: reconcile with watchlist ∪ held after every change or trade
tracker = TickerTracker(source)
await tracker.sync({"AAPL", "TSLA"} | held_tickers)

# Shutdown
await source.stop()
```
