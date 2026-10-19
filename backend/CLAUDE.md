# Backend — Developer Guide

## Project Setup

```bash
cd backend
uv sync --extra dev   # Install all dependencies including test/lint tools
```

## Market Data API

The market data subsystem lives in `app/market/`. Use these imports:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, TickerTracker, create_market_data_source
```

### Core Types

- **`PriceUpdate`** — Immutable dataclass: `ticker`, `price`, `previous_price`, `open_price` (first price seen since the ticker was added), `timestamp` (Unix seconds), plus properties `change`, `change_percent`, `direction` ("up"/"down"/"flat"), and `to_dict()` for JSON serialization.

- **`PriceCache`** — Thread-safe in-memory store. Key methods:
  - `update(ticker, price, timestamp=None) -> PriceUpdate`
  - `get(ticker) -> PriceUpdate | None`
  - `get_price(ticker) -> float | None`
  - `get_all() -> dict[str, PriceUpdate]`
  - `remove(ticker)` — bumps `version` if the ticker was present; a re-added ticker gets a fresh `open_price`
  - `version` property — monotonic counter, increments on every update and removal (for SSE change detection)
  - Gotcha: an empty cache is falsy (`__len__`), so write `cache if cache is not None else PriceCache()`, never `cache or PriceCache()`

- **`MarketDataSource`** — Abstract interface implemented by `SimulatorDataSource` and `MassiveDataSource`. Lifecycle: `start(tickers)` -> `add_ticker()` / `remove_ticker()` -> `stop()`.

- **`create_market_data_source(cache)`** — Factory. Returns `MassiveDataSource` if `MASSIVE_API_KEY` is set (poll interval from `MASSIVE_POLL_INTERVAL`, default 5s), otherwise `SimulatorDataSource` (seeded by `SIM_SEED` if set). Invalid numbers fail fast with `ValueError`.
  - Simulator: `add_ticker()` seeds the cache immediately.
  - Massive: the first poll detects the plan. Paid plans use snapshots; the free plan (no snapshot access) falls back to the last session's closing prices, refreshed hourly. In snapshot mode `add_ticker()` does not seed; the price arrives on the next poll.
  - Sources take tickers as given; normalize (`strip().upper()`, `^[A-Z][A-Z.]{0,5}$`) at the API layer.

- **`TickerTracker(source)`** — `await tracker.sync(watchlist | held)` adds/removes the difference (serialized by an `asyncio.Lock`). Call it after every watchlist change and every trade. This is PLAN.md's `sync_tracked_tickers()`.

### SSE Streaming

```python
from app.market import create_stream_router

router = create_stream_router(price_cache)  # New APIRouter per call
# Endpoint: GET /api/stream/prices (text/event-stream)
# Emits a full snapshot {ticker: PriceUpdate.to_dict()} only when cache.version changes; `{}` when empty
```

### Seed Data

Default tickers: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX. Seed prices and per-ticker volatility/drift params are in `app/market/seed_prices.py`.

## Running Tests

```bash
uv run --extra dev pytest -v              # All tests
uv run --extra dev pytest --cov=app       # With coverage
uv run --extra dev ruff check app/ tests/ # Lint
```

## Integration Example

`app/example_api.py` shows the API layer wired to the market package (ticker validation, watchlist add/remove, trading an unpriced ticker, `TickerTracker`), with an in-memory store standing in for SQLite. See `planning/archive/MARKET_DATA_DESIGN.md` §13 for the production `main.py` shape.

```bash
SIM_SEED=42 uv run uvicorn app.example_api:create_app --factory --port 8000
curl -N http://localhost:8000/api/stream/prices
```

## Demo

```bash
uv run --extra dev market_data_demo.py   # Live terminal dashboard (rich is a dev dependency)
```
