"""Feature rows: build_training_rows, features_for_match_map and the RatingProvider protocol."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from sklearn.metrics import roc_auc_score
from sqlalchemy.orm import Session

from cs2bot import datasets
from cs2bot.config import Settings
from cs2bot.domain.enums import MatchFormat, Side, TeamSlot, Tier, VetoAction
from cs2bot.domain.match import LineupPlayer, MatchContext
from cs2bot.domain.predictions import MapWinEstimate
from cs2bot.domain.records import MapResultRecord, VetoStep
from cs2bot.features import (
    META_COLUMNS,
    RATING_LOGIT_FEATURE,
    FeatureConfig,
    FeatureStore,
    RatingProvider,
    build_training_rows,
    features_for_match_map,
    match_map_features,
)
from cs2bot.testing.synthetic import SynthUniverse
from tests.learning.learning_helpers import same_features


class FakeRatings:
    """A provider that records every question it is asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, str, datetime, bool]] = []

    def predict(
        self, team1_id: int, team2_id: int, map_name: str, as_of: datetime, *, is_lan: bool = False
    ) -> MapWinEstimate:
        self.calls.append((team1_id, team2_id, map_name, as_of, is_lan))
        p = 1.0 / (1.0 + math.exp(-(team2_id - team1_id) / 10.0))
        return MapWinEstimate(
            map_name=map_name,
            p_team1_win=p,
            sigma=0.07,
            components={"overall": p, "games": 12},
        )


class HistoricalRatings(FakeRatings):
    """Like a rating model fitted with ``record_history=True``."""

    records_history = True

    def __init__(self) -> None:
        super().__init__()
        self.historical_calls = 0

    def predict(self, *args, **kwargs) -> MapWinEstimate:  # type: ignore[no-untyped-def]
        raise ValueError("as_of is not after the last processed game")

    def historical_predict(
        self, team1_id: int, team2_id: int, map_name: str, as_of: datetime, *, is_lan: bool = False
    ) -> MapWinEstimate:
        self.historical_calls += 1
        return FakeRatings.predict(self, team1_id, team2_id, map_name, as_of, is_lan=is_lan)


@pytest.fixture(scope="module")
def maps(learning_universe: SynthUniverse) -> list[MapResultRecord]:
    return sorted(learning_universe.map_results(), key=lambda r: (r.played_at, r.match_id, r.map_number))


@pytest.fixture(scope="module")
def store(learning_universe: SynthUniverse, maps: list[MapResultRecord]) -> FeatureStore:
    return FeatureStore.build(
        maps,
        player_maps=learning_universe.player_map_records(),
        rounds=learning_universe.round_records(),
        vetoes=learning_universe.veto_records(),
    )


