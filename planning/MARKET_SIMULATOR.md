# Market Simulator

The approach and code structure for FinAlly's built-in price simulator. It is the default market data source, used whenever `MASSIVE_API_KEY` is not set. It implements the `MarketDataSource` interface (MARKET_INTERFACE.md §5) and writes to the shared `PriceCache`. Nothing downstream can tell it apart from real data.

The simulator already exists in `backend/app/market/simulator.py` and `seed_prices.py`. This document records how it works and why, the numbers behind its behavior (measured, not guessed), and the one required change: reproducible seeding via `SIM_SEED` (PLAN §5, §13).

---

## 1. Goals

| Goal | How |
|---|---|
| Looks like a live market | Geometric Brownian motion (GBM), a tick every 500 ms, realistic seed prices and per-stock volatility |
| Stocks move together | Sector-correlated random shocks (Cholesky decomposition) |
| Drama for a demo | Rare sudden 2–5% jumps on a single ticker |
| Any ticker works | Unknown symbols get a random $50–$300 start and default parameters |
| Instant prices | `start()` and `add_ticker()` write the price to the cache before returning |
| Reproducible tests | `SIM_SEED` seeds the simulator's random generators |
| Cheap and dependency-free | Pure Python + NumPy, in-process, no I/O |

Non-goals: order-book microstructure, bid/ask, volume, market hours, overnight gaps. The simulated market is always open.

---

## 2. The model

### 2.1 Geometric Brownian motion

Each ticker follows

```
dS = μ S dt + σ S dW
```

whose exact solution over one step is

```
S(t+Δt) = S(t) · exp( (μ − σ²/2) Δt + σ √Δt · Z ),    Z ~ N(0, 1)
```

| Symbol | Meaning | Source |
|---|---|---|
| `S` | price | seed price, then the previous tick |
| `μ` | annualized drift | `TICKER_PARAMS[t]["mu"]` |
| `σ` | annualized volatility | `TICKER_PARAMS[t]["sigma"]` |
| `Δt` | step as a fraction of a trading year | `0.5 / (252 × 6.5 × 3600) ≈ 8.48e-8` |
| `Z` | standard normal draw, **correlated across tickers** (§3) | |

Why GBM:

- **Prices stay positive.** The exponential form can never go negative or to zero, unlike adding normal noise.
- **Moves are proportional.** A $800 NVDA moves ~4× more in dollars than a $190 AAPL with the same σ, as real stocks do.
- **The update is exact,** not an Euler approximation, so there is no discretization error however `Δt` is chosen.
- It is the textbook model, so students can check the code against any reference.

### 2.2 Time scale

`Δt` is calibrated so that **wall-clock time equals market time**. One real hour of running is one hour of a 6.5-hour trading day, and annual σ means what it says. The constants are on the class:

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600   # 5,896,800
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ≈ 8.48e-8
```

The consequence is that **diffusion alone is gentle**. These values are measured from the current parameters (σ·√Δt, then scaled by √time):

| Ticker | Seed | σ | 1 tick (σ, %) | 1 tick (σ, $) | 10 min | 1 hour |
|---|---|---|---|---|---|---|
| AAPL | 190 | 0.22 | 0.0064% | $0.012 | 0.22% | 0.54% |
| GOOGL | 175 | 0.25 | 0.0073% | $0.013 | 0.25% | 0.62% |
| MSFT | 420 | 0.20 | 0.0058% | $0.025 | 0.20% | 0.49% |
| AMZN | 185 | 0.28 | 0.0082% | $0.015 | 0.28% | 0.69% |
| TSLA | 250 | 0.50 | 0.0146% | $0.036 | 0.50% | 1.24% |
| NVDA | 800 | 0.40 | 0.0116% | $0.093 | 0.40% | 0.99% |
| META | 500 | 0.30 | 0.0087% | $0.044 | 0.30% | 0.74% |
| JPM | 195 | 0.18 | 0.0052% | $0.010 | 0.18% | 0.44% |
| V | 280 | 0.17 | 0.0050% | $0.014 | 0.17% | 0.42% |
| NFLX | 600 | 0.35 | 0.0102% | $0.061 | 0.35% | 0.86% |

Drift is negligible on demo timescales. μ = 5%/yr is about 0.003% per hour. It is kept for correctness, not for visible effect.

**Flat ticks.** The cache rounds to cents. A typical JPM tick is about $0.01, so **~36% of JPM ticks round to an unchanged price** (measured over 20,000 ticks). They show as `direction: "flat"` with no flash. High-priced names (NVDA, NFLX, META) change almost every tick. This uneven flashing looks realistic and is intended.

### 2.3 Random events

After the GBM step, each ticker independently has probability `event_probability` (default `0.001`) of a shock:

```python
if rng.random() < event_probability:
    magnitude = rng.uniform(0.02, 0.05)          # 2–5%
    sign = rng.choice((-1, 1))
    price *= 1 + sign * magnitude
