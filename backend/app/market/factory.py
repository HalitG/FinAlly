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
