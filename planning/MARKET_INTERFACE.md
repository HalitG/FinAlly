# Market Data Interface

The unified Python API FinAlly uses for stock prices. It uses Massive when `MASSIVE_API_KEY` is set and the built-in simulator otherwise. Everything downstream (SSE, portfolio valuation, trades, the LLM context) reads one shared `PriceCache` and never knows which source is running.

The design builds on the code already in `backend/app/market/`. It keeps the public API (`PriceCache`, `PriceUpdate`, `MarketDataSource`, `create_market_data_source`, `create_stream_router`) and specifies the changes that PLAN.md §13 and the Massive research (MASSIVE_API.md) require. §11 is the checklist of changes. Simulator internals are in MARKET_SIMULATOR.md.

---

## 1. Principles

1. **Producers write, consumers read.** Exactly one data source writes to the `PriceCache`. Nothing calls the source to *get* a price; the source only receives *which tickers to track*.
2. **One interface, two implementations** (strategy pattern), chosen once at startup by a factory.
3. **The API layer normalizes tickers.** It applies `strip().upper()` and the regex `^[A-Z][A-Z.]{0,5}$` before a ticker reaches the source or the DB (PLAN §8). Sources assume clean input and do no normalization.
4. **Tracked set = watchlist ∪ open positions.** One helper, `sync_tracked_tickers()`, reconciles it with the source (§8.2).
5. **Never crash the app over market data.** Source errors are logged and the next cycle retries. Missing prices are a normal state that callers handle (`None`).
6. **Single process.** The cache, source and snapshot task are in-process state, so the app runs with one uvicorn worker (PLAN §3).

## 2. Architecture

```
                    create_market_data_source(cache)          reads MASSIVE_API_KEY, SIM_SEED
                                  │
                ┌─────────────────┴──────────────────┐
                ▼                                    ▼
      SimulatorDataSource                    MassiveDataSource
      GBM, ticks every 0.5 s                 mode auto-detected on first poll:
      seeds price on add_ticker              • SNAPSHOT (paid plans) every 5 s
                │                            • EOD (free plan) grouped daily, hourly
                │                                    │
                └──────────── write ─────────────────┘
                                  ▼
                   PriceCache (thread-safe, versioned)
                   ticker → PriceUpdate{price, previous_price, open_price, timestamp}
                                  │ read
       ┌──────────────┬───────────┴──────┬────────────────────┬──────────────────┐
       ▼              ▼                  ▼                    ▼                  ▼
  SSE /api/stream  GET /api/portfolio  POST trade         record_snapshot()   LLM context
  (version-gated)  (valuation)         (fill price)       (every 30 s)        (chat)
```

## 3. Module layout

```
backend/app/market/
├── __init__.py         public exports (unchanged)
├── models.py           PriceUpdate                      ← add open_price
├── cache.py            PriceCache                       ← open_price, remove() bumps version
├── interface.py        MarketDataSource ABC             ← docstring contract only
├── factory.py          create_market_data_source()      ← SIM_SEED, MASSIVE_POLL_INTERVAL
├── simulator.py        GBMSimulator, SimulatorDataSource ← seeded RNGs (MARKET_SIMULATOR.md)
├── seed_prices.py      seed prices, GBM params, correlation groups (unchanged)
├── massive_client.py   MassiveDataSource                ← rewritten poll logic (§6)
└── stream.py           create_stream_router()           ← router built inside the factory
```

Downstream code imports only from the package:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source, create_stream_router
```

---

## 4. Data model and cache

### 4.1 `PriceUpdate` (`models.py`)

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float          # price at the previous cache write (tick-to-tick)
    open_price: float              # first price the cache saw for this ticker this session
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        if self.price > self.previous_price:
            return "up"
        if self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE. Shape is fixed by PLAN §6."""
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

`open_price` has no default, so every construction must pass it. Tests that build `PriceUpdate` directly need updating. Change since open (`(price - open_price) / open_price * 100`) is computed by the frontend, not here.

### 4.2 `PriceCache` (`cache.py`)

```python
class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker.

    Writer: the one active MarketDataSource.
    Readers: SSE stream, portfolio valuation, trade execution, snapshots, LLM context.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version = 0  # bumped on every update AND every removal

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
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

    def remove(self, ticker: str) -> None:
        with self._lock:
            if self._prices.pop(ticker, None) is not None:
                self._version += 1   # so SSE emits a snapshot without the ticker

    # get(), get_price(), get_all(), version, __len__, __contains__: unchanged
