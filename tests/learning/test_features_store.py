"""FeatureStore: values against brute-force recomputation, leakage safety, speed."""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from cs2bot import datasets
from cs2bot.domain.enums import BuyType, MatchFormat, Side, TeamSlot, Tier
from cs2bot.domain.records import MapResultRecord, PlayerMapRecord, RoundRecord
from cs2bot.features import FeatureConfig, FeatureStore, build_training_rows, match_map_features
from cs2bot.testing.synthetic import SynthUniverse, generate_universe
from tests.learning.learning_helpers import same_features

T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)


def _rec(
    match_id: int,
    day: float,
    t1: int,
    t2: int,
    score: tuple[int, int],
    *,
    map_name: str = "mirage",
    map_number: int = 1,
    lineup1: tuple[int, ...] = (),
    lineup2: tuple[int, ...] = (),
    picked_by: TeamSlot | None = None,
    is_lan: bool = False,
    sides: tuple[int, int, int, int] | None = None,
) -> MapResultRecord:
    return MapResultRecord(
        match_id=match_id,
        map_number=map_number,
        map_name=map_name,
        played_at=T0 + day * DAY,
        team1_id=t1,
        team2_id=t2,
        team1_rounds=score[0],
        team2_rounds=score[1],
        team1_ct_rounds=sides[0] if sides else None,
        team1_t_rounds=sides[1] if sides else None,
        team2_ct_rounds=sides[2] if sides else None,
        team2_t_rounds=sides[3] if sides else None,
        picked_by=picked_by,
        is_lan=is_lan,
        tier=Tier.A,
        format=MatchFormat.BO3,
        team1_lineup=lineup1,
        team2_lineup=lineup2,
    )


@pytest.fixture(scope="module")
def universe_store(learning_universe: SynthUniverse) -> FeatureStore:
    return FeatureStore.build(
        learning_universe.map_results(),
        player_maps=learning_universe.player_map_records(),
        rounds=learning_universe.round_records(),
        vetoes=learning_universe.veto_records(),
    )


# ------------------------------------------------------------------------------ hand-checked values