```

| Count | Events |
|---|---|
| per ticker | 1 per 1,000 ticks ≈ every 8.3 min (~7.2 per hour) |
| 10 tickers (default watchlist) | one somewhere every ~50 s |
| 30 tickers (watchlist cap) | one every ~17 s |

Shocks are **idiosyncratic**: they hit one ticker and are not correlated. That makes them stand out against the correlated background.

**Events dominate volatility.** A Monte-Carlo run of AAPL (2,000 one-hour paths) gave:

| Model | 1-hour σ of return |
|---|---|
| GBM only | 0.55% |
| GBM + events (default) | **9.7%** |

About 95% of the variance comes from events. Over a one-hour demo a stock typically ends ±10% from its start, in a few visible jumps on a gently wandering line. This is the intended "drama": the heatmap fills with color (its scale clamps at ±10%, PLAN §10) and the AI assistant has something to talk about. To make the market calmer, lower `event_probability` (§7).

Because the shock is `× (1 ± s)` rather than `× exp(±s)`, the mean is preserved but the median drifts down by about `E[s²]/2` per event, roughly −0.5% per hour. That is small next to the ±10% spread. Use `exp(sign * magnitude)` if exact log-symmetry ever matters.

---

## 3. Correlated moves

### 3.1 Correlation structure (`seed_prices.py`)

| Pair | ρ | Constant |
|---|---|---|
| Both in tech (AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX) | 0.6 | `INTRA_TECH_CORR` |
| Both in finance (JPM, V) | 0.5 | `INTRA_FINANCE_CORR` |
| TSLA with anything | 0.3 | `TSLA_CORR` (TSLA is listed in tech but "does its own thing") |
| Cross-sector, or any unknown ticker | 0.3 | `CROSS_GROUP_CORR` |

A baseline of 0.3 between everything gives a visible "market" factor: on most ticks the board leans green or red together.

### 3.2 Generating correlated draws

Build the n×n correlation matrix `C` for the current tickers, factor it once as `C = L Lᵀ` (Cholesky), and on every tick:

```python
z_independent = rng.standard_normal(n)
z_correlated = L @ z_independent          # Cov(z_correlated) = L Lᵀ = C
```

`L` is rebuilt only when tickers are added or removed. That costs an O(n²) Python loop to fill `C` plus an O(n³) NumPy factorization, which takes microseconds at the 30-ticker cap. The per-tick cost is one matrix-vector product.

### 3.3 The matrix is always positive-definite

Cholesky fails unless `C` is positive-definite. That holds for **any** mix of known and unknown tickers. Write `C` as

```
C = 0.3·J + 0.3·J_tech + 0.2·J_fin + D
```

where `J` is the all-ones matrix and `J_tech` / `J_fin` are all-ones on the tech (excluding TSLA) and finance blocks. These are positive semi-definite. `D` is diagonal with entries 0.4 (tech), 0.5 (finance) or 0.7 (other). Therefore every eigenvalue is ≥ 0.4. A numeric check with all 10 seeded tickers plus 20 unknown ones gave a minimum eigenvalue of exactly 0.400. If someone edits the constants, the argument still holds as long as `0 ≤ CROSS_GROUP_CORR ≤` every in-group ρ `< 1` and `TSLA_CORR == CROSS_GROUP_CORR`.

---

## 4. Parameters and seed data (`seed_prices.py`)

```python
SEED_PRICES = {"AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00, "TSLA": 250.00,
               "NVDA": 800.00, "META": 500.00, "JPM": 195.00, "V": 280.00, "NFLX": 600.00}

