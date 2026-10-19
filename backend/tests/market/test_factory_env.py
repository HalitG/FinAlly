"""Factory: SIM_SEED and MASSIVE_POLL_INTERVAL handling."""

import os
from unittest.mock import patch

import pytest

from app.market.cache import PriceCache
from app.market.factory import create_market_data_source
from app.market.massive_client import MassiveDataSource
from app.market.simulator import SimulatorDataSource


def build(env: dict[str, str]):
    with patch.dict(os.environ, env, clear=True):
        return create_market_data_source(PriceCache())


def test_sim_seed_passed_to_simulator():
    source = build({"SIM_SEED": "42"})
    assert isinstance(source, SimulatorDataSource) and source._seed == 42


def test_blank_sim_seed_is_unseeded():
    assert build({"SIM_SEED": "  "})._seed is None


def test_invalid_sim_seed_fails_fast():
    with pytest.raises(ValueError, match="SIM_SEED"):
        build({"SIM_SEED": "abc"})


def test_massive_default_poll_interval():
    source = build({"MASSIVE_API_KEY": "k"})
    assert isinstance(source, MassiveDataSource) and source._interval == 5.0


def test_massive_poll_interval_override():
    assert build({"MASSIVE_API_KEY": "k", "MASSIVE_POLL_INTERVAL": "2.5"})._interval == 2.5


def test_sim_seed_ignored_when_massive_selected():
    assert isinstance(build({"MASSIVE_API_KEY": "k", "SIM_SEED": "abc"}), MassiveDataSource)
