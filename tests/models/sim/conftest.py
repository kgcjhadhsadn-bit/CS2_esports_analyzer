"""Shared fixtures for the P6 simulation tests (round model, map simulator, player props, engine)."""

from __future__ import annotations

import pytest

from cs2bot.models.map_sim import MapSimulator
from cs2bot.models.player_props import PlayerKillModel
from cs2bot.models.round_model import RoundModel
from cs2bot.testing.synthetic import SynthUniverse


@pytest.fixture(scope="session")
def fitted_round_model(synthetic_universe: SynthUniverse) -> RoundModel:
    return RoundModel().fit(synthetic_universe.round_records())


@pytest.fixture(scope="session")
def fitted_kill_model(synthetic_universe: SynthUniverse) -> PlayerKillModel:
    return PlayerKillModel().fit(synthetic_universe.player_map_records(), half_life_days=120)


@pytest.fixture
def simulator() -> MapSimulator:
    """A fresh simulator (no cache sharing between tests)."""
    return MapSimulator()
