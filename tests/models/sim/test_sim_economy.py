"""EconomyParams / BuyPolicy / RoundFlowParams: CS2 constants and validation."""

from __future__ import annotations

import pytest

from cs2bot.domain.enums import BuyType
from cs2bot.models.economy import (
    BUY_CODE,
    BUY_ORDER,
    BuyPolicy,
    EconomyParams,
    RoundFlowParams,
)


def test_cs2_default_rules() -> None:
    e = EconomyParams()
    assert (e.rounds_per_half, e.rounds_to_win, e.regulation_rounds) == (12, 13, 24)
    assert (e.ot_rounds_per_half, e.ot_rounds_to_win, e.ot_rounds) == (3, 4, 6)
    assert e.start_money == 800 and e.max_money == 16_000
    assert e.ot_start_money == 10_000  # Premier default used by Valve Majors
    assert (e.win_elimination, e.win_time, e.win_defuse, e.win_detonation) == (3250, 3250, 3500, 3500)
    assert e.plant_bonus_team == 600 and e.ct_team_kill_bonus == 50
    assert e.ot_switch_sides_at_start is False


def test_loss_bonus_ladder_and_cs2_semantics() -> None:
    e = EconomyParams()
    assert e.loss_bonus_ladder == (1400, 1900, 2400, 2900, 3400)
    # mp_starting_losses 1: the pistol-round loser gets $1,900
    assert e.loss_bonus(e.starting_losses) == 1900
    # capped at the top of the ladder
    assert e.loss_bonus(10) == 3400 and e.loss_bonus(-3) == 1400


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rounds_to_win": 16},
        {"ot_rounds_to_win": 3},
        {"loss_aversion": 3},
        {"kill_reward_by_buy": (300, 300)},
    ],
)
def test_economy_validation(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        EconomyParams(**kwargs)  # type: ignore[arg-type]


def test_policy_and_flow_validation() -> None:
    with pytest.raises(ValueError):
        BuyPolicy(p_force_base=1.5)
    with pytest.raises(ValueError):
        RoundFlowParams(p_ct_win_defuse=0.7, p_ct_win_time=0.5)
    # hashable (used in simulation cache keys)
    assert hash(EconomyParams()) == hash(EconomyParams())
    assert hash(BuyPolicy()) == hash(BuyPolicy()) and hash(RoundFlowParams()) == hash(RoundFlowParams())


def test_buy_codes() -> None:
    assert BUY_ORDER == (BuyType.PISTOL, BuyType.ECO, BuyType.FORCE, BuyType.FULL)
    assert [BUY_CODE[b] for b in BUY_ORDER] == [0, 1, 2, 3]