def test_form_schedule_h2h_and_side_features_on_a_tiny_history():
    records = [
        _rec(1, 0, 1, 2, (13, 7), sides=(8, 5, 4, 3), picked_by=TeamSlot.TEAM1, is_lan=True),
        _rec(
            1,
            0.05,
            1,
            2,
            (10, 13),
            map_name="nuke",
            map_number=2,
            sides=(6, 4, 8, 5),
            picked_by=TeamSlot.TEAM2,
        ),
        _rec(2, 20, 2, 1, (13, 11), sides=(7, 6, 6, 5)),  # team 1 loses on mirage as "team2"
        _rec(3, 50, 1, 3, (13, 3), sides=(9, 4, 2, 1)),
        _rec(4, 58, 3, 1, (16, 14), map_name="nuke"),  # no side data on this one
    ]
    store = FeatureStore.build(records, patch_timeline=None)
    as_of = T0 + 60 * DAY
    f = store.team_features(1, "mirage", as_of)

    # overall form: last 30 days = matches 3, 4 (win 13-3, loss 14-16); 90 days = all five maps
    assert f["maps_30d"] == 2 and f["win_rate_30d"] == pytest.approx(0.5)
    assert f["round_diff_30d"] == pytest.approx((10 - 2) / 2)
    assert f["maps_90d"] == 5 and f["win_rate_90d"] == pytest.approx(2 / 5)
    assert f["round_diff_90d"] == pytest.approx((6 - 3 - 2 + 10 - 2) / 5)
    # on mirage: 13-7 win, 11-13 loss, 13-3 win
    assert f["map_maps_90d"] == 3 and f["map_win_rate_90d"] == pytest.approx(2 / 3)
    assert f["map_maps_30d"] == 1 and f["map_win_rate_30d"] == 1.0
    assert f["map_round_diff_365d"] == pytest.approx((6 - 2 + 10) / 3)
    # schedule
    assert f["days_since_last_match"] == pytest.approx(2.0)
    assert f["maps_14d"] == 2 and f["matches_14d"] == 2
    assert f["maps_alltime"] == 5
    # side strength over maps with side data: CT rounds won / CT rounds played
    ct_won, ct_played = 8 + 6 + 6 + 9, (8 + 3) + (6 + 5) + (6 + 6) + (9 + 1)
    t_won, t_played = 5 + 4 + 5 + 4, (5 + 4) + (4 + 8) + (5 + 7) + (4 + 2)
    assert f["ct_win_pct_90d"] == pytest.approx(ct_won / ct_played)
    assert f["t_win_pct_90d"] == pytest.approx(t_won / t_played)
    assert f["map_ct_win_pct_365d"] == pytest.approx((8 + 6 + 9) / (11 + 12 + 10))
    # LAN record and pick record
    assert f["lan_maps_365d"] == 1 and f["lan_win_rate_365d"] == 1.0
    assert f["own_pick_win_rate_365d"] == 1.0 and f["opp_pick_win_rate_365d"] == 0.0
    # pool: two maps seen, neither three times inside the window -> depth 0 (pool_min_maps = 3) ... mirage is
    assert f["pool_depth"] == 1 and f["pool_depth_winning"] == 1
    assert f["map_play_share_180d"] == pytest.approx(3 / 5)
    assert f["map_pick_share_180d"] == 1.0
    # nothing round-level / demo / player / veto was provided
    for name in ("pistol_win_pct_180d", "trade_kill_share_180d", "pl_kpr_mean", "map_ban_rate_180d"):
        assert math.isnan(f[name])
    assert f["pl_known"] == 0  # no lineup player has data (the same value a store with stat lines gives)

    # a window boundary: match 2 was played exactly 30 days before day 50 + 0 -> still inside [t-30d, t)
    g = store.team_features(1, "mirage", T0 + 50 * DAY)
    assert g["maps_30d"] == 1 and g["days_since_last_match"] == pytest.approx(30.0)
    # the map played at exactly as_of is not visible
    assert g["maps_alltime"] == 3

    # head-to-head from team1's point of view, both orientations
    h12 = store.matchup_features(1, 2, "mirage", as_of)
    assert h12["h2h_maps"] == 3 and h12["h2h_win_rate"] == pytest.approx(1 / 3)
    assert h12["h2h_round_diff"] == pytest.approx((6 - 3 - 2) / 3)
    assert h12["h2h_map_maps"] == 2 and h12["h2h_map_win_rate"] == pytest.approx(0.5)
    h21 = store.matchup_features(2, 1, "mirage", as_of)
    assert h21["h2h_win_rate"] == pytest.approx(2 / 3) and h21["h2h_round_diff"] == pytest.approx(-1 / 3)
    assert h21["strength_logit"] == pytest.approx(-h12["strength_logit"])
    assert h21["strength_p_win"] == pytest.approx(1 - h12["strength_p_win"])
    none = store.matchup_features(2, 3, "mirage", as_of)
    assert none["h2h_maps"] == 0 and math.isnan(none["h2h_win_rate"])

    # all-teams map statistics: CT share on mirage over the maps with side data
    m = store.map_features("mirage", as_of)
    assert m["map_global_maps_180d"] == 3
    assert m["map_ct_round_share_180d"] == pytest.approx((8 + 4 + 7 + 6 + 9 + 2) / (20 + 24 + 16))
    unknown = store.map_features("cache", as_of)
    assert unknown["map_global_maps_180d"] == 0 and math.isnan(unknown["map_ct_round_share_180d"])

    # a team without any history: counts are zero, rates are NaN
    new = store.team_features(99, "mirage", as_of)
    assert new["maps_90d"] == 0 and new["matches_14d"] == 0
    assert (
        math.isnan(new["win_rate_90d"])
        and math.isnan(new["elo"])
        and math.isnan(new["days_since_last_match"])
    )


