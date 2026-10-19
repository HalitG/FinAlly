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