```

Design notes:

- **`open_price` travels inside `PriceUpdate`**, carried forward from the previous entry. It needs no second dict and cannot go out of sync.
- **Removal drops `open_price`.** A ticker that is removed and later re-added starts a fresh session. This clarifies PLAN §6 ("first price since process start"): the simulator restarts a re-added ticker from its seed price, or from a new random price for unknown tickers. Keeping the old open would produce a meaningless change-%, possibly hundreds of percent.
- `remove()` bumps `version` only when something was actually removed, so a no-op removal doesn't trigger an SSE event.
- `timestamp=None` is checked with `is None`, not `or`, so a legitimate `0.0` is not replaced.
- Reads of `version` without the lock are safe. It is a single int read, and SSE polls it every 500 ms.

---

## 5. The `MarketDataSource` contract (`interface.py`)

The ABC is unchanged. This is the contract both implementations must meet, and the contract tests in §10 enforce it.

```python
class MarketDataSource(ABC):
    @abstractmethod
    async def start(self, tickers: list[str]) -> None: ...
    @abstractmethod
    async def stop(self) -> None: ...
    @abstractmethod
    async def add_ticker(self, ticker: str) -> None: ...
    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None: ...
    @abstractmethod
    def get_tickers(self) -> list[str]: ...
```

| Method | Contract |
|---|---|
| `start(tickers)` | Called once. Begins a background task that writes to the cache. Does whatever first fetch it can before returning, so the cache is warm, and **never raises** for data errors (it logs them). Input is already normalized. |
| `stop()` | Cancels the background task. Idempotent. No cache writes after it returns. |
| `add_ticker(t)` | Idempotent. Adds `t` to the tracked set. **Simulator:** the price is in the cache when the call returns. **Massive SNAPSHOT:** no price until the next poll (≤ 5 s). **Massive EOD:** the price is written immediately if the ticker is in the stored daily bars. |
| `remove_ticker(t)` | Idempotent. Removes `t` from the tracked set **and** calls `cache.remove(t)`, which bumps `version`. No write for `t` may happen afterwards, even from a request already in flight. |
| `get_tickers()` | Returns a copy of the tracked set. `sync_tracked_tickers()` diffs against this. |

Callers must treat "ticker tracked but no cached price" as normal (PLAN §7 valuation fallback, §8 "No price yet").

---

## 6. `MassiveDataSource` (`massive_client.py`)

### 6.1 Modes

Massive entitlements decide what is possible (MASSIVE_API.md §2):

| Mode | Chosen when | Endpoint | Cadence | Calls |
|---|---|---|---|---|
| `SNAPSHOT` | First snapshot call succeeds (Starter, Developer, Advanced…) | `get_snapshot_all(tickers=…)` | `poll_interval`, default **5 s** | 1 per poll |
| `EOD` | First snapshot call returns `NOT_AUTHORIZED` (free Basic plan) | `get_grouped_daily_aggs(date)` with walk-back | `eod_refresh_interval`, default **1 h** | ≤ 4 per refresh |

Detection costs nothing extra, because the first regular poll is the probe. On a free key, startup spends 1 (probe) + ≤ 4 (walk-back) = **≤ 5 calls**, which is exactly the free per-minute budget. After an hour, the refresh costs ≤ 4. Once the source is in EOD mode it stays there until restart.

EOD-mode prices don't move intraday, so the UI shows a static board. The app should log one clear warning: *"Massive key has no snapshot access (free plan?) — showing end-of-day prices"*.

### 6.2 Price and timestamp extraction

`last_trade` is `None` on Starter. Its timestamp field is `sip_timestamp` in nanoseconds. Snapshot fields are zero between 3:30 and 4:00 AM ET. Hence:

```python
def best_price(snap: TickerSnapshot) -> tuple[float, float] | None:
    """Best available (price, unix_seconds) from a snapshot, or None.

    last trade (Developer+) → latest minute bar → today's bar → previous close.
    Zero/missing values are skipped.
    """
    lt = snap.last_trade
    if lt and lt.price:
        return lt.price, _ns_to_s(lt.sip_timestamp or snap.updated)
    if snap.min and snap.min.close:
        ts = snap.min.timestamp / 1_000 if snap.min.timestamp else _ns_to_s(snap.updated)
        return snap.min.close, ts
    for bar in (snap.day, snap.prev_day):
        if bar and bar.close:
            return bar.close, _ns_to_s(snap.updated)
    return None