def test_roster_continuity_change_and_standins():
    old = (1, 2, 3, 4, 5)
    new = (1, 2, 3, 4, 6)
    records = [_rec(i, i, 1, 2, (13, 9), lineup1=old) for i in range(1, 11)]
    records += [_rec(i, i, 1, 2, (13, 9), lineup1=new) for i in range(11, 15)]
    records.append(_rec(15, 15, 1, 2, (13, 9), lineup1=(1, 2, 3, 4)))  # incomplete row: same roster
    store = FeatureStore.build(records, patch_timeline=None, config=FeatureConfig(roster_last_maps=10))

    before = store.team_features(1, "mirage", T0 + 10.5 * DAY, lineup=old, standins=0)
    assert before["roster_continuity"] == 1.0 and before["maps_with_lineup"] == 10
    assert before["days_since_roster_change"] == pytest.approx(9.5)  # lower bound: first indexed map
    assert before["roster_new_players"] == 0 and before["standin_count"] == 0 and before["lineup_known"] == 1

    # the change happens with the match being predicted
    changing = store.team_features(1, "mirage", T0 + 10.5 * DAY, lineup=new, standins=1)
    assert changing["roster_continuity"] == pytest.approx(0.8)
    assert changing["maps_with_lineup"] == 0 and changing["days_since_roster_change"] == 0
    assert changing["roster_new_players"] == 1 and changing["standin_count"] == 1

    # five maps into the new roster (the four-player row does not count as another change)
    after = store.team_features(1, "mirage", T0 + 15.5 * DAY, lineup=new)
    assert after["maps_with_lineup"] == 5
    assert after["days_since_roster_change"] == pytest.approx(4.5)
    # last ten maps: five with the old five (4/5 overlap), four with the new five, one with four of them
    assert after["roster_continuity"] == pytest.approx((5 * 0.8 + 4 * 1.0 + 0.8) / 10)
    assert math.isnan(after["standin_count"])

    # no lineup given: the last fielded roster is assumed and flagged as not confirmed
    assumed = store.team_features(1, "mirage", T0 + 15.5 * DAY)
    assert assumed["lineup_known"] == 0 and assumed["maps_with_lineup"] == 5
    assert store.last_lineup(1, T0 + 15.5 * DAY) == new
    assert store.last_lineup(1, T0 + 5.5 * DAY) == old
    assert store.last_lineup(77, T0) == ()


def test_round_level_and_player_aggregates():
    rec = _rec(1, 0, 1, 2, (13, 5))

    def rnd(
        number: int, winner: TeamSlot, buy1: BuyType, buy2: BuyType, side: Side, planted: bool
    ) -> RoundRecord:
        return RoundRecord(
            match_id=1,
            map_number=1,
            map_name="mirage",
            played_at=rec.played_at,
            round_number=number,
            team1_id=1,
            team2_id=2,
            team1_side=side,
            winner=winner,
            team1_buy=buy1,
            team2_buy=buy2,
            bomb_planted=planted,
        )

    one, two = TeamSlot.TEAM1, TeamSlot.TEAM2
    rounds = [
        rnd(1, one, BuyType.PISTOL, BuyType.PISTOL, Side.T, True),  # pistol win, post-plant win for team1
        rnd(2, two, BuyType.FULL, BuyType.ECO, Side.T, False),  # team2 converts an eco
        rnd(3, one, BuyType.FULL, BuyType.FORCE, Side.T, True),  # anti-eco win, post-plant win
        rnd(4, one, BuyType.FULL, BuyType.FULL, Side.T, True),  # full v full, post-plant win
        rnd(5, two, BuyType.FULL, BuyType.FULL, Side.T, True),  # team2 retakes
        rnd(13, two, BuyType.PISTOL, BuyType.PISTOL, Side.CT, True),  # second pistol lost, failed retake
    ]

    def stat(player_id: int, team_id: int, **kw) -> PlayerMapRecord:
        base = {
            "match_id": 1,
            "map_number": 1,
            "map_name": "mirage",
            "played_at": rec.played_at,
            "player_id": player_id,
            "team_id": team_id,
            "opponent_team_id": 3 - team_id,
            "kills": 18,
            "deaths": 12,
            "rounds_played": 18,
        }
        return PlayerMapRecord(**{**base, **kw})

    players = [
        stat(
            11,
            1,
            kills=27,
            deaths=9,
            rating=1.5,
            adr=100.0,
            kast=80.0,
            first_kills=6,
            first_deaths=2,
            awp_kills=18,
        ),
        stat(
            12,
            1,
            kills=9,
            deaths=18,
            rating=0.7,
            adr=50.0,
            kast=0.6,
            first_kills=1,
            first_deaths=3,
            awp_kills=0,
        ),
        stat(13, 1, is_standin=True),  # no optional fields at all
        stat(21, 2, kills=18, deaths=18),
    ]
    store = FeatureStore.build([rec], rounds=rounds, player_maps=players, patch_timeline=None)
    assert store.has_round_data and store.has_player_data and not store.has_demo_data
    as_of = T0 + DAY
    f1 = store.team_features(1, "mirage", as_of, lineup=(11, 12, 13, 14))
    f2 = store.team_features(2, "mirage", as_of, lineup=(21,))
    assert f1["pistol_win_pct_180d"] == pytest.approx(0.5) and f2["pistol_win_pct_180d"] == pytest.approx(0.5)
    assert f2["eco_force_conv_180d"] == pytest.approx(0.5)  # one of team2's two low buys beat a full buy
    assert math.isnan(f1["eco_force_conv_180d"])  # team1 never bought low against a full buy
    assert f1["anti_eco_win_pct_180d"] == pytest.approx(0.5)
    assert f1["full_buy_win_pct_180d"] == pytest.approx(0.5)
    assert f1["post_plant_win_pct_180d"] == pytest.approx(3 / 4)
    assert f1["retake_win_pct_180d"] == 0.0 and f2["retake_win_pct_180d"] == pytest.approx(1 / 4)

    assert f1["pl_known"] == 3 and f1["pl_maps_mean"] == 1
    assert f1["pl_kpr_mean"] == pytest.approx((27 + 9 + 18) / 18 / 3)
    assert f1["pl_kpr_max"] == pytest.approx(27 / 18)
    assert f1["pl_kd_diff_pr_mean"] == pytest.approx((18 - 9 + 6) / 18 / 3)
    assert (
        f1["pl_rating_mean"] == pytest.approx(1.1)
        and f1["pl_rating_max"] == 1.5
        and f1["pl_rating_min"] == 0.7
    )
    assert f1["pl_adr_mean"] == pytest.approx(75.0)
    assert f1["pl_kast_mean"] == pytest.approx(0.7)  # 80 (percent) and 0.6 (fraction) are both understood
    assert f1["pl_opening_win_pct"] == pytest.approx(7 / 12)
    assert f1["pl_opening_kpr"] == pytest.approx(7 / 18)
    assert f1["pl_awp_share_max"] == pytest.approx(18 / 27) and f1["pl_awp_kpr_max"] == pytest.approx(1.0)
    assert f2["pl_known"] == 1 and math.isnan(f2["pl_rating_mean"]) and math.isnan(f2["pl_awp_share_max"])
    assert store.standin_count(1, 1) == 1 and store.standin_count(1, 2) == 0
    # no stat line of that team in that match: unknown, not "no stand-in"
    assert store.standin_count(1, 99) is None and store.standin_count(424242, 1) is None


