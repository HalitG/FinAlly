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
