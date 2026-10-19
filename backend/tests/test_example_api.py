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
