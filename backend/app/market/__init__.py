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
