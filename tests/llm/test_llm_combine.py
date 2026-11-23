"""combine_flags: clipping, confidence weighting, expiry, veto - unit and property tests."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cs2bot.config import LLMSettings
from cs2bot.domain.context import ContextFlag
from cs2bot.domain.enums import ContextFlagType, TeamSlot
from cs2bot.llm.combine import active_flags, clip, combine_flags, flag_contribution, is_expired
from cs2bot.llm.context import combine_flags as combine_flags_from_context
from tests.llm.llm_factories import NOW, make_flag

SETTINGS = LLMSettings()  # max_flag 0.20, max_total 0.35


def test_no_flags_is_neutral() -> None:
    assert combine_flags([], SETTINGS, now=NOW) == (0.0, False)


def test_architecture_path_exports_the_same_function() -> None:
    assert combine_flags_from_context is combine_flags


def test_shift_is_weighted_by_confidence() -> None:
    total, veto = combine_flags([make_flag(logit_shift=0.1, confidence=0.5)], SETTINGS, now=NOW)
    assert total == pytest.approx(0.05)
    assert veto is False


def test_each_flag_is_clipped_before_weighting() -> None:
    total, _ = combine_flags([make_flag(logit_shift=5.0, confidence=0.5)], SETTINGS, now=NOW)
    assert total == pytest.approx(0.20 * 0.5)
    total, _ = combine_flags([make_flag(logit_shift=-5.0, confidence=1.0)], SETTINGS, now=NOW)
    assert total == pytest.approx(-0.20)


def test_total_is_clipped() -> None:
    flags = [make_flag(logit_shift=0.2, confidence=1.0) for _ in range(5)]
    assert combine_flags(flags, SETTINGS, now=NOW)[0] == pytest.approx(0.35)
    flags = [make_flag(logit_shift=-0.2, confidence=1.0) for _ in range(5)]
    assert combine_flags(flags, SETTINGS, now=NOW)[0] == pytest.approx(-0.35)


def test_opposite_flags_cancel() -> None:
    flags = [make_flag(logit_shift=0.15), make_flag(logit_shift=-0.15, team=TeamSlot.TEAM1)]
    assert combine_flags(flags, SETTINGS, now=NOW)[0] == pytest.approx(0.0)


def test_expired_flags_are_ignored() -> None:
    expired = make_flag(logit_shift=0.2, veto=True, expires_at=NOW - timedelta(seconds=1))
    assert combine_flags([expired], SETTINGS, now=NOW) == (0.0, False)
    assert is_expired(expired, NOW)


def test_flag_expiring_exactly_now_or_later_still_counts() -> None:
    at_now = make_flag(logit_shift=0.1, veto=True, expires_at=NOW)
    later = make_flag(logit_shift=0.1, expires_at=NOW + timedelta(hours=1))
    total, veto = combine_flags([at_now, later], SETTINGS, now=NOW)
    assert total == pytest.approx(0.2)
    assert veto is True
    assert active_flags([at_now, later], now=NOW) == [at_now, later]


def test_expiry_in_another_timezone_is_compared_as_an_instant() -> None:
    warsaw = datetime.fromisoformat("2026-11-20T12:30:00+01:00")  # 11:30 UTC, i.e. before NOW (12:00 UTC)
    assert combine_flags([make_flag(expires_at=warsaw)], SETTINGS, now=NOW) == (0.0, False)


@pytest.mark.parametrize(("confidence", "expected"), [(0.59, False), (0.6, True), (0.95, True)])
def test_veto_needs_confidence(confidence: float, expected: bool) -> None:
    flag = make_flag(veto=True, confidence=confidence, logit_shift=0.0)
    assert combine_flags([flag], SETTINGS, now=NOW)[1] is expected


def test_veto_from_any_single_flag() -> None:
    flags = [make_flag(), make_flag(veto=True, confidence=0.7, flag_type=ContextFlagType.OTHER), make_flag()]
    assert combine_flags(flags, SETTINGS, now=NOW)[1] is True


def test_custom_veto_threshold() -> None:
    flag = make_flag(veto=True, confidence=0.7)
    assert combine_flags([flag], SETTINGS, now=NOW, veto_min_confidence=0.8)[1] is False


def test_non_finite_values_contribute_nothing() -> None:
    nan_shift = make_flag(logit_shift=float("nan"))
    inf_shift = make_flag(logit_shift=float("inf"))
    total, veto = combine_flags([nan_shift, inf_shift], SETTINGS, now=NOW)
    assert (total, veto) == (0.0, False)
    assert clip(float("-inf"), 0.2) == 0.0
    assert clip(0.5, float("nan")) == 0.0


def test_out_of_range_confidence_from_a_corrupted_payload_is_clamped() -> None:
    # model_construct bypasses validation, like a hand-edited JSON payload could.
    weird = ContextFlag.model_construct(
        flag_type=ContextFlagType.OTHER,
        team=None,
        severity=0.5,
        confidence=7.0,
        logit_shift=0.1,
        veto=False,
        rationale="x",
        evidence=[],
        expires_at=None,
    )
    assert combine_flags([weird], SETTINGS, now=NOW)[0] == pytest.approx(0.1)
    nan_conf = weird.model_copy(update={"confidence": float("nan"), "veto": True})
    assert combine_flags([nan_conf], SETTINGS, now=NOW) == (0.0, False)


def test_negative_bounds_in_settings_are_treated_as_magnitudes() -> None:
    settings = LLMSettings(max_flag_logit_shift=-0.2, max_total_logit_shift=-0.35)
    assert combine_flags([make_flag(logit_shift=1.0)], settings, now=NOW)[0] == pytest.approx(0.2)


def test_zero_bounds_disable_the_adjustment_but_not_the_veto() -> None:
    settings = LLMSettings(max_flag_logit_shift=0.0, max_total_logit_shift=0.0)
    total, veto = combine_flags([make_flag(logit_shift=0.2, veto=True)], settings, now=NOW)
    assert total == 0.0 and math.copysign(1.0, total) == 1.0  # never "-0.0"
    assert veto is True


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        combine_flags([make_flag()], SETTINGS, now=datetime(2026, 11, 20, 12, 0))


def test_naive_expiry_is_read_as_utc() -> None:
    flag = ContextFlag.model_construct(
        flag_type=ContextFlagType.OTHER,
        team=None,
        severity=0.1,
        confidence=1.0,
        logit_shift=0.1,
        veto=False,
        rationale="x",
        evidence=[],
        expires_at=datetime(2026, 11, 20, 11, 0),  # naive, one hour before NOW
    )
    assert combine_flags([flag], SETTINGS, now=NOW)[0] == 0.0


def test_flag_contribution_matches_single_flag_total() -> None:
    flag = make_flag(logit_shift=-0.3, confidence=0.4)
    assert flag_contribution(flag, SETTINGS) == pytest.approx(-0.08)
    assert combine_flags([flag], SETTINGS, now=NOW)[0] == pytest.approx(-0.08)


# ---------------------------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------------------------

_expiry = st.one_of(
    st.none(),
    st.integers(min_value=-10_000, max_value=10_000).map(lambda minutes: NOW + timedelta(minutes=minutes)),
)
_flags = st.lists(
    st.builds(
        ContextFlag,
        flag_type=st.sampled_from(list(ContextFlagType)),
        team=st.sampled_from([None, TeamSlot.TEAM1, TeamSlot.TEAM2]),
        severity=st.floats(0.0, 1.0),
        confidence=st.floats(0.0, 1.0),
        logit_shift=st.floats(allow_nan=True, allow_infinity=True),
        veto=st.booleans(),
        rationale=st.just("r"),
        evidence=st.just([]),
        expires_at=_expiry,
    ),
    max_size=12,
)
_settings = st.builds(
    LLMSettings,
    max_flag_logit_shift=st.floats(0.0, 2.0),
    max_total_logit_shift=st.floats(0.0, 3.0),
)


@given(flags=_flags, settings=_settings)
def test_property_total_is_always_finite_and_bounded(flags: list[ContextFlag], settings: LLMSettings) -> None:
    total, veto = combine_flags(flags, settings, now=NOW)
    assert math.isfinite(total)
    assert abs(total) <= settings.max_total_logit_shift + 1e-12
    live = [f for f in flags if f.expires_at is None or f.expires_at >= NOW]
    assert abs(total) <= sum(settings.max_flag_logit_shift * f.confidence for f in live) + 1e-9
    assert veto == any(f.veto and f.confidence >= 0.6 for f in live)


@given(flags=_flags, settings=_settings, data=st.data())
def test_property_order_does_not_matter(flags: list[ContextFlag], settings: LLMSettings, data: st.DataObject) -> None:
    shuffled = data.draw(st.permutations(flags))
    assert combine_flags(shuffled, settings, now=NOW) == combine_flags(flags, settings, now=NOW)


@given(flags=_flags, settings=_settings)
def test_property_expired_flags_never_change_the_result(flags: list[ContextFlag], settings: LLMSettings) -> None:
    live = [f for f in flags if f.expires_at is None or f.expires_at >= NOW]
    assert combine_flags(flags, settings, now=NOW) == combine_flags(live, settings, now=NOW)


@given(flags=_flags, settings=_settings)
def test_property_mirror_symmetry(flags: list[ContextFlag], settings: LLMSettings) -> None:
    """Negating every shift (swapping the teams' perspective) negates the total and keeps the veto."""
    mirrored = [f.model_copy(update={"logit_shift": -f.logit_shift}) for f in flags]
    total, veto = combine_flags(flags, settings, now=NOW)
    m_total, m_veto = combine_flags(mirrored, settings, now=NOW)
    assert m_total == pytest.approx(-total, abs=1e-12)
    assert m_veto == veto


@given(
    shift=st.floats(-1.0, 1.0),
    low=st.floats(0.0, 1.0),
    high=st.floats(0.0, 1.0),
)
def test_property_more_confidence_never_shrinks_a_single_flag(shift: float, low: float, high: float) -> None:
    low, high = sorted((low, high))
    weak = combine_flags([make_flag(logit_shift=shift, confidence=low)], SETTINGS, now=NOW)[0]
    strong = combine_flags([make_flag(logit_shift=shift, confidence=high)], SETTINGS, now=NOW)[0]
    assert abs(strong) >= abs(weak) - 1e-12


@given(flags=_flags, later=st.integers(min_value=0, max_value=100_000))
def test_property_time_only_removes_flags(flags: list[ContextFlag], later: int) -> None:
    """As time passes flags can only expire: the set of active flags never grows."""
    then = NOW + timedelta(minutes=later)
    assert set(map(id, active_flags(flags, now=then))) <= set(map(id, active_flags(flags, now=NOW)))


def test_utc_constant_used_by_tests_is_aware() -> None:
    assert NOW.tzinfo is UTC