TICKER_PARAMS = {
    "AAPL": {"sigma": 0.22, "mu": 0.05},  "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT": {"sigma": 0.20, "mu": 0.05},  "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA": {"sigma": 0.50, "mu": 0.03},  "NVDA":  {"sigma": 0.40, "mu": 0.08},
    "META": {"sigma": 0.30, "mu": 0.05},  "JPM":   {"sigma": 0.18, "mu": 0.04},
    "V":    {"sigma": 0.17, "mu": 0.04},  "NFLX":  {"sigma": 0.35, "mu": 0.05},
}
DEFAULT_PARAMS = {"sigma": 0.25, "mu": 0.05}      # any ticker not listed

CORRELATION_GROUPS = {"tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
                      "finance": {"JPM", "V"}}
INTRA_TECH_CORR, INTRA_FINANCE_CORR, CROSS_GROUP_CORR, TSLA_CORR = 0.6, 0.5, 0.3, 0.3
```

The σ values are rough long-run implied volatilities: banks and payments are low, mega-cap tech is mid, NVDA and TSLA are high. Seed prices are round, plausible levels. They are not live quotes and do not need updating.

Unknown tickers (e.g. `PYPL` added via chat) start at `uniform(50, 300)` with `DEFAULT_PARAMS` and correlation 0.3 to everything. With `SIM_SEED` set, that start price is reproducible (§5).

To give a new symbol realistic behavior, add it to `SEED_PRICES`, `TICKER_PARAMS` and, if it fits, a `CORRELATION_GROUPS` set. No code changes are needed.

---

## 5. Reproducibility: `SIM_SEED`

PLAN §5: *"If `SIM_SEED` is set → the simulator seeds `random` and `numpy` with it, so a run is reproducible."*

**Design:** the simulator owns **private generator instances** instead of seeding the global modules:

```python
self._rng = random.Random(seed)               # events, unknown-ticker start prices
self._np_rng = np.random.default_rng(seed)    # correlated normal draws
```

This meets the requirement (same seed → same sequence) and is safer than `random.seed()` / `np.random.seed()`. Other code that uses the global RNGs, such as `uuid4` and library internals, can't shift the simulator's sequence or be shifted by it. `default_rng` is NumPy's recommended API, and the legacy global `np.random.*` functions are discouraged. With `seed=None`, both generators are seeded from OS entropy, which matches today's behavior.

Every random call in the simulator goes through these two generators: the normal draws, the event roll, magnitude and sign, and the random start price.

**What is reproducible:** for the same seed and the same sequence of `add_ticker` / `remove_ticker` calls, the k-th tick produces the same prices. Unknown tickers get the same start price.

**What is not:** the wall clock. How many ticks have run when a test looks, or when a chat adds a ticker, depends on timing. Seeding removes randomness, not scheduling. E2E tests should therefore assert **behavior** ("a price changes within 3 s", "an added ticker has a price"), not exact values. Unit tests that call `GBMSimulator.step()` directly *are* fully deterministic and can assert exact numbers.

The factory reads `SIM_SEED` and fails fast on a non-integer (MARKET_INTERFACE.md §7).

---

## 6. Code structure (`simulator.py`)

There are two classes with a deliberate split:

| Class | Responsibility | Async? | Knows about the cache? |
|---|---|---|---|
| `GBMSimulator` | The math: state, parameters, correlation, `step()` | No | No |
| `SimulatorDataSource` | `MarketDataSource` adapter: background loop, cache writes, lifecycle | Yes | Yes |

`GBMSimulator` is a plain synchronous object, so its unit tests need no event loop, no sleeps and no cache. `SimulatorDataSource` holds no math, so its tests check only lifecycle and cache semantics.

### 6.1 `GBMSimulator` (target code; the seeding change is marked)

```python
class GBMSimulator:
    """Geometric Brownian Motion simulator for correlated stock prices.

    S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z),  Z correlated N(0,1)
    """

    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
        seed: int | None = None,                               # NEW
    ) -> None:
        self._dt = dt
        self._event_prob = event_probability
        self._rng = random.Random(seed)                        # NEW
        self._np_rng = np.random.default_rng(seed)             # NEW

        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def step(self) -> dict[str, float]:
        """Advance all tickers one time step. Returns {ticker: price rounded to cents}."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z = self._np_rng.standard_normal(n)                    # was np.random.standard_normal
        if self._cholesky is not None:
            z = self._cholesky @ z

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu, sigma = self._params[ticker]["mu"], self._params[ticker]["sigma"]
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            if self._rng.random() < self._event_prob:          # was random.random
                shock = self._rng.uniform(0.02, 0.05) * self._rng.choice((-1, 1))
                self._prices[ticker] *= 1 + shock
                logger.debug("Random event on %s: %+.1f%%", ticker, shock * 100)

            result[ticker] = round(self._prices[ticker], 2)
        return result

    def add_ticker(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        return self._prices.get(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    def _add_ticker_internal(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker) or self._rng.uniform(50.0, 300.0)  # was random.uniform
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return
        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                corr[i, j] = corr[j, i] = self._pairwise_correlation(self._tickers[i], self._tickers[j])
        self._cholesky = np.linalg.cholesky(corr)

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR
        if t1 in CORRELATION_GROUPS["tech"] and t2 in CORRELATION_GROUPS["tech"]:
            return INTRA_TECH_CORR
        if t1 in CORRELATION_GROUPS["finance"] and t2 in CORRELATION_GROUPS["finance"]:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR
```

Notes:

- **Full-precision state, rounded output.** `_prices` keeps floats unrounded, so cent rounding never accumulates into a bias. Only the returned dict is rounded. The cache rounds again, which is harmless.
- **The order of `_tickers` matters.** It indexes `z` and the Cholesky rows, and it is why seeded runs depend on the add/remove sequence.
- The `SEED_PRICES.get(ticker) or ...` form only calls the RNG when a seed price is missing, so known tickers consume no random numbers on add. This keeps seeded sequences stable when the default watchlist changes order.
- The per-ticker Python loop is fine at n ≤ 30 (microseconds per tick). Vectorizing it with NumPy arrays is possible but not worth the extra complexity here.

### 6.2 `SimulatorDataSource`

```python
class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by GBMSimulator; ticks every `update_interval` seconds."""

    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,
        event_probability: float = 0.001,
        seed: int | None = None,                               # NEW, from SIM_SEED
    ) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._seed = seed
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob, seed=self._seed)
        for ticker in self._sim.get_tickers():                 # seed cache: SSE has data at once
            self._cache.update(ticker=ticker, price=self._sim.get_price(ticker))
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            self._cache.update(ticker=ticker, price=self._sim.get_price(ticker))  # price now

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)                             # bumps version (MARKET_INTERFACE §4.2)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        while True:
            try:
                if self._sim:
                    for ticker, price in self._sim.step().items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")      # keep ticking
            await asyncio.sleep(self._interval)
