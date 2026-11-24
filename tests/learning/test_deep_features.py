"""deep_data: the deep feature block (schedule, travel, stakes, format, economy, duels, market).

The central guarantee is tested on a synthetic universe with kills and market history: a training row
built from the full history equals the row built from a store that only ever saw data from before the
match started (no look-ahead), and the live path (``features_for_match_map``) equals the training path.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy.orm import Session

from cs2bot.backtest.deep_eval import synthetic_deep_data
from cs2bot.backtest.synthetic import synthetic_backtest_matches
from cs2bot.datasets.deep import (
    ClutchRecord,
    CoachChangeRecord,
    DeepData,
    DuelRecord,
    MarketPoint,
    MatchMetaRecord,
    TeamMetaRecord,
)
from cs2bot.domain.enums import MatchFormat, MatchStatus, Side, TeamSlot
from cs2bot.domain.records import MapResultRecord
from cs2bot.features import FeatureStore, build_training_rows, features_for_match_map
from cs2bot.features.deep import DEEP_MATCH_FEATURES, DEEP_TEAM_FEATURES, DeepConfig, DeepFeatureIndex
from cs2bot.features.geo import normalize_country, parse_location, team_home, tz_shift_hours
from cs2bot.features.meta import load_patch_timeline
from cs2bot.features.rows import match_map_features
from cs2bot.features.stakes import classify_stage, swiss_situation
from cs2bot.features.store import FeatureConfig
from cs2bot.testing.synthetic import SynthUniverse, generate_universe

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def rec(
    match_id: int,
    t: datetime,
    t1: int,
    t2: int,
    *,
    score: tuple[int, int] = (13, 7),
    map_number: int = 1,
    map_name: str = "mirage",
    fmt: MatchFormat = MatchFormat.BO3,
    picked_by: TeamSlot | None = None,
) -> MapResultRecord:
    return MapResultRecord(
        match_id=match_id,
        map_number=map_number,
        map_name=map_name,
        played_at=t,
        team1_id=t1,
        team2_id=t2,
        team1_rounds=score[0],
        team2_rounds=score[1],
        picked_by=picked_by,
        format=fmt,
    )


# ------------------------------------------------------------------------------------- stage / geo


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Grand final", {"grand_final", "final", "playoff"}),
        ("Lower bracket final", {"final", "lower_bracket", "elimination", "playoff"}),
        ("Upper bracket semi-final", {"semi", "upper_bracket", "playoff"}),
        ("Quarter-final", {"quarter", "playoff"}),
        ("Group A decider match", {"group", "decider"}),
        ("Group B elimination match", {"group", "elimination"}),
        ("Last Chance Qualifier", {"qualification"}),
        ("Swiss round 5", {"swiss"}),
        ("Opening match", {"opening"}),
        ("Showmatch", {"low_stakes"}),
        ("3rd place decider", {"low_stakes", "decider"}),
        (None, set()),
    ],
)
def test_classify_stage(text: str | None, expected: set[str]) -> None:
    info = classify_stage(text)
    flags = {k.removeprefix("stage_") for k, v in info.features().items() if k.startswith("stage_") and v}
    assert flags == expected


def test_stakes_level_and_swiss() -> None:
    assert classify_stage("Grand final").stakes_level == 5.0
    assert classify_stage("Showmatch").stakes_level == 0.0
    assert classify_stage("Lower bracket round 1").elimination
    assert swiss_situation(2, 2) == (True, True)
    assert swiss_situation(0, 2) == (True, False) and swiss_situation(2, 0) == (False, True)
    assert swiss_situation(1, 1) == (False, False)


def test_geo_lookups() -> None:
    assert normalize_country("DK") == "denmark" and normalize_country("Czechia") == "czech republic"
    assert normalize_country("nowhere") is None and normalize_country(None) is None
    cologne = parse_location("Cologne, Germany")
    assert (
        cologne is not None
        and cologne.country == "germany"
        and cologne.utc_offset == 1
        and not cologne.online
    )
    austin = parse_location("Austin, TX, USA")
    assert austin is not None and austin.country == "united states" and austin.region == "north_america"
    assert parse_location("Katowice") is not None and parse_location("Katowice").country == "poland"  # type: ignore[union-attr]
    online = parse_location("Europe (Online)")
    assert online is not None and online.online and online.region == "europe"
    assert parse_location("Online") is not None and parse_location("") is None
    home = team_home(None, ("Brazil", "Brazil", "Portugal"))
    assert home is not None and home.country == "brazil"
    assert team_home("CIS") is not None and team_home(None) is None
    assert tz_shift_hours(10, -6) == 8 and tz_shift_hours(1, 1) == 0 and tz_shift_hours(None, 1) is None


# ------------------------------------------------------------------------------- handcrafted blocks


def test_schedule_and_format_features() -> None:
    day = timedelta(days=1)
    records = [
        rec(1, T0 - 3 * day - timedelta(hours=3), 1, 2, fmt=MatchFormat.BO3, picked_by=TeamSlot.TEAM1),
        rec(1, T0 - 3 * day - timedelta(hours=2), 1, 2, map_number=2, fmt=MatchFormat.BO3, score=(5, 13)),
        rec(1, T0 - 3 * day - timedelta(hours=1), 1, 2, map_number=3, fmt=MatchFormat.BO3, map_name="nuke"),
        rec(2, T0 - 1 * day, 3, 1, fmt=MatchFormat.BO1, score=(13, 11)),
        rec(3, T0 - timedelta(hours=4), 1, 4, fmt=MatchFormat.BO1),
        rec(9, T0 + day, 1, 2, fmt=MatchFormat.BO1),  # future: invisible
    ]
    idx = DeepFeatureIndex(records)
    f1, f2, m = idx.features(1, 2, "mirage", T0, match_id=99, exclude_match_id=99)
    assert set(f1) == set(DEEP_TEAM_FEATURES) and set(m) == set(DEEP_MATCH_FEATURES)
    assert f1["dp_maps_3d"] == 2.0  # match 2 and match 3 (match 1 ended just over 3 days ago)
    assert f1["dp_maps_7d"] == 5.0 and f1["dp_matches_7d"] == 3.0 and f1["dp_matches_3d"] == 2.0
    assert f1["dp_rest_days"] == pytest.approx(4 / 24)
    assert f1["dp_same_day_series"] == 1.0 and f1["dp_maps_today"] == 1.0
    assert f1["dp_play_streak_days"] == 1.0  # yesterday yes, two days ago no
    assert f1["dp_bo1_series_win"] == pytest.approx(0.5)  # lost match 2, won match 3
    assert f1["dp_bo3_series_win"] == 1.0  # won match 1 two maps to one
    assert f1["dp_decider_maps"] == 1.0 and f1["dp_decider_win"] == 1.0
    assert f1["dp_pick_edge"] == pytest.approx(1.0 - 3 / 5)
    # team 2 lost match 1 only
    assert f2["dp_bo3_series_win"] == 0.0 and f2["dp_matches_7d"] == 1.0
    assert math.isnan(f1["dp_tz_shift"])  # no metadata
    assert all(v == 0.0 for k, v in m.items() if k.startswith("stage_"))


def _meta(mid: int, t: datetime, t1: int, t2: int, **kw: object) -> MatchMetaRecord:
    return MatchMetaRecord(match_id=mid, scheduled_at=t, team1_id=t1, team2_id=t2, **kw)  # type: ignore[arg-type]


def test_travel_event_and_coach_features() -> None:
    day = timedelta(days=1)
    meta = [
        _meta(1, T0 - 10 * day, 1, 3, is_lan=False, tournament_id=7, location="Europe (Online)"),
        _meta(
            2,
            T0 - 2 * day,
            1,
            4,
            is_lan=True,
            tournament_id=8,
            location="Cologne, Germany",
            stage="Swiss round 3",
        ),
        _meta(
            3,
            T0 - 1 * day,
            1,
            5,
            is_lan=True,
            tournament_id=8,
            location="Cologne, Germany",
            stage="Swiss round 4",
        ),
        _meta(10, T0, 1, 2, is_lan=True, tournament_id=8, location="Cologne, Germany", stage="Swiss round 5"),
    ]
    records = [
        rec(1, T0 - 10 * day, 1, 3),
        rec(2, T0 - 2 * day, 1, 4, score=(10, 13), fmt=MatchFormat.BO1),
        rec(3, T0 - 1 * day, 1, 5, fmt=MatchFormat.BO1),
    ]
    deep = DeepData(
        match_meta=meta,
        team_meta={1: TeamMetaRecord(1, "Denmark"), 2: TeamMetaRecord(2, None, ("Brazil", "Brazil"))},
        coach_changes=[CoachChangeRecord(2, date(2026, 2, 1)), CoachChangeRecord(2, date(2026, 3, 1))],
    )
    idx = DeepFeatureIndex(records, deep=deep)
    f1, f2, m = idx.features(1, 2, "mirage", T0, match_id=10, exclude_match_id=10)
    assert m["stage_swiss"] == 1.0
    assert f1["dp_tz_shift"] == 0.0 and f1["dp_away_region"] == 0.0
    assert f2["dp_tz_shift"] == 4.0 and f2["dp_away_region"] == 1.0
    assert f1["dp_first_after_travel"] == 0.0 and f1["dp_days_at_event"] == pytest.approx(2.0)
    assert f2["dp_first_after_travel"] == 1.0 and f2["dp_days_at_event"] == 0.0
    assert (f1["dp_event_wins"], f1["dp_event_losses"]) == (1.0, 1.0)
    assert f1["dp_swiss_elimination"] == 0.0 and f2["dp_event_wins"] == 0.0
    # the change on the match day itself is not visible yet (date-only data)
    assert f2["dp_days_since_coach_change"] == pytest.approx(28.5) and f2["dp_coach_change_90d"] == 1.0
    assert math.isnan(f1["dp_days_since_coach_change"])
    # mirrored query: travel follows the team, not the slot
    g1, g2, _ = idx.features(2, 1, "mirage", T0, match_id=10, exclude_match_id=10)
    assert g1["dp_tz_shift"] == 4.0 and g2["dp_tz_shift"] == 0.0
    # online event: no jet lag for anyone
    online = DeepData(
        match_meta=[_meta(10, T0, 1, 2, is_lan=False, location="Online")], team_meta=deep.team_meta
    )
    h1, h2, _ = DeepFeatureIndex(records, deep=online).features(
        1, 2, "mirage", T0, match_id=10, exclude_match_id=10
    )
    assert h1["dp_tz_shift"] == h2["dp_tz_shift"] == 0.0 and h2["dp_first_after_travel"] == 0.0


def test_duel_clutch_and_matchup_features() -> None:
    day = timedelta(days=1)
    records = [rec(1, T0 - 5 * day, 1, 2), rec(2, T0 - 4 * day, 1, 2, map_name="nuke")]
    duels: list[DuelRecord] = []
    # player 11 (team 1 AWPer) vs 21 (team 2 AWPer): 30 AWP duels, 11 wins 20 of them
    for i in range(30):
        won = i < 20
        a, v = (11, 21) if won else (21, 11)
        duels.append(
            DuelRecord(
                1, 1, "mirage", T0 - 5 * day, a, v, 1 if won else 2, 2 if won else 1, Side.CT, "awp", "awp"
            )
        )
    # openings: 12 (team 1 entry) wins 8 of 10 T-side opening duels against 22 in "a site"
    for i in range(10):
        won = i < 8
        a, v = (12, 22) if won else (22, 12)
        side = Side.T if won else Side.CT
        duels.append(
            DuelRecord(
                1,
                1,
                "mirage",
                T0 - 5 * day,
                a,
                v,
                None,
                None,
                side,
                "ak47",
                None,
                True,
                False,
                i % 2 == 0,
                "A Site",
            )
        )
    clutches = [
        ClutchRecord(1, 1, "mirage", T0 - 5 * day, 1, 11, 1, True),
        ClutchRecord(1, 1, "mirage", T0 - 5 * day, 1, 12, 3, False),
        ClutchRecord(2, 1, "nuke", T0 + day, 1, 12, 1, True),  # future
    ]
    idx = DeepFeatureIndex(
        records,
        deep=DeepData(duels=duels, clutches=clutches),
        config=DeepConfig(min_awp_kills=10),
        patch_timeline=load_patch_timeline(),
    )
    f1, f2, m = idx.features(
        1, 2, "mirage", T0, match_id=3, exclude_match_id=3, team1_lineup=(11, 12), team2_lineup=(21, 22)
    )
    assert f1["dp_clutch_win"] == 0.5 and f1["dp_clutch_1v1_win"] == 1.0 and f1["dp_clutch_1vx_win"] == 0.0
    assert f1["dp_awper_kill_share"] == 1.0 and f1["dp_awper_awp_duel_win"] == pytest.approx(20 / 30)
    # player 12 took all ten opening duels on the T side and won eight; player 22 defended them as CT
    assert f1["dp_open_t_win"] == pytest.approx(0.8) and math.isnan(f1["dp_open_ct_win"])
    assert f1["dp_entry_open_t_win"] == pytest.approx(0.8)
    assert f2["dp_open_ct_win"] == pytest.approx(0.2) and math.isnan(f2["dp_open_t_win"])
    assert m["dp_awp_h2h_n"] == 30 and m["dp_awp_h2h_share"] == pytest.approx(20 / 30)
    assert m["dp_lineup_h2h_n"] == 40 and m["dp_lineup_h2h_share"] == pytest.approx(28 / 40)
    # team1 attacking "a site": T 0.8 vs team2's CT 0.2 -> 0.6; no data the other way round -> 0.6 / 2
    assert m["dp_area_open_edge"] == pytest.approx(0.3)
    assert m["dp_map_days_in_pool"] > 0  # mirage has been Active Duty since the CS2 release
    # another map: no lineup duels there
    _, _, nuke = idx.features(
        1, 2, "nuke", T0, match_id=3, exclude_match_id=3, team1_lineup=(11, 12), team2_lineup=(21, 22)
    )
    assert nuke["dp_lineup_h2h_n"] == 0 and math.isnan(nuke["dp_lineup_h2h_share"])
    # strictly before as_of: nothing seen at the time of the duels
    e1, _, em = idx.features(1, 2, "mirage", T0 - 5 * day, team1_lineup=(11,), team2_lineup=(21,))
    assert em["dp_awp_h2h_n"] == 0 and math.isnan(e1["dp_open_t_win"])


def test_market_block_follows_orientation() -> None:
    meta = [_meta(5, T0, 1, 2)]
    market = {
        5: [MarketPoint(T0 - timedelta(hours=5), 1.5, 2.6), MarketPoint(T0 - timedelta(hours=1), 1.4, 2.9)]
    }
    idx = DeepFeatureIndex(
        [rec(1, T0 - timedelta(days=3), 1, 2)], deep=DeepData(match_meta=meta, market=market)
    )
    _, _, m = idx.features(1, 2, "mirage", T0, match_id=5, exclude_match_id=5)
    _, _, flipped = idx.features(2, 1, "mirage", T0, match_id=5, exclude_match_id=5)
    assert m["mkt_p_last"] > 0.6 and flipped["mkt_p_last"] == pytest.approx(1 - m["mkt_p_last"])
    _, _, early = idx.features(1, 2, "mirage", T0 - timedelta(hours=2), match_id=5, exclude_match_id=5)
    assert early["mkt_p_last"] == pytest.approx(early["mkt_p_open"]) and early["mkt_n_moves"] == 0
    off = DeepFeatureIndex(
        [], deep=DeepData(match_meta=meta, market=market), config=DeepConfig(with_market=False)
    )
    assert math.isnan(off.features(1, 2, "mirage", T0, match_id=5)[2]["mkt_p_last"])


# --------------------------------------------------------------------- leakage + train/live parity


@pytest.fixture(scope="module")
def deep_universe() -> tuple[SynthUniverse, DeepData]:
    uni = generate_universe(n_teams=8, n_matches=90, seed=5, days=120, with_kills=True)
    data = synthetic_deep_data(uni, seed=5, truth_sims=200)
    # add venues / stages / homes / clutches so every block is exercised
    meta = [
        MatchMetaRecord(
            m.match_id,
            m.scheduled_at,
            m.team1_id,
            m.team2_id,
            MatchStatus.FINISHED,
            m.format,
            m.tier,
            m.is_lan,
            ["Group A decider match", "Grand final", "Swiss round 2", None][m.match_id % 4],
            m.tournament_id,
            m.tournament_name,
            ["Cologne, Germany", "Austin, USA", "Online", "Shanghai, China"][m.match_id % 4],
        )
        for m in data.match_meta
    ]
    rng = random.Random(1)
    clutches = [
        ClutchRecord(
            d.match_id,
            d.map_number,
            d.map_name,
            d.played_at,
            d.attacker_team_id or 1,
            d.attacker_id,
            rng.randint(1, 3),
            rng.random() < 0.4,
        )
        for d in data.duels[::97]
        if d.attacker_team_id is not None
    ]
    teams = {t: TeamMetaRecord(t, ["Denmark", "Brazil", "Russia", "USA"][t % 4]) for t in uni.teams}
    coach = [CoachChangeRecord(3, date(2024, 2, 1))]
    return uni, DeepData(meta, teams, clutches, data.duels, coach, data.market)


def _store(uni: SynthUniverse, deep: DeepData, until: datetime | None = None) -> FeatureStore:
    def cut(items: list) -> list:  # type: ignore[type-arg]
        return items if until is None else [r for r in items if r.played_at < until]

    return FeatureStore.build(
        cut(uni.map_results()),
        rounds=cut(uni.round_records()),
        player_maps=cut(uni.player_map_records()),
        vetoes=cut(uni.veto_records()),
        deep=deep if until is None else deep.cut(until),
    )


def _same(a: float, b: float) -> bool:
    return (math.isnan(a) and math.isnan(b)) or a == pytest.approx(b, rel=1e-9, abs=1e-12)


def test_rows_from_full_history_equal_rows_from_history_cut_at_kickoff(
    deep_universe: tuple[SynthUniverse, DeepData],
) -> None:
    uni, deep = deep_universe
    full = _store(uni, deep)
    assert full.deep is not None and full.deep.has_demo and full.deep.has_market and full.deep.has_meta
    rng = random.Random(7)
    # The synthetic schedule is random, so a team can have two series running at the same time - which
    # never happens in reality. A store cut at kick-off then holds part of the other series (its maps
    # before kick-off) while the full store places that series at its last map, after kick-off. Such
    # matches are left out; everything else must be identical.
    starts: dict[int, list[tuple[datetime, int]]] = {}
    for m in uni.matches:
        for team in (m.team1_id, m.team2_id):
            starts.setdefault(team, []).append((m.played_at, m.id))

    def overlaps(match_id: int, team: int, start: datetime) -> bool:
        return any(
            mid != match_id and start - timedelta(hours=8) < t < start + timedelta(hours=8)
            for t, mid in starts[team]
        )

    by_match = {m.id: m for m in uni.matches}
    records = [
        r
        for r in uni.map_results()
        if r.match_id > 30
        and not overlaps(r.match_id, r.team1_id, by_match[r.match_id].played_at)
        and not overlaps(r.match_id, r.team2_id, by_match[r.match_id].played_at)
    ]
    checked = 0
    deep_cols_with_values: set[str] = set()
    for r in rng.sample(records, 12):
        start = full.match_start(r.match_id)
        assert start is not None
        cut = _store(uni, deep, until=start)
        same_match = [x for x in uni.map_results() if x.match_id == r.match_id]
        x_full, _, meta_full = build_training_rows(same_match, store=full)
        x_cut, _, meta_cut = build_training_rows(same_match, store=cut)
        assert (meta_full["as_of"] == meta_cut["as_of"]).all()
        x_full = x_full[(meta_full["map_number"] == r.map_number).to_numpy()]
        x_cut = x_cut[(meta_cut["map_number"] == r.map_number).to_numpy()]
        assert list(x_full.columns) == list(x_cut.columns)
        for col in x_full.columns:
            if col.endswith("standin_count"):
                # the store reads stand-ins of the match itself from its stat lines (lineups are
                # public before the match); a store cut at kick-off has none -> unknown. Not a leak.
                continue
            a, b = float(x_full[col].iloc[0]), float(x_cut[col].iloc[0])
            assert _same(a, b), (r.match_id, col, a, b)
            if ("dp_" in col or col.startswith(("mkt_", "stage_"))) and not math.isnan(a):
                deep_cols_with_values.add(col)
        checked += 1
    assert checked == 12
    # the comparison is meaningful: duel, market, travel and stage features were actually filled
    for expected in (
        "diff_dp_open_t_win",
        "mkt_p_last",
        "diff_dp_tz_shift",
        "stage_decider",
        "dp_lineup_h2h_share",
    ):
        assert expected in deep_cols_with_values, expected


def test_live_rows_equal_training_rows(deep_universe: tuple[SynthUniverse, DeepData]) -> None:
    uni, deep = deep_universe
    store = _store(uni, deep)
    by_id = {m.match_id: m for m in synthetic_backtest_matches(uni)}
    stage_of = {m.match_id: m.stage for m in deep.match_meta}
    for r in [r for r in uni.map_results() if r.match_id in (50, 61, 77)]:
        ctx = by_id[r.match_id].context
        assert ctx is not None
        ctx = ctx.model_copy(update={"stage": stage_of[r.match_id]})
        live = features_for_match_map(
            ctx, r.map_name, store=store, as_of=r.played_at + timedelta(days=30), picked_by=r.picked_by
        )
        x, _, _ = build_training_rows([r], store=store)
        for col in [c for c in x.columns if "dp_" in c or c.startswith(("mkt_", "stage_", "stakes"))]:
            assert _same(float(x[col].iloc[0]), live[col]), (r.match_id, col)


def test_deep_block_can_be_switched_off_and_team_columns_widened(
    deep_universe: tuple[SynthUniverse, DeepData],
) -> None:
    uni, deep = deep_universe
    recs = uni.map_results()[:40]
    off = FeatureStore.build(recs, config=FeatureConfig(deep_features=False), deep=deep)
    assert off.deep is None
    x_off, _, _ = build_training_rows(recs[-3:], store=off)
    assert not [c for c in x_off.columns if "dp_" in c or c.startswith("mkt_")]
    wide = FeatureStore.build(recs, config=FeatureConfig(deep=DeepConfig(team_columns="all")), deep=deep)
    row = match_map_features(wide, team1_id=1, team2_id=2, map_name="mirage", as_of=T0)
    assert "t1_dp_rest_days" in row and "t2_dp_rest_days" in row and "diff_dp_rest_days" in row
    narrow = match_map_features(
        FeatureStore.build(recs, deep=deep), team1_id=1, team2_id=2, map_name="mirage", as_of=T0
    )
    assert "t1_dp_rest_days" not in narrow and "diff_dp_rest_days" in narrow


def test_from_session_loads_every_deep_part(session: Session) -> None:
    from sqlalchemy import select

    from cs2bot.db.models import (
        BookmakerEvent,
        BookmakerMarket,
        BookmakerSelection,
        Match,
        OddsSnapshot,
        PlayerRoundStats,
        RosterPeriod,
        Team,
        Tournament,
    )
    from cs2bot.domain.enums import Bookmaker, DataSource, LinkStatus
    from cs2bot.testing.synthetic import populate_db

    uni = generate_universe(n_teams=6, n_matches=20, seed=2, days=40, with_kills=True)
    populate_db(session, uni, with_rounds=True, with_kills=True)
    for tour in session.scalars(select(Tournament)):
        tour.location = "Katowice, Poland"
    session.get(Team, 1).country = "Denmark"  # type: ignore[union-attr]
    session.add(
        RosterPeriod(
            team_id=1, player_id=1, started_on=date(2024, 1, 5), is_coach=True, source=DataSource.HLTV
        )
    )
    first_map = session.scalar(select(Match).order_by(Match.id))
    assert first_map is not None
    session.add(
        PlayerRoundStats(
            map_played_id=1,
            round_number=1,
            player_id=1,
            team_id=1,
            side=Side.CT,
            clutch_situation="1v2",
            clutch_won=True,
        )
    )
    last = uni.matches[-1]
    ev = BookmakerEvent(
        bookmaker=Bookmaker.BETCLIC,
        external_id="x",
        name="a-b",
        team1_raw="a",
        team2_raw="b",
        starts_at=last.played_at,
        match_id=last.id,
        link_status=LinkStatus.AUTO,
        first_seen_at=last.played_at - timedelta(days=1),
        last_seen_at=last.played_at,
    )
    session.add(ev)
    session.flush()
    mk = BookmakerMarket(
        event_id=ev.id,
        raw_name="mw",
        spec_key="match_winner",
        market_type="match_winner",
        is_mapped=True,
        first_seen_at=ev.first_seen_at,
        last_seen_at=ev.last_seen_at,
    )
    session.add(mk)
    session.flush()
    for key, odds in (("team1", "1.55"), ("team2", "2.40")):
        sel = BookmakerSelection(market_id=mk.id, raw_name=key, selection_key=key, is_mapped=True)
        session.add(sel)
        session.flush()
        session.add(
            OddsSnapshot(selection_id=sel.id, odds=odds, captured_at=last.played_at - timedelta(hours=2))
        )
    session.flush()

    data = DeepData.from_session(session)
    assert len(data.match_meta) == 20 and data.match_meta[0].location == "Katowice, Poland"
    assert data.team_meta[1].country == "Denmark" and data.duels and data.coach_changes
    assert len(data.clutches) == 1 and data.clutches[0].opponents == 2
    assert last.id in data.market and len(data.market[last.id]) == 2
    store = FeatureStore.from_session(session)
    assert store.deep is not None and store.deep.has_market and store.deep.has_meta
    x, _, _ = build_training_rows(store.map_results[-2:], store=store)
    assert np.isfinite(x["mkt_p_last"].to_numpy()).all()
    assert FeatureStore.from_session(session, with_deep=False).deep is not None  # map-derived block only
    assert not FeatureStore.from_session(session, with_deep=False).deep.has_market  # type: ignore[union-attr]


def test_from_session_attaches_sharp_quotes(session: Session, tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json

    from cs2bot.odds.sharp import JsonFileSharpOddsSource
    from cs2bot.testing.synthetic import populate_db

    uni = generate_universe(n_teams=4, n_matches=6, seed=3, days=10, with_kills=False)
    populate_db(session, uni, with_rounds=False)
    m = uni.matches[-1]
    path = tmp_path / "sharp.json"
    quotes = [
        {
            "match_id": m.id,
            "captured_at": (m.played_at - timedelta(hours=1)).isoformat(),
            "team1_odds": 1.5,
            "team2_odds": 2.7,
        }
    ]
    path.write_text(json.dumps({"quotes": quotes}), encoding="utf-8")
    data = DeepData.from_session(session, sharp=JsonFileSharpOddsSource(path))
    assert list(data.sharp) == [m.id] and 0.6 < data.sharp[m.id][0][1] < 0.7
    store = FeatureStore.build(uni.map_results(), deep=data)
    rows, _, _ = build_training_rows([r for r in uni.map_results() if r.match_id == m.id], store=store)
    assert rows["sharp_p_last"].notna().all()