def test_training_rows_shape_labels_and_meta(maps: list[MapResultRecord], store: FeatureStore):
    x, y, meta = build_training_rows(maps, store=store)
    assert len(x) == len(y) == len(meta) == len(maps)
    assert isinstance(x, pd.DataFrame) and isinstance(y, np.ndarray)
    assert list(meta.columns) == list(META_COLUMNS)
    assert all(str(dtype) == "float64" for dtype in x.dtypes)
    assert not any(col.startswith("rating_") for col in x.columns)
    # labels and identity columns follow the (chronological) input
    assert y.tolist() == [1.0 if m.team1_rounds > m.team2_rounds else 0.0 for m in maps]
    assert meta["match_id"].tolist() == [m.match_id for m in maps]
    assert meta["map_number"].tolist() == [m.map_number for m in maps]
    assert meta["map_name"].tolist() == [m.map_name for m in maps]
    assert not meta["mirrored"].any()
    # as_of is the start of the match for every map of a series, never later than the map itself
    first_map = {}
    for m in maps:
        first_map[m.match_id] = min(first_map.get(m.match_id, m.played_at), m.played_at)
    assert [t.to_pydatetime() for t in meta["as_of"]] == [first_map[m.match_id] for m in maps]
    assert (meta["as_of"] <= meta["played_at"]).all()
    # every block is present
    for name in (
        "t1_win_rate_90d",
        "t2_win_rate_90d",
        "diff_win_rate_90d",
        "diff_roster_continuity",
        "diff_pl_kpr_mean",
        "diff_ct_win_pct_90d",
        "diff_pool_depth",
        "h2h_win_rate",
        "strength_p_win",
        "is_lan",
        "tier_rank",
        "best_of",
        "pick_team1",
        "pick_team2",
        "pick_diff",
        "map_ct_round_share_180d",
        "days_since_patch",
    ):
        assert name in x.columns, name
    assert "team1_starts_ct" not in x.columns  # off by default
    row = x.iloc[-1]
    rec = maps[-1]
    assert row["diff_win_rate_90d"] == pytest.approx(row["t1_win_rate_90d"] - row["t2_win_rate_90d"])
    assert row["is_lan"] == float(rec.is_lan)
    assert row["tier_rank"] == {Tier.S: 5.0, Tier.A: 4.0, Tier.B: 3.0}[rec.tier]
    assert row["best_of"] == rec.format.n_maps
    picks = {TeamSlot.TEAM1: (1.0, 0.0, 1.0), TeamSlot.TEAM2: (0.0, 1.0, -1.0), None: (0.0, 0.0, 0.0)}
    for rec, (_, row) in zip(maps[-30:], x.iloc[-30:].iterrows(), strict=True):
        assert (row["pick_team1"], row["pick_team2"], row["pick_diff"]) == picks[rec.picked_by]
    # the very first match has no history at all: counts are 0, rates NaN, nothing crashes
    assert x.iloc[0]["t1_maps_90d"] == 0 and math.isnan(x.iloc[0]["t1_win_rate_90d"])
    assert x.iloc[0]["h2h_maps"] == 0

    # subsets and empty input
    sub_x, _, _ = build_training_rows(maps[100:110], store=store)
    assert same_features(sub_x.iloc[3].to_dict(), x.iloc[103].to_dict()) == []
    empty_x, empty_y, empty_meta = build_training_rows([], store=store)
    assert empty_x.empty and empty_y.shape == (0,) and list(empty_meta.columns) == list(META_COLUMNS)


