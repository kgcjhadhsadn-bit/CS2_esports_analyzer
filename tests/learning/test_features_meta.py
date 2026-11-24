"""features.meta: active map pool detection, the patch timeline and change-point detection."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest

from cs2bot.domain.records import MapResultRecord
from cs2bot.features import (
    BOCPDConfig,
    FeatureStore,
    PatchTimeline,
    RegimeConfig,
    RegimeDetector,
    bocpd,
    days_since_last_patch,
    detect_active_map_pool,
    form_trend,
    load_patch_timeline,
    patch_id,
    recent_regime_break,
)
from cs2bot.features.meta import DEFAULT_PATCHES_FILE
from cs2bot.testing.synthetic import SynthUniverse
from cs2bot.utils.paths import resolve_path

T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
POOL = ["ancient", "anubis", "dust2", "inferno", "mirage", "nuke", "train"]


@dataclass(frozen=True)
class Played:
    map_name: str
    played_at: datetime


def _spread(maps: list[str], start_day: float, end_day: float, per_map: int) -> list[Played]:
    out = []
    for k, name in enumerate(maps):
        for i in range(per_map):
            day = start_day + (end_day - start_day) * (i + k / len(maps)) / per_map
            out.append(Played(name, T0 + day * DAY))
    return out


# --------------------------------------------------------------------------------------- map pool


def test_pool_is_detected_from_synthetic_results(learning_universe: SynthUniverse):
    maps = learning_universe.map_results()
    as_of = max(m.played_at for m in maps) + DAY
    assert detect_active_map_pool(maps, as_of) == sorted(learning_universe.map_pool)
    # only results strictly before as_of count: at the very beginning there is no evidence
    first = min(m.played_at for m in maps)
    assert detect_active_map_pool(maps, first) == []
    assert detect_active_map_pool([], as_of) == []


def test_pool_rules_share_threshold_rotation_and_noise():
    as_of = T0 + 60 * DAY
    history = _spread(POOL, 0, 60, 20)  # 140 maps, evenly played
    assert detect_active_map_pool(history, as_of) == POOL
    # names are normalised
    odd = [Played(f"de_{p.map_name.title()}", p.played_at) for p in history]
    assert detect_active_map_pool(odd, as_of) == POOL
    # a showmatch map with a 2 % share stays out, and so does anything outside the window
    noise = [Played("cache", T0 + d * DAY) for d in (10, 30, 50)]
    old = [Played("vertigo", T0 - (5 + d) * DAY) for d in range(40)]
    assert detect_active_map_pool(history + noise + old, as_of) == POOL
    # with a lower threshold the rare map would count - and the eighth map is cut by play share
    assert "cache" not in detect_active_map_pool(history + noise, as_of, min_share=0.01)
    assert "cache" in detect_active_map_pool(history + noise, as_of, min_share=0.01, pool_size=None)
    # too little evidence
    assert detect_active_map_pool(history[:20], as_of) == []

    # rotation two weeks ago: train out, overpass in. The long window still holds more train
    # (46 days of it) than overpass, but the recent window settles it.
    rotated = [p for p in history if not (p.map_name == "train" and p.played_at >= T0 + 46 * DAY)]
    rotated += _spread(["overpass"], 46, 60, 8)
    pool = detect_active_map_pool(rotated, as_of)
    assert pool == sorted([m for m in POOL if m != "train"] + ["overpass"])
    # one week before the rotation the old pool is still reported from the same data
    assert detect_active_map_pool(rotated, T0 + 40 * DAY, min_maps=20) == POOL


# --------------------------------------------------------------------------------- patch timeline


def test_the_patch_file_loads_and_is_consistent():
    timeline = load_patch_timeline()
    assert timeline is load_patch_timeline()  # cached
    assert len(timeline) >= 10
    patches = timeline.patches
    assert patches[0].id == "cs2-release" and patches[0].date == date(2023, 9, 27)
    assert [p.date for p in patches] == sorted(p.date for p in patches)
    assert len({p.id for p in patches}) == len(patches)
    assert all(p.date >= date(2023, 9, 27) for p in patches)
    assert all(p.title and p.notes for p in patches)
    assert {p.impact for p in patches} <= {"minor", "moderate", "major"}
    # every pool has seven maps and each rotation swaps exactly one map
    pools = [p.map_pool for p in patches if p.map_pool]
    assert len(pools) >= 4 and all(len(pool) == 7 for pool in pools)
    for before, after in pairwise(pools):
        assert len(set(before) ^ set(after)) == 2
    # unverified dates are marked as such in the notes as well (the owner must be able to see it)
    for p in patches:
        if not p.date_verified:
            assert "UNVERIFIED" in p.notes, p.id
    assert any(p.date_verified for p in patches) and any(not p.date_verified for p in patches)


def test_patch_lookups():
    timeline = load_patch_timeline()
    before_cs2 = datetime(2023, 9, 1, tzinfo=UTC)
    assert timeline.patch_id(before_cs2) is None and patch_id(before_cs2) is None
    assert math.isnan(timeline.days_since_last_patch(before_cs2))
    assert timeline.patch_index(before_cs2) == -1 and timeline.map_pool_at(before_cs2) is None

    release_day = datetime(2023, 9, 27, tzinfo=UTC)
    assert patch_id(release_day) == "cs2-release"  # effective from 00:00 UTC of the release day
    assert days_since_last_patch(release_day + timedelta(days=10, hours=12)) == pytest.approx(10.5)
    assert "overpass" in timeline.map_pool_at(release_day) and "vertigo" in timeline.map_pool_at(release_day)

    # the day before / of / after the first pool change
    change = datetime(2024, 4, 25, tzinfo=UTC)
    assert patch_id(change - timedelta(seconds=1)) != "2024-04-25-dust2-in-overpass-out"
    assert patch_id(change) == "2024-04-25-dust2-in-overpass-out"
    assert "dust2" in timeline.map_pool_at(change) and "dust2" not in timeline.map_pool_at(change - DAY)
    assert timeline.days_since_last_patch(change + 3 * DAY, kinds=("map_pool",)) == pytest.approx(3.0)
    assert timeline.days_since_pool_change(change + 3 * DAY) == pytest.approx(3.0)
    assert math.isnan(timeline.days_since_pool_change(change - DAY))
    # review round 1: "Train added to the game" (kind map_pool, no new pool) is not a pool change
    train_added = datetime(2024, 11, 13, tzinfo=UTC)
    assert patch_id(train_added) == "2024-11-13-train-returns"
    expected = (train_added + DAY - change).days
    assert timeline.days_since_pool_change(train_added + DAY) == pytest.approx(expected)
    store = FeatureStore.build([], patch_timeline=timeline)
    assert store.meta_features(train_added + DAY)["days_since_pool_change"] == pytest.approx(expected)
    # minor patches are skipped by the default "moderate" filter but visible on request
    feb = datetime(2024, 2, 20, tzinfo=UTC)
    assert patch_id(feb) == "2024-02-06-call-to-arms"
    assert days_since_last_patch(feb) == pytest.approx((feb - release_day).days)
    assert days_since_last_patch(feb, min_impact="minor") == pytest.approx(14.0)
    assert timeline.patch_index(feb) == 1 and timeline.patch_index(feb, min_impact="major") == 0
    # naive datetimes are read as UTC
    assert patch_id(datetime(2024, 4, 25)) == "2024-04-25-dust2-in-overpass-out"
    today = datetime(2026, 11, 23, tzinfo=UTC)
    assert timeline.patch_index(today) == len(timeline) - 1
    assert timeline.map_pool_at(today) == sorted(timeline.map_pool_at(today))


def test_patch_file_errors_are_loud(tmp_path: Path):
    good = tmp_path / "patches.yaml"
    good.write_text(
        "patches:\n"
        "  - {id: b, date: 2024-05-01, impact: major, map_pool: [de_Mirage, Dust II]}\n"
        "  - {id: a, date: 2024-01-01, kind: economy}\n",
        encoding="utf-8",
    )
    timeline = PatchTimeline.from_yaml(good)
    assert [p.id for p in timeline.patches] == ["a", "b"]  # sorted by date
    assert timeline.patches[1].map_pool == ("dust2", "mirage")
    assert timeline.patches[0].impact == "moderate" and not timeline.patches[0].date_verified
    custom = datetime(2024, 6, 1, tzinfo=UTC)
    assert days_since_last_patch(custom, timeline=timeline) == pytest.approx(31.0)
    assert patch_id(custom, timeline=timeline) == "b"

    for text in (
        "patches:\n  - {id: x, date: not-a-date}\n",
        "patches:\n  - {id: x, date: 2024-01-01, impact: huge}\n",
        "patches:\n  - {date: 2024-01-01}\n",
        "something_else: []\n",
        "patches:\n  - {id: x, date: 2024-01-01}\n  - {id: x, date: 2024-02-01}\n",
    ):
        bad = tmp_path / "bad.yaml"
        bad.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError):
            PatchTimeline.from_yaml(bad)
    with pytest.raises(OSError):
        PatchTimeline.from_yaml(tmp_path / "missing.yaml")
    # the store keeps working without a timeline: patch features are NaN
    store = FeatureStore.build([], patch_timeline=None)
    assert all(math.isnan(v) for v in store.meta_features(custom).values())
    with_timeline = FeatureStore.build([], patch_timeline=timeline).meta_features(custom)
    assert with_timeline["days_since_patch"] == 31.0 and with_timeline["patch_index"] == 1.0
    assert math.isnan(with_timeline["days_since_pool_change"])  # no entry of kind "map_pool"
    assert resolve_path(DEFAULT_PATCHES_FILE).is_file()


# ---------------------------------------------------------------------- change-point detection


def _series(n_before: int, n_after: int, shift: float, *, seed: int = 0, sd: float = 0.11) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.concatenate([rng.normal(0.0, sd, n_before), rng.normal(shift, sd, n_after)])


def test_bocpd_finds_an_injected_break():
    found = 0
    for seed in range(10):
        values = _series(80, 40, 0.15, seed=seed)
        result = bocpd(values)
        assert len(result.map_run_length) == len(result.p_recent_change) == 120
        if any(70 <= cp <= 95 for cp in result.change_points):
            found += 1
        # long before the break the filter is calm
        assert result.p_recent_change[40:78].mean() < 0.25
    assert found >= 8
    # a clear example: the break is located within a few maps, the level estimate follows
    values = _series(80, 40, 0.20, seed=3)
    result = bocpd(values)
    assert any(abs(cp - 80) <= 4 for cp in result.change_points)
    assert result.p_recent_change[85:92].max() > 0.5
    assert abs(result.regime_mean[75]) < 0.06 and result.regime_mean[-1] == pytest.approx(0.20, abs=0.06)
    assert result.map_run_length[-1] == pytest.approx(40, abs=6)


def test_bocpd_is_calm_without_a_break_and_strictly_online():
    alarms = []
    for seed in range(20):
        result = bocpd(_series(200, 0, 0.0, seed=100 + seed))
        alarms.append((result.p_recent_change[30:] >= 0.5).mean())
    assert float(np.mean(alarms)) < 0.03
    # online: the value after observation i never changes when later observations arrive
    values = _series(60, 40, -0.2, seed=5)
    full = bocpd(values)
    prefix = bocpd(values[:70])
    assert np.array_equal(full.map_run_length[:70], prefix.map_run_length)
    assert np.allclose(full.p_recent_change[:70], prefix.p_recent_change)
    assert np.allclose(full.regime_mean[:70], prefix.regime_mean)
    # missing observations carry the state forward, empty input is fine, config is honoured
    holes = values.copy()
    holes[10] = np.nan
    assert np.isfinite(bocpd(holes).p_recent_change).all()
    assert len(bocpd([]).map_run_length) == 0 and bocpd([]).change_points == ()
    short = bocpd(values, BOCPDConfig(max_run_length=20))
    assert short.map_run_length.max() <= 20


def _detector(values: np.ndarray, *, config: RegimeConfig | None = None) -> RegimeDetector:
    times = np.array([(T0 + i * DAY).timestamp() for i in range(len(values))])
    return RegimeDetector({7: (times, values, np.arange(len(values)))}, config)


def test_regime_detector_reports_a_recent_break_only_after_it_happened():
    cfg = RegimeConfig(warmup_maps=0)
    values = _series(80, 40, 0.20, seed=3)
    detector = _detector(values, config=cfg)
    before = detector.recent_regime_break(7, T0 + 79.5 * DAY)
    assert not before.detected and before.probability < 0.3
    after = recent_regime_break(7, T0 + 90.5 * DAY, detector=detector)
    assert after.detected and after.probability >= 0.5
    assert after.shift == pytest.approx(0.20, abs=0.08) and 5 <= after.maps_in_regime <= 14
    # long after the break it is no longer "recent"
    late = detector.recent_regime_break(7, T0 + 119.5 * DAY)
    assert not late.detected and late.maps_in_regime > 25
    # the trend follows the break and settles again
    assert form_trend(7, T0 + 90.5 * DAY, detector=detector) > 0.04
    assert abs(detector.form_trend(7, T0 + 79.5 * DAY)) < 0.04
    # a slump is found too (how many maps the filter needs varies from series to series)
    for seed in range(4, 9):
        down = _detector(_series(80, 40, -0.20, seed=seed), config=cfg)
        reports = [down.recent_regime_break(7, T0 + (day + 0.5) * DAY) for day in range(80, 100)]
        slump = max(reports, key=lambda r: r.probability)
        assert slump.detected and slump.shift < -0.1
        assert min(down.form_trend(7, T0 + (day + 0.5) * DAY) for day in range(80, 100)) < -0.04

    # leakage: what is reported at day 79.5 does not depend on anything that came later
    truncated = _detector(values[:80], config=cfg)
    assert truncated.recent_regime_break(7, T0 + 79.5 * DAY) == before
    assert truncated.form_trend(7, T0 + 79.5 * DAY) == detector.form_trend(7, T0 + 79.5 * DAY)
    # excluding the match being predicted moves the reference back by its maps
    assert detector.recent_regime_break(
        7, T0 + 90.5 * DAY, exclude_match_id=90
    ) == detector.recent_regime_break(7, T0 + 89.5 * DAY)
    # unknown team, no history yet, too little history
    nothing = detector.recent_regime_break(99, T0)
    assert (
        not nothing.detected and math.isnan(nothing.probability) and math.isnan(detector.form_trend(99, T0))
    )
    assert not detector.recent_regime_break(7, T0).detected
    young = detector.recent_regime_break(7, T0 + 10.5 * DAY)
    assert not young.detected and math.isnan(young.probability)
    assert math.isnan(detector.form_trend(7, T0 + 3.5 * DAY))


def test_warmup_maps_are_ignored():
    # a newcomer the strength proxy underrates: big positive residuals at first, then level
    values = np.concatenate([np.full(12, 0.25), _series(60, 0, 0.0, seed=8)])
    naive = _detector(values, config=RegimeConfig(warmup_maps=0, min_maps=20))
    patient = _detector(values, config=RegimeConfig(warmup_maps=15, min_maps=20))
    as_of = T0 + 45.5 * DAY
    assert naive.recent_regime_break(7, as_of).shift < -0.15  # the warm-up looks like a collapse
    report = patient.recent_regime_break(7, as_of)
    assert not report.detected and (math.isnan(report.shift) or abs(report.shift) < 0.1)


def _season(strength_after: float, *, seed: int = 1) -> list[MapResultRecord]:
    """Team 1 plays 120 maps against average opponents; its level changes after map 80."""
    rng = np.random.default_rng(seed)
    records = []
    for i in range(120):
        p_round = 0.5 if i < 80 else strength_after
        opponent = 2 + i % 6
        won = int(rng.binomial(24, p_round))
        score = (13, min(12, 24 - won)) if won >= 12 else (max(0, won), 13)
        records.append(
            MapResultRecord(
                match_id=i + 1,
                map_number=1,
                map_name=POOL[i % 7],
                played_at=T0 + i * DAY,
                team1_id=1,
                team2_id=opponent,
                team1_rounds=score[0],
                team2_rounds=score[1],
            )
        )
        # the opponents also play each other so that they stay "average"
        other = 2 + (i + 3) % 6
        if other != opponent:
            a = int(rng.binomial(24, 0.5))
            s = (13, min(12, 24 - a)) if a >= 12 else (a, 13)
            records.append(
                MapResultRecord(
                    match_id=1000 + i,
                    map_number=1,
                    map_name=POOL[i % 7],
                    played_at=T0 + i * DAY + timedelta(hours=3),
                    team1_id=opponent,
                    team2_id=other,
                    team1_rounds=s[0],
                    team2_rounds=s[1],
                )
            )
    return records


def test_store_detects_a_team_whose_level_changed():
    records = _season(0.68)
    store = FeatureStore.build(records, patch_timeline=None)
    calm = store.recent_regime_break(1, T0 + 79.5 * DAY)
    assert not calm.detected
    probabilities = [store.recent_regime_break(1, T0 + (d + 0.5) * DAY).probability for d in range(82, 100)]
    assert max(probabilities) >= 0.5
    best = max(range(82, 100), key=lambda d: store.recent_regime_break(1, T0 + (d + 0.5) * DAY).probability)
    report = store.recent_regime_break(1, T0 + (best + 0.5) * DAY)
    assert report.detected and report.shift > 0.04
    assert max(store.form_trend(1, T0 + (d + 0.5) * DAY) for d in range(82, 95)) > 0.02
    features = store.team_features(1, "mirage", T0 + (best + 0.5) * DAY)
    assert features["regime_break_prob"] == pytest.approx(report.probability)
    assert features["regime_shift"] == pytest.approx(report.shift)
    assert features["form_trend"] == pytest.approx(store.form_trend(1, T0 + (best + 0.5) * DAY))
    # a team that stays the same raises no alarm over the same period
    steady = FeatureStore.build(_season(0.5), patch_timeline=None)
    flags = [steady.recent_regime_break(1, T0 + (d + 0.5) * DAY).detected for d in range(60, 120)]
    assert sum(flags) <= 6
    # the detector built directly from map results agrees with the one inside the store
    direct = RegimeDetector.from_map_results(records)
    assert direct.recent_regime_break(1, T0 + (best + 0.5) * DAY) == report