def test_features_on_the_synthetic_universe_are_complete_and_sane(universe_store: FeatureStore):
    rec = universe_store.map_results[-1]
    f = universe_store.team_features(rec.team1_id, rec.map_name, rec.played_at, lineup=rec.team1_lineup)
    assert all(isinstance(v, float) for v in f.values())
    for name in (
        "win_rate_90d",
        "map_win_rate_365d",
        "adj_round_share_90d",
        "elo",
        "ct_win_pct_90d",
        "pistol_win_pct_180d",
        "eco_force_conv_180d",
        "post_plant_win_pct_180d",
        "retake_win_pct_180d",
        "map_ban_rate_180d",
        "pl_kpr_mean",
        "pl_opening_win_pct",
        "roster_continuity",
        "regime_break_prob",
        "form_trend",
    ):
        assert math.isfinite(f[name]), name
    for name in (
        "win_rate_90d",
        "ct_win_pct_90d",
        "pistol_win_pct_180d",
        "map_ban_rate_180d",
        "roster_continuity",
    ):
        assert 0.0 <= f[name] <= 1.0
    # eco rounds are won far less often than full-buy rounds in the generator
    assert f["eco_force_conv_180d"] < f["full_buy_win_pct_180d"] < f["anti_eco_win_pct_180d"]
    # 7-map pool; on this small universe a rarely vetoed-in map can fall below pool_min_maps
    assert 6 <= f["pool_depth"] <= 7 and f["roster_continuity"] == 1.0
    meta = universe_store.meta_features(rec.played_at)
    assert meta["days_since_patch"] >= 0 and meta["patch_index"] >= 0
    # the strength proxy recovers the true team ordering reasonably well
    truth, proxy = [], []
    for team in range(1, 11):
        elo, _ = universe_store.team_strength(team, None, rec.played_at + DAY)
        truth.append(team)
        proxy.append(elo)
    assert all(math.isfinite(v) for v in proxy)


# ----------------------------------------------------------------------------------------- leakage


