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
