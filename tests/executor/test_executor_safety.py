"""Halt check from the database (fail safe) and human-like pacing."""

from __future__ import annotations

import random

import pytest
from sqlalchemy.orm import Session, sessionmaker

from cs2bot.db import state
from cs2bot.executor.safety import HumanPacer, db_halt_check, never_halted


def test_db_halt_check_follows_kill_switch_and_pause(session_factory: sessionmaker[Session]) -> None:
    check = db_halt_check(session_factory)
    assert check() == (False, None)
    with session_factory() as s:
        state.set_kill_switch(s, True, actor="test", reason="manual")
        s.commit()
    assert check() == (True, "kill switch is on")  # a fresh session per call sees it immediately
    with session_factory() as s:
        state.set_kill_switch(s, False, actor="test")
        state.set_paused(s, True, actor="test", reason="daily loss limit")
        s.commit()
    halted, reason = check()
    assert halted and reason is not None and "daily loss limit" in reason


def test_db_halt_check_fails_safe() -> None:
    def broken_factory() -> Session:
        raise RuntimeError("database unreachable")

    halted, reason = db_halt_check(broken_factory)()  # type: ignore[arg-type]
    assert halted is True
    assert reason is not None and "RuntimeError" in reason


def test_never_halted() -> None:
    assert never_halted() == (False, None)


def test_human_pacer_sleeps_within_bounds() -> None:
    slept: list[float] = []
    pacer = HumanPacer(1.5, 4.0, sleep=slept.append, rng=random.Random(1))
    for _ in range(50):
        pacer.pause()
    assert len(slept) == 50 and all(1.5 <= s <= 4.0 for s in slept)
    pacer.pause(0.5)
    assert 0.75 <= slept[-1] <= 2.0
    pacer.sleep(3.0)
    assert slept[-1] == 3.0
    assert 60 <= pacer.typing_delay_ms() <= 180


def test_human_pacer_zero_delays_never_sleep() -> None:
    slept: list[float] = []
    pacer = HumanPacer(0, 0, sleep=slept.append)
    assert pacer.pause() == 0
    pacer.sleep(0)
    assert slept == []


def test_human_pacer_validation() -> None:
    with pytest.raises(ValueError):
        HumanPacer(-1, 2)
    swapped = HumanPacer(3, 1)
    assert (swapped.min_s, swapped.max_s) == (1, 3)