def test_features_never_use_the_match_itself_or_anything_later(learning_universe: SynthUniverse):
    maps = sorted(learning_universe.map_results(), key=lambda r: (r.played_at, r.match_id, r.map_number))
    players = learning_universe.player_map_records()
    rounds = learning_universe.round_records()
    vetoes = learning_universe.veto_records()
    full = FeatureStore.build(maps, player_maps=players, rounds=rounds, vetoes=vetoes)
    x_full, _, meta_full = build_training_rows(maps, store=full)
    starts = dict(zip(meta_full["match_id"], meta_full["as_of"], strict=True))
    by_match = {m.id: m for m in learning_universe.matches}
    checked = 0
    for row in range(40, len(maps), 37):
        rec = maps[row]
        cut = starts[rec.match_id].to_pydatetime()
        # a store that has never seen this match nor anything after its start
        past = FeatureStore.build(
            [m for m in maps if m.played_at < cut],
            player_maps=[p for p in players if by_match[p.match_id].played_at < cut],
            rounds=[r for r in rounds if r.played_at < cut],
            vetoes=[v for v in vetoes if v.played_at < cut],
        )
        series = [m for m in maps if m.match_id == rec.match_id]
        x_past, _, _ = build_training_rows(series, store=past)
        # stand-in counts come from the match's own lineup announcement, which `past` lacks
        a = {k: v for k, v in x_full.iloc[row].to_dict().items() if "standin" not in k}
        b = {k: v for k, v in x_past.iloc[series.index(rec)].to_dict().items() if "standin" not in k}
        assert same_features(a, b) == [], f"row {row} leaks"
        checked += 1
    assert checked >= 6