def _ns_to_s(ns: int | None) -> float:
    return ns / 1_000_000_000 if ns else time.time()
```

`best_price()` and `latest_grouped_daily()` (MASSIVE_API.md §7.2) are module-level pure functions, so they can be unit-tested without a source or event loop.

### 6.3 Implementation

```python
import asyncio
import contextlib
import logging
import time
from datetime import date
from enum import Enum

from massive import RESTClient
from massive.exceptions import BadResponse
from massive.rest.models import GroupedDailyAgg, SnapshotMarketType, TickerSnapshot

from .cache import PriceCache
from .interface import MarketDataSource

# best_price() from §6.2 and latest_grouped_daily() from MASSIVE_API.md §7.2 live here too.


class MassiveMode(str, Enum):
    SNAPSHOT = "snapshot"   # paid plans: 15-min delayed or real-time snapshots
    EOD = "eod"             # free plan: last session's closes from grouped daily bars


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API."""

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 5.0,
        eod_refresh_interval: float = 3600.0,
        client: RESTClient | None = None,          # injectable for tests
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._eod_interval = eod_refresh_interval
        self._client = client
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._mode: MassiveMode | None = None       # decided by the first poll
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
        await self._poll_once()                     # warm cache + detect mode
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def add_ticker(self, ticker: str) -> None:
        if ticker in self._tickers:
            return
        self._tickers.append(ticker)
        if self._mode is MassiveMode.EOD:
            self._write_eod(ticker)                 # data already in memory, no API call

    async def remove_ticker(self, ticker: str) -> None:
        self._tickers = [t for t in self._tickers if t != ticker]
        self._last_written.pop(ticker, None)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- internals ---

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._eod_interval if self._mode is MassiveMode.EOD else self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        try:
            if self._mode is MassiveMode.EOD:
                await self._refresh_eod()
            elif self._tickers:
                await self._poll_snapshot()
        except Exception as e:                      # 401, 429, network, timeouts...
            logger.error("Massive poll failed: %s", e)

    async def _poll_snapshot(self) -> None:
        requested = list(self._tickers)             # copy: list may change during the call
        try:
            snaps = await asyncio.to_thread(
                self._client.get_snapshot_all, market_type=SnapshotMarketType.STOCKS, tickers=requested
            )
        except BadResponse as e:
            if "NOT_AUTHORIZED" not in str(e):
                raise
            logger.warning("Massive key has no snapshot access (free plan?) — showing end-of-day prices")
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
        for ticker in list(self._tickers):
            self._write_eod(ticker)

    def _write_eod(self, ticker: str) -> None:
        bar = self._eod_bars.get(ticker)
        if bar:
            self._write(ticker, bar.close, bar.timestamp / 1_000 if bar.timestamp else time.time())

    def _write(self, ticker: str, price: float, timestamp: float) -> None:
        if ticker not in self._tickers:             # removed while a request was in flight
            return
        if self._last_written.get(ticker) == (price, timestamp):
            return                                  # nothing new (market closed / EOD refresh)
        self._last_written[ticker] = (price, timestamp)
        self._cache.update(ticker=ticker, price=price, timestamp=timestamp)
```

Behavior worth knowing:

- **Unchanged data is not rewritten.** When the market is closed, each poll returns the same price and timestamp. Skipping the write keeps `version` still, so the SSE stream goes quiet, as PLAN §6 says it should. It also keeps the last real `direction` instead of flattening it.
- **Unknown symbols.** The v2 snapshot leaves out tickers it doesn't know, and grouped daily won't contain them. Such a ticker stays tracked with no price. The watchlist shows `null`, and a trade gets `400 No price yet for XYZ, try again shortly` (PLAN §8). This is acceptable. Validating symbols against `/v3/reference/tickers` would be a later enhancement.
- **Ticker removed mid-request:** `_write()` drops results for tickers no longer tracked. All of this runs on the event loop thread, so the check is race-free.
- **Startup latency:** with `retries=0` and the default 10 s connect and read timeouts, a dead network delays `start()` by ≤ ~20 s, after which the app runs without prices. The poller keeps trying.

---

## 7. Factory (`factory.py`)

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Select the source from the environment. Returns an unstarted source.

    MASSIVE_API_KEY set and non-blank → MassiveDataSource
    otherwise                         → SimulatorDataSource (seeded if SIM_SEED is set)
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        interval = _env_number("MASSIVE_POLL_INTERVAL", float, default=5.0)
        logger.info("Market data source: Massive API (poll %.1fs)", interval)
        return MassiveDataSource(api_key=api_key, price_cache=price_cache, poll_interval=interval)

    seed = _env_number("SIM_SEED", int, default=None)
    logger.info("Market data source: GBM simulator%s", f" (seed={seed})" if seed is not None else "")
    return SimulatorDataSource(price_cache=price_cache, seed=seed)


def _env_number(name, cast, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
```

- **Invalid `SIM_SEED` fails fast** at startup. If a typo silently fell back to an unseeded run, the E2E tests would become nondeterministic without anyone noticing.
- `MASSIVE_POLL_INTERVAL` is a **new optional variable** (§11). In snapshot mode the key is necessarily on a paid plan with no per-minute cap, so 5 s is a safe default. Real-time Advanced users may go to 2 s.
- The factory is the **only** place that reads market-data environment variables. Sources take plain constructor arguments.
- `load_dotenv(find_dotenv())` must run **before** the factory is called. Run it at the top of `app/main.py` so `os.environ` is populated. (Massive's own env default is read at import time, but FinAlly never relies on it; see MASSIVE_API.md §1.)

---

## 8. Integration with the rest of the backend

### 8.1 App construction and lifespan (`app/main.py`)

```python
load_dotenv(find_dotenv())


def create_app() -> FastAPI:
    cache = PriceCache()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = open_database(db_path())                   # §7 steps 1-3: create, migrate, seed
        source = create_market_data_source(cache)
        await source.start(sorted(tracked_tickers(db))) # step 4: watchlist ∪ positions
        snapshots = asyncio.create_task(snapshot_loop(db, cache), name="portfolio-snapshots")  # step 5
        app.state.db, app.state.price_cache, app.state.market = db, cache, source
        try:
            yield
        finally:
            snapshots.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await snapshots
            await source.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(create_stream_router(cache))
    # ... portfolio, watchlist, chat, health routers ...
    app.mount("/", StaticFiles(directory="static", html=True), name="static")  # last
    return app


app = create_app()
```

The function names for the DB layer (`open_database`, `tracked_tickers`, `snapshot_loop`) are illustrative; the backend agent owns that module. The cache is created in `create_app()` rather than in the lifespan because the SSE router needs it at build time. Building the app twice (in tests) gives two independent caches and routers, which is why `create_stream_router()` must build its `APIRouter` internally (§9).

### 8.2 `sync_tracked_tickers()`

There is one helper, called after every watchlist change and every executed trade (PLAN §6):

```python
_sync_lock = asyncio.Lock()


async def sync_tracked_tickers(source: MarketDataSource, db) -> None:
    """Make the source track exactly watchlist ∪ open positions."""
    async with _sync_lock:                          # chat + manual trades can race
        desired = set(db.watchlist_tickers()) | set(db.held_tickers())
        current = set(source.get_tickers())
        for ticker in sorted(desired - current):
            await source.add_ticker(ticker)
        for ticker in sorted(current - desired):
            await source.remove_ticker(ticker)
```

What this gives:

| Event | Effect |
|---|---|
| Remove a held ticker from the watchlist | Still in `held`, so it stays tracked and priced. It disappears only from the watchlist panel. |
| Full sell of an unwatched ticker | Leaves both sets → `remove_ticker` → `cache.remove` → `version` bump → the next SSE snapshot omits it |
| Add to watchlist | `add_ticker`. With the simulator the price is immediate. |

Call it **after** the DB transaction commits, so that it reads committed state.

### 8.3 Reading prices

```python
cache: PriceCache = request.app.state.price_cache

price = cache.get_price("AAPL")                 # float | None
update = cache.get("AAPL")                      # PriceUpdate | None (open_price, direction…)
everything = cache.get_all()                    # dict[str, PriceUpdate], shallow copy

# Valuation fallback (PLAN §7): no cached price → value at avg_cost
current = cache.get_price(pos.ticker) or pos.avg_cost
```

`cache.get_price(t) or pos.avg_cost` is safe because a real price is never `0.0`. Both sources skip zero prices.

### 8.4 Trading a ticker with no cached price (PLAN §8)

```python
price = cache.get_price(ticker)
if price is None:
    if ticker not in source.get_tickers():
        db.add_to_watchlist(ticker)                 # same path as POST /api/watchlist (cap applies)
        await sync_tracked_tickers(source, db)      # simulator seeds the price here
        price = cache.get_price(ticker)
    if price is None:                               # Massive: arrives on next poll
        raise HTTPException(400, f"No price yet for {ticker}, try again shortly")
```

The fill price is read once and used for both validation and execution, inside the `BEGIN IMMEDIATE` transaction.

---

## 9. SSE stream (`stream.py`)

```python
def create_stream_router(price_cache: PriceCache) -> APIRouter:
    router = APIRouter(prefix="/api/stream", tags=["streaming"])   # built per call

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    return router


async def _generate_events(price_cache, request, interval: float = 0.5):
    yield "retry: 1000\n\n"
    last_version = -1
    while not await request.is_disconnected():
        version = price_cache.version
        if version != last_version:
            last_version = version
            data = {t: u.to_dict() for t, u in price_cache.get_all().items()}
            yield f"data: {json.dumps(data)}\n\n"      # send {} too: frontend replaces state
        await asyncio.sleep(interval)
```

Two changes from the current code:

1. **The router is created inside the factory.** The module-level `router` registered `/prices` again each time the app was built.
2. **Empty snapshots are sent.** The current `if prices:` guard means removing the last tracked ticker never reaches the client. Because the frontend *replaces* its price map on each event (PLAN §6), it would keep showing a stale price.

The first event goes out immediately on connect (`last_version = -1`), so a new client never waits for the next tick.

---

## 10. Testing

**Contract tests** run the same suite against both sources:

```python
@pytest.fixture(params=["simulator", "massive"])
async def source(request):
    cache = PriceCache()
    if request.param == "simulator":
        src = SimulatorDataSource(cache, update_interval=0.05, seed=42)
    else:
        src = MassiveDataSource("k", cache, poll_interval=0.05, client=FakeMassiveClient())
    await src.start(["AAPL", "MSFT"])
    yield src, cache
    await src.stop()


async def test_remove_clears_cache_and_bumps_version(source):
    src, cache = source
    v = cache.version
    await src.remove_ticker("AAPL")
    assert "AAPL" not in src.get_tickers() and cache.get("AAPL") is None and cache.version > v


async def test_add_is_idempotent(source):
    src, _ = source
    await src.add_ticker("NVDA"); await src.add_ticker("NVDA")
    assert src.get_tickers().count("NVDA") == 1
```

**Massive tests use real model objects, not `MagicMock`.** `MagicMock` invents any attribute, which is how `last_trade.timestamp` slipped through. Build fixtures from documented JSON:

```python
from massive.rest.models import TickerSnapshot, GroupedDailyAgg
from massive.exceptions import BadResponse

STARTER_SNAP = {"ticker": "AAPL", "day": {"c": 190.4}, "min": {"c": 190.52, "t": 1760890500000},
                "prevDay": {"c": 189.0}, "updated": 1760890512000000000}          # no lastTrade


class FakeMassiveClient:
    def __init__(self, snapshot_allowed=True):
        self.snapshot_allowed = snapshot_allowed

    def get_snapshot_all(self, market_type, tickers):
        if not self.snapshot_allowed:
            raise BadResponse('{"status":"NOT_AUTHORIZED","message":"You are not entitled to this data."}')
        return [TickerSnapshot.from_dict({**STARTER_SNAP, "ticker": t}) for t in tickers]

    def get_grouped_daily_aggs(self, date, adjusted=True):
        return [GroupedDailyAgg.from_dict({"T": t, "c": 100.0, "t": 1760731200000}) for t in ("AAPL", "MSFT", "NVDA")]


def test_best_price_without_last_trade():
    assert best_price(TickerSnapshot.from_dict(STARTER_SNAP)) == (190.52, 1760890500.0)
```

Must-have cases:

- `best_price` for each fallback level, including zeros and all fields missing
- Units: an ns `sip_timestamp` and an ms `min.t` both become 10-digit seconds
- The free plan switches to EOD; `add_ticker` in EOD mode writes immediately
- Walk-back skips weekends and handles `[]` and `NOT_AUTHORIZED`
- A removed ticker is not rewritten by an in-flight poll
- Unchanged `(price, ts)` doesn't bump `version`
- Factory: blank key → simulator; `SIM_SEED` → seeded; invalid `SIM_SEED` → `ValueError`
- Cache: `open_price` carried forward and reset after `remove`; `remove` of an unknown ticker doesn't bump `version`
- SSE: the router factory can be called twice; an empty cache after removal emits `data: {}`

---

## 11. Change checklist against the current code

| # | File | Change | Source |
|---|---|---|---|
| 1 | `models.py` | Add `open_price` field and include it in `to_dict()` | PLAN §6, §13 |
| 2 | `cache.py` | Carry `open_price`; `remove()` bumps `version`; `timestamp is None` check | PLAN §13 |
| 3 | `stream.py` | Build `APIRouter` inside `create_stream_router()`; emit empty snapshots | PLAN §13, §9 here |
| 4 | `simulator.py` | `seed` param → instance RNGs (see MARKET_SIMULATOR.md) | PLAN §5, §13 |
| 5 | `factory.py` | Read `SIM_SEED` (fail fast if invalid) and `MASSIVE_POLL_INTERVAL` | PLAN §5 |
| 6 | `massive_client.py` | `best_price()` (fixes the `AttributeError` bug), EOD mode, `retries=0`, in-flight removal guard, skip unchanged writes, injectable client, drop ticker uppercasing | MASSIVE_API.md §10, PLAN §13 |
| 7 | `tests/market/` | Real-model Massive fixtures; contract tests; cases listed in §10 | |
| 8 | `.env.example` | Document optional `MASSIVE_POLL_INTERVAL` | |

### Suggested PLAN.md updates

These are not applied; they are left for the plan owner:

- §6 Massive: replace "Free tier: poll every 15 seconds… verify" with "Free (Basic) keys have no snapshot access. The source detects this and shows the last session's closing prices (grouped daily bars, refreshed hourly). Paid keys poll the snapshot every 5 s (`MASSIVE_POLL_INTERVAL`); Starter and Developer data is 15 minutes delayed."
- §5: add `MASSIVE_POLL_INTERVAL` (optional, seconds, default 5).
- §6 Shared Price Cache: note that `open_price` resets when a ticker is removed and re-added.