```

Behavior notes:

- **No race on removal.** `step()` and the cache writes after it run synchronously with no `await` in between, so `remove_ticker()` can never interleave and have a removed ticker written back. (The Massive source needs an explicit guard for this; the simulator doesn't.)
- **`add_ticker` on an already-tracked ticker** re-writes its current price to the cache. That is harmless: `previous_price` becomes the same value for one tick, and the next tick restores direction.
- **A re-added ticker restarts.** Removing a seeded ticker discards its state, and re-adding starts it again from `SEED_PRICES` (or a new random price). The cache also resets its `open_price` (MARKET_INTERFACE.md §4.2), so the change-% stays meaningful.
- **Cadence:** `sleep(interval)` after each step means the period is 500 ms plus step time (microseconds). Drift-free scheduling is unnecessary, because the cache timestamp is wall-clock time at write.
- **Each tick writes every ticker,** so the cache `version` advances every 500 ms and the SSE stream emits about twice a second (PLAN §6).

---

## 7. Tuning knobs

| Want | Change | Effect |
|---|---|---|
| Calmer market | `event_probability=0.0002` | ~1.4 events/ticker/hour; 1-hour σ falls from ~9.7% to ~4.4% |
| Livelier ticks between events | `dt = k * DEFAULT_DT` (time compression ×k) | Per-tick σ grows by √k. For example k = 16 gives AAPL ~$0.05/tick and few flat ticks. |
| No jumps (pure GBM, e.g. math tests) | `event_probability=0.0` | |
| Faster or slower stream | `update_interval` | Change `dt` with it to keep market time = wall time |
| A ticker's personality | `TICKER_PARAMS`, `CORRELATION_GROUPS` | Data-only change |

The defaults match the existing code, tests and demo. Change them only with a reason, and keep the unit tests in step.

---

## 8. Testing

Existing suites: `tests/market/test_simulator.py` (17 tests, math and state) and `test_simulator_source.py` (10, lifecycle and cache). None of them patch the global RNGs, so moving to instance generators breaks nothing. Add:

```python
def test_same_seed_same_path():
    a = GBMSimulator(["AAPL", "PYPL"], seed=7)
    b = GBMSimulator(["AAPL", "PYPL"], seed=7)
    assert a.get_price("PYPL") == b.get_price("PYPL")          # random start price reproducible
    assert [a.step() for _ in range(100)] == [b.step() for _ in range(100)]


