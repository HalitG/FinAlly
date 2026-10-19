"""Tests for PriceUpdate dataclass."""

import pytest

from app.market.models import PriceUpdate


def _update(price: float, previous: float, open_price: float = 190.00) -> PriceUpdate:
    return PriceUpdate(
        ticker="AAPL",
        price=price,
        previous_price=previous,
        open_price=open_price,
        timestamp=1234567890.0,
    )


class TestPriceUpdate:
    """Unit tests for the PriceUpdate model."""

    def test_price_update_creation(self):
        update = _update(190.50, 190.00, open_price=189.00)
        assert update.ticker == "AAPL"
        assert update.price == 190.50
        assert update.previous_price == 190.00
        assert update.open_price == 189.00
        assert update.timestamp == 1234567890.0

    def test_open_price_is_required(self):
        with pytest.raises(TypeError):
            PriceUpdate(ticker="AAPL", price=1.0, previous_price=1.0)  # type: ignore[call-arg]

    def test_change_calculation(self):
        assert _update(190.50, 190.00).change == 0.50

    def test_change_negative(self):
        assert _update(189.50, 190.00).change == -0.50

    def test_change_percent_up(self):
        assert _update(190.00, 100.00).change_percent == 90.0

    def test_change_percent_down(self):
        assert _update(100.00, 200.00).change_percent == -50.0

    def test_change_percent_zero_previous(self):
        assert _update(100.00, 0.00).change_percent == 0.0

    def test_direction_up(self):
        assert _update(191.00, 190.00).direction == "up"

    def test_direction_down(self):
        assert _update(189.00, 190.00).direction == "down"

    def test_direction_flat(self):
        assert _update(190.00, 190.00).direction == "flat"

    def test_to_dict(self):
        result = _update(190.50, 190.00, open_price=189.00).to_dict()
        assert result == {
            "ticker": "AAPL",
            "price": 190.50,
            "previous_price": 190.00,
            "open_price": 189.00,
            "timestamp": 1234567890.0,
            "change": 0.50,
            "change_percent": 0.2632,  # (0.50 / 190.00) * 100
            "direction": "up",
        }

    def test_immutability(self):
        update = _update(190.50, 190.00)
        with pytest.raises(AttributeError):
            update.price = 200.00  # type: ignore[misc]
