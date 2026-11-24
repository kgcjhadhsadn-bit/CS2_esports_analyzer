"""deep_data review round 1: regression tests for the issues fixed in the review.

* a series still being played (live: its first maps are already stored) is not a won / lost series;
* the market block is empty when the orientation of the stored prices is unknown;
* a kick-off (training) row reads the market a per-match lead before the start, never the closing
  line the live bot cannot see; live rows read the prices on offer at ``as_of``;
* schedule features are anchored to the match's scheduled start, so a live row computed hours before
  equals the kick-off row when nothing was played in between;
* stage texts ("Elimination stage" is a Swiss stage);
* the high-confidence guard: ``p == threshold``, stricter trust, thin markets, isotonic end plateaus.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from cs2bot.datasets.deep import CoachChangeRecord, DeepData, MarketPoint, MatchMetaRecord
from cs2bot.domain.enums import MatchFormat
from cs2bot.domain.records import MapResultRecord
from cs2bot.features.deep import DeepConfig, DeepFeatureIndex
from cs2bot.features.stakes import classify_stage
from cs2bot.models.calibration import IsotonicCalibrator
from cs2bot.models.confidence import (
    POOLED,
    ConfidenceConfig,
    ConfidenceGuard,
    HighConfidenceShrinker,
    assess_market_trust,
)
from cs2bot.models.ensemble import EnsembleConfig, MapWinEnsemble, temper_isotonic_ends

T0 = datetime(2026, 3, 1, 18, 0, tzinfo=UTC)
H = timedelta(hours=1)
D = timedelta(days=1)


def rec(
    match_id: int,
    t: datetime,
    t1: int,
    t2: int,
    *,
    score: tuple[int, int] = (13, 7),
    map_number: int = 1,
    fmt: MatchFormat | None = MatchFormat.BO3,
) -> MapResultRecord:
    return MapResultRecord(
        match_id=match_id,
        map_number=map_number,
        map_name="mirage",
        played_at=t,
        team1_id=t1,
        team2_id=t2,
        team1_rounds=score[0],
        team2_rounds=score[1],
        format=fmt,
    )


# ------------------------------------------------------------------------------- series in progress


def test_a_series_still_being_played_is_not_a_result() -> None:
    meta = [
        MatchMetaRecord(1, T0 - 2 * H, 1, 3, tournament_id=5, stage="Swiss round 4"),
        MatchMetaRecord(2, T0 + 3 * H, 1, 2, tournament_id=5, stage="Swiss round 5"),
    ]
    live = DeepFeatureIndex([rec(1, T0 - 2 * H, 1, 3)], deep=DeepData(match_meta=meta))  # 1-0 so far
    f1, _, _ = live.features(1, 2, "mirage", T0, match_id=2, exclude_match_id=2)
    assert f1["dp_event_wins"] == 0.0 and f1["dp_event_losses"] == 0.0
    assert math.isnan(f1["dp_bo3_series_win"])
    assert f1["dp_matches_7d"] == 1.0  # the team is playing: it still counts for the schedule load
    done = DeepFeatureIndex(
        [rec(1, T0 - 3 * H, 1, 3), rec(1, T0 - 2 * H, 1, 3, map_number=2)], deep=DeepData(match_meta=meta)
    )
    g1, _, _ = done.features(1, 2, "mirage", T0, match_id=2, exclude_match_id=2)
    assert g1["dp_event_wins"] == 1.0 and g1["dp_bo3_series_win"] == 1.0
    # BO1 complete after one map; unknown format: any decided score counts (old behaviour)
    bo1 = DeepFeatureIndex([rec(1, T0 - 2 * H, 1, 3, fmt=MatchFormat.BO1)], deep=DeepData(match_meta=meta))
    assert bo1.features(1, 2, "mirage", T0, match_id=2)[0]["dp_event_wins"] == 1.0
    unknown = DeepFeatureIndex([rec(1, T0 - 2 * H, 1, 3, fmt=None)], deep=DeepData(match_meta=meta))
    assert unknown.features(1, 2, "mirage", T0, match_id=2)[0]["dp_event_wins"] == 1.0


# -------------------------------------------------------------------------------------- market block

_PRICES = [MarketPoint(T0 - 60 * H, 1.30, 3.50), MarketPoint(T0 - 20 * H, 1.50, 2.60)]


def test_market_block_is_empty_when_the_orientation_is_unknown() -> None:
    no_meta = DeepFeatureIndex([], deep=DeepData(market={7: _PRICES}))
    for t1, t2 in ((1, 2), (2, 1)):
        m = no_meta.features(t1, t2, "mirage", T0 - 30 * H, match_id=7)[2]
        assert math.isnan(m["mkt_p_last"]) and math.isnan(m["mkt_lead_hours"]) and m["mkt_n_moves"] == 0
    meta = [MatchMetaRecord(7, T0, 1, 2)]
    idx = DeepFeatureIndex([], deep=DeepData(match_meta=meta, market={7: _PRICES}))
    other_teams = idx.features(1, 9, "mirage", T0 - 30 * H, match_id=7)[2]
    assert math.isnan(other_teams["mkt_p_last"])
    straight = idx.features(1, 2, "mirage", T0 - 30 * H, match_id=7)[2]
    mirrored = idx.features(2, 1, "mirage", T0 - 30 * H, match_id=7)[2]
    assert straight["mkt_p_last"] > 0.7 and mirrored["mkt_p_last"] == pytest.approx(
        1 - straight["mkt_p_last"]
    )


def test_kickoff_rows_read_the_market_at_a_per_match_lead_live_rows_at_as_of() -> None:
    cfg = DeepConfig(market_lead_hours=(0.25, 48.0))
    leads: list[float] = []
    for mid in range(1, 60):
        meta = [MatchMetaRecord(mid, T0, 1, 2)]
        idx = DeepFeatureIndex([], deep=DeepData(match_meta=meta, market={mid: _PRICES}), config=cfg)
        ref = idx.market_reference(mid, T0.timestamp(), T0.timestamp())
        lead = (T0.timestamp() - ref) / 3600.0
        assert 0.25 <= lead <= 48.0
        assert idx.market_reference(mid, T0.timestamp(), T0.timestamp()) == ref  # deterministic
        m = idx.features(1, 2, "mirage", T0, match_id=mid)[2]  # kick-off row
        assert m["mkt_lead_hours"] == pytest.approx(lead)
        expected_last = 1 / 1.5 / (1 / 1.5 + 1 / 2.6) if lead < 20 else 1 / 1.3 / (1 / 1.3 + 1 / 3.5)
        assert m["mkt_p_last"] == pytest.approx(expected_last)
        assert m["mkt_hours_quoted"] == pytest.approx(60.0 - lead)
        # a later start (delayed match: first map after the scheduled time) is still a kick-off row
        late = idx.features(1, 2, "mirage", T0 + 2 * H, match_id=mid)[2]
        assert late["mkt_lead_hours"] == pytest.approx(lead)
        leads.append(lead)
        # live: 30 h before the start the price on offer then is used, whatever the lead
        live = idx.features(1, 2, "mirage", T0 - 30 * H, match_id=mid)[2]
        assert live["mkt_lead_hours"] == pytest.approx(30.0)
        assert live["mkt_p_last"] == pytest.approx(1 / 1.3 / (1 / 1.3 + 1 / 3.5))
    assert min(leads) < 12 and max(leads) > 36  # spread over the betting window
    # the old behaviour on request: read at kick-off
    meta = [MatchMetaRecord(3, T0, 1, 2)]
    closing = DeepFeatureIndex(
        [], deep=DeepData(match_meta=meta, market={3: _PRICES}), config=DeepConfig(market_lead_hours=None)
    ).features(1, 2, "mirage", T0, match_id=3)[2]
    assert closing["mkt_lead_hours"] == 0.0 and closing["mkt_p_last"] == pytest.approx(
        1 / 1.5 / (1 / 1.5 + 1 / 2.6)
    )
    with pytest.raises(ValueError):
        DeepConfig(market_lead_hours=(5.0, 1.0))


def test_training_rows_do_not_see_prices_after_the_lead() -> None:
    """Leakage: a late steam move (after the row's market reference) must not change the row."""
    for mid in range(1, 30):
        meta = [MatchMetaRecord(mid, T0, 1, 2)]
        late = [*_PRICES, MarketPoint(T0 - timedelta(minutes=5), 1.05, 9.0)]
        with_late = DeepFeatureIndex([], deep=DeepData(match_meta=meta, market={mid: late}))
        without = DeepFeatureIndex([], deep=DeepData(match_meta=meta, market={mid: _PRICES}))
        a = with_late.features(1, 2, "mirage", T0, match_id=mid)[2]
        b = without.features(1, 2, "mirage", T0, match_id=mid)[2]
        for k, v in a.items():
            assert (math.isnan(v) and math.isnan(b[k])) or v == pytest.approx(b[k]), (mid, k)


# ---------------------------------------------------------------------------- schedule anchoring


def test_live_row_equals_kickoff_row_when_nothing_was_played_in_between() -> None:
    """The real live case: the match is not in the store and ``as_of`` is hours before the start."""
    records = [
        rec(1, T0 - 3 * D - 4 * H, 1, 3, fmt=MatchFormat.BO1),
        rec(2, T0 - 2 * D - 2 * H, 2, 4, fmt=MatchFormat.BO1),
        rec(3, T0 - 1 * D - 8 * H, 1, 4, fmt=MatchFormat.BO1),
    ]
    meta = [
        MatchMetaRecord(
            1, T0 - 3 * D - 4 * H, 1, 3, tournament_id=8, location="Cologne, Germany", is_lan=True
        ),
        MatchMetaRecord(
            2, T0 - 2 * D - 2 * H, 2, 4, tournament_id=8, location="Cologne, Germany", is_lan=True
        ),
        MatchMetaRecord(
            3, T0 - 1 * D - 8 * H, 1, 4, tournament_id=8, location="Cologne, Germany", is_lan=True
        ),
        # team 2 plays an earlier series the same day (public schedule), not played yet at 08:00
        MatchMetaRecord(4, T0 - 5 * H, 2, 5, tournament_id=8, location="Cologne, Germany", is_lan=True),
        MatchMetaRecord(9, T0, 1, 2, tournament_id=8, location="Cologne, Germany", is_lan=True),
    ]
    deep = DeepData(match_meta=meta, coach_changes=[CoachChangeRecord(1, date(2026, 2, 1))])
    idx = DeepFeatureIndex(records, deep=deep)
    morning = T0 - 10 * H  # 08:00, before team 2's 13:00 series
    for live, kick in zip(
        idx.features(1, 2, "mirage", morning, match_id=9, exclude_match_id=9)[:2],
        idx.features(1, 2, "mirage", T0, match_id=9, exclude_match_id=9)[:2],  # the kick-off row
        strict=True,
    ):
        for name in (
            "dp_rest_days",
            "dp_maps_3d",
            "dp_maps_7d",
            "dp_matches_3d",
            "dp_same_day_series",
            "dp_play_streak_days",
            "dp_days_at_event",
            "dp_days_since_coach_change",
            "dp_first_after_travel",
        ):
            a, b = live[name], kick[name]
            assert (math.isnan(a) and math.isnan(b)) or a == pytest.approx(b), name
    f1, f2, _ = idx.features(1, 2, "mirage", morning, match_id=9, exclude_match_id=9)
    assert f1["dp_rest_days"] == pytest.approx(1 + 8 / 24)  # to the scheduled start, not to 08:00
    assert f2["dp_same_day_series"] == 1.0 and f1["dp_same_day_series"] == 0.0
    assert f1["dp_days_since_coach_change"] == pytest.approx(28 + 18 / 24)
    assert f2["dp_maps_today"] == 0.0  # nothing played yet today


# ------------------------------------------------------------------------------------------ stages


@pytest.mark.parametrize(
    ("text", "elimination", "playoff", "swiss"),
    [
        ("Elimination stage", False, False, True),
        ("Elimination Stage round 4", False, False, True),
        ("Elimination match", True, False, False),
        ("Group A winners' match", False, False, False),
        ("Group A losers' match", True, False, False),
        ("Lower bracket round 1", True, True, False),
        ("Upper bracket final", False, True, False),
    ],
)
def test_stage_texts(text: str, elimination: bool, playoff: bool, swiss: bool) -> None:
    info = classify_stage(text)
    assert (info.elimination, info.playoff, info.swiss) == (elimination, playoff, swiss)
    assert not classify_stage("Lower bracket round 1").opening and classify_stage("Swiss round 1").opening


# ------------------------------------------------------------------------------ confidence guard


def test_probability_at_the_threshold_is_adjusted_too() -> None:
    rng = np.random.default_rng(1)
    p = np.full(400, 0.76)
    y = (rng.random(400) < 0.60).astype(float)  # first bin badly over-confident
    s = HighConfidenceShrinker().fit(p, y)
    assert s.adjust(0.75) < 0.70  # used to pass unchanged and clear a ">= 0.75" rule
    assert s.adjust(0.7499) <= s.adjust(0.75) + 1e-12  # monotone across the threshold
    assert s.adjust(0.60) == 0.60


def test_trust_needs_a_calibrated_point_estimate_and_no_over_confident_bin() -> None:
    rng = np.random.default_rng(2)
    base = rng.uniform(0.0, 1.0, 3000)
    y_base = (rng.random(3000) < base).astype(float)
    # 400 predictions at 0.90 winning 80 %: the old rule (Wilson upper bound of the whole range at
    # least the mean prediction minus 0.03) let this through
    hp = np.full(400, 0.90)
    hy = np.zeros(400)
    hy[:320] = 1.0
    p = np.concatenate([base, hp])
    y = np.concatenate([y_base, hy])
    trust = assess_market_trust("match_winner", p, y, config=ConfidenceConfig(max_ece=1.0))
    assert not trust.trusted
    assert any("bin [0.900" in r for r in trust.reasons)
    calibrated = assess_market_trust("match_winner", base, y_base)
    assert calibrated.trusted, calibrated.reasons


def test_thin_market_takes_the_more_conservative_of_own_and_pooled() -> None:
    rng = np.random.default_rng(3)
    records = [("match_winner", 0.92, int(rng.random() < 0.72)) for _ in range(3000)]  # over-confident
    records += [("map_winner", 0.92, 1), ("map_winner", 0.92, 1)]  # two lucky wins
    guard = ConfidenceGuard.fit(records)
    own = guard.shrinkers["map_winner"].adjust(0.92)
    pooled = guard.shrinkers[POOLED].adjust(0.92)
    assert pooled < own
    assert guard.adjust("map_winner", 0.92) == pytest.approx(pooled)
    assert guard.adjust("never_seen", 0.92) == pytest.approx(pooled)


def test_isotonic_end_plateaus_are_tempered() -> None:
    rng = np.random.default_rng(4)
    p = rng.uniform(0.02, 0.98, 1500)
    y = (rng.random(1500) < p).astype(float)
    p = np.concatenate([p, np.full(30, 0.995), np.full(30, 0.004)])
    y = np.concatenate([y, np.ones(30), np.zeros(30)])
    plain = IsotonicCalibrator(eps=1e-3).fit(p, y)
    assert plain(0.999) == pytest.approx(0.999)
    tempered = temper_isotonic_ends(IsotonicCalibrator(eps=1e-3).fit(p, y), p, y)
    # the top plateau holds the 30 extra wins plus the uniform samples it pooled with: (k + 1) / (n + 2)
    x_top = plain.x_[int(np.argmax(plain.y_ == plain.y_[-1]))]
    n_top = int((p >= x_top).sum())
    assert n_top >= 30 and tempered(0.999) == pytest.approx((n_top + 1) / (n_top + 2))
    x_low = plain.x_[int(np.argmin(plain.y_ == plain.y_[0])) - 1]
    n_low = int((p <= x_low).sum())
    assert tempered(0.001) == pytest.approx(1 / (n_low + 2))
    assert np.all(np.diff(tempered.y_) >= 0)
    grid = np.linspace(0.3, 0.7, 41)
    assert np.allclose(tempered.transform(grid), plain.transform(grid))  # only the ends move
    # the ensemble uses it for its isotonic calibrator (0 = plain isotonic)
    ens = MapWinEnsemble(EnsembleConfig(min_isotonic_rows=10))
    assert ens._fit_calibrator(p, y, 1500)(0.999) == pytest.approx(tempered(0.999))
    off = MapWinEnsemble(EnsembleConfig(min_isotonic_rows=10, calibration_end_prior=0.0))
    assert off._fit_calibrator(p, y, 1500)(0.999) == pytest.approx(0.999)