def test_different_seed_different_path():
    a, b = GBMSimulator(["AAPL"], seed=1), GBMSimulator(["AAPL"], seed=2)
    assert [a.step() for _ in range(20)] != [b.step() for _ in range(20)]


def test_seed_isolated_from_global_random():
    a = GBMSimulator(["AAPL"], seed=7)
    random.random(); np.random.standard_normal(5)              # other code using global RNGs
    b = GBMSimulator(["AAPL"], seed=7)
    assert a.step() == b.step()


def test_correlation_matrix_pd_at_watchlist_cap():
    sim = GBMSimulator(list(SEED_PRICES) + [f"X{i}" for i in range(20)])   # 30 tickers
    assert sim._cholesky is not None                            # cholesky() would have raised


def _log_returns(sim: GBMSimulator, tickers: list[str], steps: int) -> np.ndarray:
    """Unrounded log-returns (cent rounding would distort tiny per-tick moves)."""
    path = [[sim._prices[t] for t in tickers]]
    for _ in range(steps):
        sim.step()
        path.append([sim._prices[t] for t in tickers])
    return np.diff(np.log(np.array(path)), axis=0)


def test_log_return_volatility_matches_sigma():
    sim = GBMSimulator(["AAPL"], event_probability=0.0, seed=3)
    r = _log_returns(sim, ["AAPL"], 20_000)[:, 0]
    assert abs(r.std() / math.sqrt(sim._dt) - 0.22) < 0.01     # ≈ annual sigma


def test_tech_stocks_correlate():
    sim = GBMSimulator(["AAPL", "MSFT", "JPM"], event_probability=0.0, seed=5)
    c = np.corrcoef(_log_returns(sim, ["AAPL", "MSFT", "JPM"], 5_000).T)
    assert 0.5 < c[0, 1] < 0.7 and 0.2 < c[0, 2] < 0.4         # tech 0.6, cross 0.3
```

The volatility and correlation tests use `event_probability=0.0`, because jumps would swamp σ (§2.3).

E2E (`test/`) runs with `SIM_SEED` fixed and asserts behavior only (§5).

---

## 9. Demo

`backend/market_data_demo.py` is a Rich terminal dashboard that runs the simulator for 60 s with sparklines and an event log:

```bash
cd backend
uv run market_data_demo.py
```

Once seeding lands, `SIM_SEED=42 uv run market_data_demo.py` replays the same session up to wall-clock timing. That is handy for screenshots and teaching.