def test_features_carry_signal_about_the_winner(maps: list[MapResultRecord], store: FeatureStore):
    x, y, _ = build_training_rows(maps, store=store)
    late = slice(len(x) // 3, None)  # skip the cold start
    for name in ("strength_p_win", "diff_win_rate_90d", "diff_elo", "diff_pl_kpr_mean"):
        values = x[name].to_numpy()[late]
        ok = ~np.isnan(values)
        assert roc_auc_score(y[late][ok], values[ok]) > 0.6, name
    # picking a map is an advantage in the generator (teams pick their relatively best map)
    picked = x["pick_diff"].to_numpy()
    assert y[picked == 1].mean() > y[picked == -1].mean()


def test_mirrored_rows_are_antisymmetric(maps: list[MapResultRecord], store: FeatureStore):
    ratings = FakeRatings()
    x, y, meta = build_training_rows(maps[200:230], store=store, rating_provider=ratings, mirror=True)
    assert len(x) == 60 and meta["mirrored"].tolist() == [False, True] * 30
    a, b = x.iloc[0::2].reset_index(drop=True), x.iloc[1::2].reset_index(drop=True)
    assert (y[0::2] + y[1::2] == 1.0).all()
    assert (meta["team1_id"].iloc[0::2].to_numpy() == meta["team2_id"].iloc[1::2].to_numpy()).all()
    for col in x.columns:
        if col.startswith("t1_"):
            other = b["t2_" + col[3:]]
            assert np.allclose(a[col], other, equal_nan=True), col
        elif col.startswith("diff_") or col in (
            "pick_diff",
            "strength_logit",
            "h2h_round_diff",
            RATING_LOGIT_FEATURE,
        ):
            assert np.allclose(a[col], -b[col], equal_nan=True, atol=1e-9), col
        elif col in ("strength_p_win", "h2h_win_rate", "h2h_map_win_rate", "rating_p"):
            assert np.allclose(a[col], 1.0 - b[col], equal_nan=True), col
        elif col in ("is_lan", "tier_rank", "best_of", "h2h_maps", "days_since_patch", "rating_sigma"):
            assert np.allclose(a[col], b[col], equal_nan=True), col
    assert (a["pick_team1"] == b["pick_team2"]).all()


def test_rating_provider_is_asked_as_of_the_match_start(maps: list[MapResultRecord], store: FeatureStore):
    ratings = FakeRatings()
    assert isinstance(ratings, RatingProvider)
    chunk = maps[150:190]
    x, _, meta = build_training_rows(chunk, store=store, rating_provider=ratings)
    assert len(ratings.calls) == len(chunk)
    for rec, call, as_of in zip(chunk, ratings.calls, meta["as_of"], strict=True):
        team1, team2, map_name, asked_at, is_lan = call
        assert (team1, team2, map_name, is_lan) == (rec.team1_id, rec.team2_id, rec.map_name, rec.is_lan)
        assert asked_at == as_of.to_pydatetime() <= rec.played_at
    p = np.array([1.0 / (1.0 + math.exp(-(r.team2_id - r.team1_id) / 10.0)) for r in chunk])
    assert np.allclose(x["rating_p"], p)
    assert np.allclose(x[RATING_LOGIT_FEATURE], np.log(p / (1 - p)))
    assert (x["rating_sigma"] == 0.07).all()
    assert np.allclose(x["rating_c_overall"], p) and (x["rating_c_games"] == 12.0).all()
    assert [c for c in x.columns if c.startswith("rating_")] == [
        "rating_p",
        "rating_logit",
        "rating_sigma",
        "rating_c_overall",
        "rating_c_games",
    ]

    # a provider with a history-aware entry point is asked through it (its predict() would refuse)
    historical = HistoricalRatings()
    x2, _, _ = build_training_rows(chunk, store=store, rating_provider=historical)
    assert historical.historical_calls == len(chunk)
    assert np.allclose(x2["rating_p"], p)
    # ... unless it did not record history: then the refusal is passed on, never swallowed
    historical.records_history = False  # type: ignore[misc]
    with pytest.raises(ValueError, match="not after the last processed game"):
        build_training_rows(chunk, store=store, rating_provider=historical)

    # extreme probabilities keep the logit finite
    class Certain(FakeRatings):
        def predict(self, *a, **k) -> MapWinEstimate:  # type: ignore[no-untyped-def]
            return MapWinEstimate(map_name="x", p_team1_win=1.0, sigma=0.0)

    x3, _, _ = build_training_rows(chunk[:2], store=store, rating_provider=Certain())
    assert np.isfinite(x3[RATING_LOGIT_FEATURE]).all() and (x3[RATING_LOGIT_FEATURE] > 10).all()


def test_real_rating_model_plugs_in(maps: list[MapResultRecord], store: FeatureStore):
    ratings_module = pytest.importorskip("cs2bot.models.ratings")
    model = ratings_module.MapRatingModel().fit(maps, record_history=True)
    assert isinstance(model, RatingProvider)
    chunk = maps[250:300]
    x, _, _ = build_training_rows(chunk, store=store, rating_provider=model)
    assert x["rating_p"].between(0.0, 1.0).all() and (x["rating_sigma"] > 0).all()
    assert "rating_c_logit_mean" in x.columns
    # leakage-free: identical to a model that only ever saw the games before each match
    rec = chunk[10]
    start = min(m.played_at for m in maps if m.match_id == rec.match_id)
    strict = ratings_module.MapRatingModel().fit([m for m in maps if m.played_at < start])
    expected = strict.predict(rec.team1_id, rec.team2_id, rec.map_name, start, is_lan=rec.is_lan)
    assert x["rating_p"].iloc[10] == pytest.approx(expected.p_team1_win, abs=1e-9)
    # without recorded history the model refuses past timestamps and the error is not hidden
    plain = ratings_module.MapRatingModel().fit(maps)
    with pytest.raises(ValueError):
        build_training_rows(chunk, store=store, rating_provider=plain)


def test_live_features_equal_the_training_row_of_the_same_match(
    learning_db: Session,
    learning_universe: SynthUniverse,
    maps: list[MapResultRecord],
    store: FeatureStore,
    settings: Settings,
):
    x, _, meta = build_training_rows(maps, store=store)
    for row in (len(maps) - 1, len(maps) - 40, len(maps) // 2):
        rec = maps[row]
        ctx = datasets.load_match_context(learning_db, rec.match_id, settings=settings)
        as_of = meta["as_of"].iloc[row].to_pydatetime()
        live = features_for_match_map(ctx, rec.map_name, store=store, as_of=as_of)
        assert same_features(live, x.iloc[row].to_dict()) == []
        # asking later (the match is long over) is clamped to its start: still the pre-match view
        late = features_for_match_map(
            ctx, f"de_{rec.map_name}", store=store, as_of=as_of + timedelta(days=200)
        )
        assert same_features(late, live) == []
        # asking earlier really is an earlier view
        early = features_for_match_map(ctx, rec.map_name, store=store, as_of=as_of - timedelta(days=60))
        assert early["t1_maps_alltime"] < live["t1_maps_alltime"]


def _ctx(**kw) -> MatchContext:  # type: ignore[no-untyped-def]
    base = {
        "match_id": 10_000,
        "scheduled_at": datetime(2025, 6, 1, tzinfo=UTC),
        "format": MatchFormat.BO3,
        "team1_id": 1,
        "team2_id": 2,
        "tier": Tier.S,
        "is_lan": True,
    }
    return MatchContext(**{**base, **kw})


def test_pick_indicator_from_the_veto_or_a_hypothesis(store: FeatureStore):
    veto = [
        VetoStep(step=1, action=VetoAction.BAN, team=TeamSlot.TEAM1, map_name="nuke"),
        VetoStep(step=2, action=VetoAction.PICK, team=TeamSlot.TEAM2, map_name="Mirage"),
        VetoStep(step=3, action=VetoAction.PICK, team=TeamSlot.TEAM1, map_name="inferno"),
        VetoStep(step=4, action=VetoAction.LEFTOVER, team=None, map_name="ancient"),
    ]
    ctx = _ctx(known_veto=veto)
    as_of = ctx.scheduled_at

    def pick(map_name: str, **kw) -> float:  # type: ignore[no-untyped-def]
        return features_for_match_map(ctx, map_name, store=store, as_of=as_of, **kw)["pick_diff"]

    assert pick("mirage") == -1.0 and pick("inferno") == 1.0
    assert pick("ancient") == 0.0  # decider
    assert pick("nuke") == 0.0  # banned: not a pick
    assert pick("dust2") == 0.0  # not in the veto
    # hypothetical veto outcomes override the published veto
    assert pick("mirage", picked_by=TeamSlot.TEAM1) == 1.0
    assert pick("mirage", picked_by=None) == 0.0
    assert pick("dust2", picked_by=TeamSlot.TEAM2) == -1.0
    row = features_for_match_map(ctx, "mirage", store=store, as_of=as_of)
    assert (row["is_lan"], row["tier_rank"], row["best_of"]) == (1.0, 5.0, 3.0)
    unranked = features_for_match_map(_ctx(tier=Tier.UNRANKED), "mirage", store=store, as_of=as_of)
    assert math.isnan(unranked["tier_rank"])


def test_lineups_and_standins_come_from_the_context(store: FeatureStore, learning_universe: SynthUniverse):
    usual1 = [p.id for p in learning_universe.teams[1].players]
    usual2 = [p.id for p in learning_universe.teams[2].players]
    as_of = store.map_results[-1].played_at + timedelta(days=1)
    known = _ctx(
        team1_lineup=[LineupPlayer(player_id=p) for p in usual1],
        team2_lineup=[LineupPlayer(player_id=p, is_standin=p == usual2[0]) for p in usual2],
    )
    f = features_for_match_map(known, "mirage", store=store, as_of=as_of)
    assert f["t1_lineup_known"] == 1 and f["t1_standin_count"] == 0 and f["t2_standin_count"] == 1
    assert f["t1_roster_continuity"] == 1.0 and f["t1_roster_new_players"] == 0
    # team 1 fields two players from another team
    borrowed = [p.id for p in learning_universe.teams[3].players][:2]
    changed = _ctx(team1_lineup=[LineupPlayer(player_id=p) for p in usual1[:3] + borrowed])
    g = features_for_match_map(changed, "mirage", store=store, as_of=as_of)
    assert g["t1_roster_continuity"] == pytest.approx(0.6) and g["t1_roster_new_players"] == 2
    assert g["t1_days_since_roster_change"] == 0 and g["t1_pl_known"] == 5
    assert g["t1_pl_kpr_mean"] != pytest.approx(f["t1_pl_kpr_mean"])
    # no lineup in the context: the last fielded one is assumed, stand-ins unknown
    assert g["t2_lineup_known"] == 0 and math.isnan(g["t2_standin_count"])
    assert g["t2_pl_kpr_mean"] == pytest.approx(f["t2_pl_kpr_mean"])


def test_start_side_feature_is_opt_in(maps: list[MapResultRecord]):
    store = FeatureStore.build(maps, config=FeatureConfig(use_start_side=True))
    x, _, _ = build_training_rows(maps[-80:], store=store)
    for rec, (_, row) in zip(maps[-80:], x.iterrows(), strict=True):
        if rec.picked_by is None:  # decider: the knife round has not happened before the match
            assert math.isnan(row["team1_starts_ct"])
        else:
            assert row["team1_starts_ct"] == (1.0 if rec.team1_start_side is Side.CT else 0.0)
    ctx = _ctx(start_sides={"mirage": Side.T, "nuke": Side.CT})
    kwargs = {"store": store, "as_of": ctx.scheduled_at}
    assert features_for_match_map(ctx, "mirage", picked_by=TeamSlot.TEAM1, **kwargs)["team1_starts_ct"] == 0.0
    assert features_for_match_map(ctx, "nuke", picked_by=TeamSlot.TEAM2, **kwargs)["team1_starts_ct"] == 1.0
    assert math.isnan(features_for_match_map(ctx, "nuke", picked_by=None, **kwargs)["team1_starts_ct"])
    assert math.isnan(
        features_for_match_map(ctx, "dust2", picked_by=TeamSlot.TEAM1, **kwargs)["team1_starts_ct"]
    )


def test_match_map_features_is_the_shared_code_path(maps: list[MapResultRecord], store: FeatureStore):
    rec = maps[-5]
    start = store.match_start(rec.match_id)
    assert start is not None
    direct = match_map_features(
        store,
        team1_id=rec.team1_id,
        team2_id=rec.team2_id,
        map_name=rec.map_name,
        as_of=start,
        team1_lineup=rec.team1_lineup,
        team2_lineup=rec.team2_lineup,
        team1_standins=0,
        team2_standins=0,
        is_lan=rec.is_lan,
        tier=rec.tier,
        fmt=rec.format,
        picked_by=rec.picked_by,
        exclude_match_id=rec.match_id,
    )
    x, _, _ = build_training_rows([m for m in maps if m.match_id == rec.match_id], store=store)
    assert same_features(direct, x.iloc[rec.map_number - 1].to_dict()) == []
    assert all(isinstance(v, float) for v in direct.values())