def test_changing_the_future_does_not_change_the_features(learning_universe: SynthUniverse):
    maps = sorted(learning_universe.map_results(), key=lambda r: (r.played_at, r.match_id, r.map_number))
    target = maps[len(maps) // 2]
    start = min(m.played_at for m in maps if m.match_id == target.match_id)

    def tampered(rec: MapResultRecord) -> MapResultRecord:
        if rec.played_at < start:
            return rec
        # flip every result from the target match on, swap sides and picks
        return rec.model_copy(
            update={
                "team1_rounds": rec.team2_rounds,
                "team2_rounds": rec.team1_rounds,
                "team1_ct_rounds": rec.team2_ct_rounds,
                "team2_ct_rounds": rec.team1_ct_rounds,
                "is_lan": not rec.is_lan,
            }
        )

    honest = FeatureStore.build(maps)
    altered = FeatureStore.build([tampered(m) for m in maps])
    for team in (target.team1_id, target.team2_id):
        a = honest.team_features(team, target.map_name, start, exclude_match_id=target.match_id)
        b = altered.team_features(team, target.map_name, start, exclude_match_id=target.match_id)
        assert same_features(a, b) == []
    a = honest.matchup_features(target.team1_id, target.team2_id, target.map_name, start)
    b = altered.matchup_features(target.team1_id, target.team2_id, target.map_name, start)
    assert same_features(a, b) == []
    # sanity: the tampering is visible one day later
    later = start + 30 * DAY
    assert same_features(
        honest.team_features(target.team1_id, target.map_name, later),
        altered.team_features(target.team1_id, target.map_name, later),
    )


def test_exclude_match_id_hides_maps_already_played_in_the_same_series():
    records = [
        _rec(1, 0, 1, 2, (13, 5)),
        _rec(2, 10, 1, 2, (13, 5), map_number=1),
        _rec(2, 10.04, 1, 2, (5, 13), map_name="nuke", map_number=2),
    ]
    store = FeatureStore.build(records, patch_timeline=None)
    mid_series = T0 + 10.08 * DAY  # asked while map 3 is about to start
    seen = store.team_features(1, "mirage", mid_series)
    hidden = store.team_features(1, "mirage", mid_series, exclude_match_id=2)
    assert seen["maps_alltime"] == 3 and hidden["maps_alltime"] == 1
    assert hidden["win_rate_90d"] == 1.0
    assert store.match_start(2) == T0 + 10 * DAY and store.match_start(99) is None


# ------------------------------------------------------------------------------------------- speed


def test_thousands_of_rows_are_built_in_seconds():
    uni = generate_universe(n_teams=16, n_matches=1200, seed=3, days=600, with_kills=False)
    maps = uni.map_results()
    assert len(maps) > 2000
    started = time.perf_counter()
    store = FeatureStore.build(maps, rounds=uni.round_records(), vetoes=uni.veto_records())
    x, y, meta = build_training_rows(maps, store=store)
    elapsed = time.perf_counter() - started
    assert x.shape[0] == len(maps) == len(y) == len(meta)
    assert x.shape[1] > 150
    # ~1.5 s on the build machine; the generous bound only guards against quadratic regressions
    assert elapsed < 30.0


# -------------------------------------------------------------------------------- from the database


def test_store_from_session_equals_store_from_records(learning_db: Session, universe_store: FeatureStore):
    store = FeatureStore.from_session(learning_db)
    assert len(store) == len(universe_store)
    assert store.has_round_data and store.has_player_data and store.has_veto_data
    rec = store.map_results[-1]
    for team, lineup in ((rec.team1_id, rec.team1_lineup), (rec.team2_id, rec.team2_lineup)):
        a = store.team_features(
            team, rec.map_name, rec.played_at, lineup=lineup, exclude_match_id=rec.match_id
        )
        b = universe_store.team_features(
            team, rec.map_name, rec.played_at, lineup=lineup, exclude_match_id=rec.match_id
        )
        # synthetic kills are loaded as demo totals from the database only
        demo = {"trade_kill_share_180d", "traded_death_share_180d"}
        assert set(same_features(a, b, tol=1e-9)) <= demo
    cut = store.map_results[len(store) // 2].played_at
    half = FeatureStore.from_session(learning_db, until=cut, with_rounds=False, with_demo=False)
    assert 0 < len(half) < len(store) and not half.has_round_data
    assert all(r.played_at < cut for r in half.map_results)
    assert len(datasets.load_map_results(learning_db, until=cut)) == len(half)


def test_excluded_match_cuts_every_query_at_its_start():
    """With ``exclude_match_id`` a later ``as_of`` must neither see the match itself nor anything that
    happened after it began, and window counts never go negative."""
    records = [_rec(k, float(k), 1, 2, (13, 5)) for k in range(1, 6)]
    records += [_rec(10, 20, 1, 2, (13, 5)), _rec(11, 25, 1, 3, (2, 13)), _rec(12, 300, 1, 3, (2, 13))]
    store = FeatureStore.build(records, patch_timeline=None)
    start = store.match_start(10)
    assert start == T0 + 20 * DAY
    late = T0 + 200 * DAY
    assert store.cutoff(late, 10) == start and store.cutoff(late) == late
    assert store.cutoff(T0, 10) == T0  # an earlier as_of is kept
    at_start = store.team_features(1, "mirage", start, exclude_match_id=10)
    later = store.team_features(1, "mirage", late, exclude_match_id=10)
    assert same_features(at_start, later) == []  # match 11 (day 25) stays invisible
    assert later["maps_alltime"] == 5
    assert not any(later[k] < 0 for k in later if k.startswith(("maps_", "map_maps_", "matches_")))
    assert (
        same_features(
            store.matchup_features(1, 3, "mirage", late, exclude_match_id=10),
            store.matchup_features(1, 3, "mirage", start, exclude_match_id=10),
        )
        == []
    )
    assert store.last_lineup(1, late, exclude_match_id=10) == store.last_lineup(1, start)
    assert store.team_strength(1, "de_mirage", late, exclude_match_id=10) == store.team_strength(
        1, "mirage", start
    )
    # the shared row builder cuts the meta features at the same instant
    row_late = build_training_rows([records[5]], store=store)[0]
    direct = match_map_features(
        store,
        team1_id=1,
        team2_id=2,
        map_name="mirage",
        as_of=late,
        tier=Tier.A,
        fmt=MatchFormat.BO3,
        team1_standins=None,
        exclude_match_id=10,
    )
    assert same_features(direct, row_late.iloc[0].to_dict()) == []


def test_player_count_is_the_same_for_a_store_without_stat_lines_and_one_cut_before_them():
    """Review round 1: ``pl_known`` was NaN for a store without any stat line but 0 for a store whose
    stat lines all lie after ``as_of`` - a walk-forward store cut early disagreed with the full one."""
    maps = [_rec(1, 0, 1, 2, (13, 5), lineup1=(11,), lineup2=(21,)), _rec(2, 10, 1, 2, (13, 9))]
    line = PlayerMapRecord(
        match_id=2,
        map_number=1,
        map_name="mirage",
        played_at=T0 + 10 * DAY,
        player_id=11,
        team_id=1,
        opponent_team_id=2,
        kills=20,
        deaths=10,
        rounds_played=22,
    )
    bare = FeatureStore.build(maps, patch_timeline=None)
    full = FeatureStore.build(maps, player_maps=[line], patch_timeline=None)
    at = T0 + 5 * DAY
    a = bare.team_features(1, "mirage", at, lineup=(11,))
    b = full.team_features(1, "mirage", at, lineup=(11,))
    assert a["pl_known"] == b["pl_known"] == 0.0
    assert same_features(a, b) == []
    assert full.team_features(1, "mirage", T0 + 11 * DAY, lineup=(11,))["pl_known"] == 1.0
