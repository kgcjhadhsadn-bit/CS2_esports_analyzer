"""Helpers shared by the P6 simulation tests (not a test module)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import numpy as np

from cs2bot.domain.enums import TeamSlot
from cs2bot.domain.predictions import MapSequenceProbability, MapWinEstimate
from cs2bot.models.player_props import PlayerInput
from cs2bot.testing.synthetic import SynthUniverse

AS_OF = datetime(2025, 3, 1, tzinfo=UTC)  # after every synthetic match (2024)


def lineup(uni: SynthUniverse, team1_id: int, team2_id: int) -> list[PlayerInput]:
    out = [
        PlayerInput(player_id=p.id, team=TeamSlot.TEAM1, nickname=p.nickname, role=p.role)
        for p in uni.teams[team1_id].players
    ]
    out += [
        PlayerInput(player_id=p.id, team=TeamSlot.TEAM2, nickname=p.nickname, role=p.role)
        for p in uni.teams[team2_id].players
    ]
    return out


def veto_sequences(
    maps: list[str], n_maps: int, *, n_seq: int = 6, seed: int = 0
) -> list[MapSequenceProbability]:
    """A random veto distribution over ordered map tuples drawn from ``maps``."""
    rng = np.random.default_rng(seed)
    perms = list(itertools.permutations(maps, n_maps))
    idx = rng.choice(len(perms), size=min(n_seq, len(perms)), replace=False)
    probs = rng.dirichlet(np.ones(idx.size))
    pickers: list[TeamSlot | None] = [TeamSlot.TEAM1, TeamSlot.TEAM2, TeamSlot.TEAM1, TeamSlot.TEAM2, None]
    return [
        MapSequenceProbability(maps=list(perms[i]), pickers=pickers[:n_maps], probability=float(q))
        for i, q in zip(idx, probs, strict=True)
    ]


def estimates(probs: dict[str, float], sigma: float = 0.04) -> dict[str, MapWinEstimate]:
    return {m: MapWinEstimate(map_name=m, p_team1_win=p, sigma=sigma) for m, p in probs.items()}
