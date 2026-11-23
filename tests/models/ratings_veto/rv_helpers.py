"""Shared helpers for the ratings / veto tests: cached synthetic universes and record builders."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import lru_cache

from cs2bot.domain.enums import MatchFormat, TeamSlot, Tier, VetoAction
from cs2bot.domain.records import MapResultRecord, VetoRecord, VetoStep
from cs2bot.testing.synthetic import SynthUniverse, generate_universe

T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
POOL7 = ["ancient", "anubis", "dust2", "inferno", "mirage", "nuke", "train"]


@lru_cache(maxsize=16)
def universe(
    seed: int = 3,
    n_teams: int = 16,
    n_matches: int = 600,
    map_offset_sd: float = 0.15,
    drift_sd_per_30d: float = 0.0,
) -> SynthUniverse:
    """Deterministic synthetic universe without kill events (fast to generate, cached per process)."""
    return generate_universe(
        n_teams=n_teams,
        n_matches=n_matches,
        seed=seed,
        map_offset_sd=map_offset_sd,
        drift_sd_per_30d=drift_sd_per_30d,
        with_kills=False,
    )


def map_result(
    match_id: int,
    team1_id: int,
    team2_id: int,
    *,
    day: float = 0.0,
    score: tuple[int, int] = (13, 8),
    map_name: str = "mirage",
    map_number: int = 1,
    is_lan: bool = False,
    tier: Tier = Tier.A,
    lineup1: tuple[int, ...] = (),
    lineup2: tuple[int, ...] = (),
) -> MapResultRecord:
    return MapResultRecord(
        match_id=match_id,
        map_number=map_number,
        map_name=map_name,
        played_at=T0 + timedelta(days=day),
        team1_id=team1_id,
        team2_id=team2_id,
        team1_rounds=score[0],
        team2_rounds=score[1],
        is_lan=is_lan,
        tier=tier,
        format=MatchFormat.BO1,
        team1_lineup=lineup1,
        team2_lineup=lineup2,
    )


def veto_steps(spec: list[tuple[str, int | None, str]]) -> tuple[VetoStep, ...]:
    """``[("ban", 1, "nuke"), ("pick", 2, "mirage"), ("leftover", None, "train")]`` -> VetoSteps."""
    slots = {1: TeamSlot.TEAM1, 2: TeamSlot.TEAM2, None: None}
    return tuple(
        VetoStep(step=i, action=VetoAction(action), team=slots[team], map_name=m)
        for i, (action, team, m) in enumerate(spec, start=1)
    )


def veto_record(
    match_id: int,
    team1_id: int,
    team2_id: int,
    spec: list[tuple[str, int | None, str]],
    *,
    day: float = 0.0,
    fmt: MatchFormat = MatchFormat.BO3,
    pool: list[str] | None = None,
) -> VetoRecord:
    return VetoRecord(
        match_id=match_id,
        played_at=T0 + timedelta(days=day),
        format=fmt,
        team1_id=team1_id,
        team2_id=team2_id,
        steps=veto_steps(spec),
        map_pool=tuple(pool) if pool is not None else tuple(POOL7),
    )
