"""Role inference from demo-style data (handcrafted kills / positions with weapons and places)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy.orm import Session

from cs2bot import datasets
from cs2bot.datasets import RoleFrames
from cs2bot.db.models import Kill, MapPlayed, Match, Player, PlayerPositionSample, PlayerRoundStats, Team
from cs2bot.domain.enums import MatchStatus, PlayerRole, Side
from cs2bot.features import RoleConfig, decide_role, infer_roles, role_metrics, update_player_roles
from cs2bot.features.roles import ROLE_INFERRED_AT_KEY
from cs2bot.testing.synthetic import SynthUniverse

T0 = datetime(2025, 5, 1, 18, 0, tzinfo=UTC)
MAPS = ["mirage", "inferno", "mirage", "inferno"]
TEAM_X = [1, 2, 3, 4, 5]  # 1 AWPer, 2 entry, 3 lurker, 4 anchor, 5 rotator
TEAM_Y = [6, 7, 8, 9, 10]  # 6 support, 7 anchor, 8 and 9 riflers, 10 entry

# who takes the T-side opening duel, per 24 T rounds
OPENERS_X = [2] * 9 + [1] * 5 + [3] * 1 + [4] * 4 + [5] * 5
OPENERS_Y = [6] * 2 + [7] * 5 + [8] * 5 + [9] * 5 + [10] * 7

SITE_A, SITE_B, MID = (2000.0, 0.0, 0.0), (-2000.0, 0.0, 0.0), (0.0, 0.0, 0.0)
CT_SETUPS: dict[int, list[tuple[str, tuple[float, float, float]]]] = {
    1: [
        ("SnipersNest", MID),
        ("BombsiteA", SITE_A),
        ("Connector", MID),
        ("BombsiteB", SITE_B),
        ("BombsiteA", SITE_A),
    ],
    2: [("BombsiteA", SITE_A)] * 3 + [("Connector", MID)] * 2,
    3: [("BombsiteA", SITE_A)] * 3 + [("Connector", MID)] * 2,
    # the anchor stands in the site or in a place right next to it that is not called "Bombsite"
    4: [("BombsiteB", SITE_B), ("Market", (-1800.0, 150.0, 0.0))],
    5: [("Connector", MID), ("Connector", MID), ("BombsiteA", SITE_A), ("BombsiteB", SITE_B)],
    6: [("BombsiteB", SITE_B)] * 3 + [("Connector", MID)] * 2,
    7: [("BombsiteA", SITE_A)],
    8: [("BombsiteB", SITE_B)] * 3 + [("Connector", MID)] * 2,
    9: [("BombsiteA", SITE_A)] * 3 + [("Connector", MID)] * 2,
    10: [("BombsiteB", SITE_B)] * 3 + [("Connector", MID)] * 2,
}


def build_rows(*, weapons: bool = True) -> tuple[list[dict], list[dict], list[dict]]:
    """Four maps of 24 rounds between team X and team Y as (kills, positions, player rounds) rows."""
    kills: list[dict] = []
    positions: list[dict] = []
    player_rounds: list[dict] = []
    t_round = {"x": 0, "y": 0}
    kill_no = dict.fromkeys(TEAM_X + TEAM_Y, 0)

    def kill(
        where: tuple[int, str, int], side: dict[int, str], attacker: int, victim: int, opening: bool
    ) -> None:
        map_id, map_name, number = where
        kill_no[attacker] += 1
        weapon = "ak47" if side[attacker] == "t" else "m4a1_silencer"
        if attacker == 1 and kill_no[attacker] % 3 != 0:
            weapon = "awp"
        late = attacker == 3 and side[attacker] == "t"
        kills.append(
            {
                "map_played_id": map_id,
                "map_name": map_name,
                "round_number": number,
                "attacker_player_id": attacker,
                "victim_player_id": victim,
                "attacker_side": side[attacker],
                "victim_side": side[victim],
                "weapon": weapon if weapons else None,
                "is_opening": opening,
                "seconds_into_round": 78.0 if late else 24.0,
            }
        )

    for map_id, map_name in enumerate(MAPS, start=1):
        for number in range(1, 25):
            x_is_ct = (number <= 12) == (map_id % 2 == 1)
            side = dict.fromkeys(TEAM_X, "ct" if x_is_ct else "t") | dict.fromkeys(
                TEAM_Y, "t" if x_is_ct else "ct"
            )
            t_team, ct_team, key = (TEAM_Y, TEAM_X, "y") if x_is_ct else (TEAM_X, TEAM_Y, "x")
            opener = (OPENERS_Y if x_is_ct else OPENERS_X)[t_round[key] % 24]
            t_round[key] += 1
            defender = ct_team[number % 5]
            where = (map_id, map_name, number)
            # the opening duel is won alternately by the T and the CT player
            if number % 2:
                kill(where, side, opener, defender, True)
            else:
                kill(where, side, defender, opener, True)
            for k in range(4):
                attacker = (TEAM_X + TEAM_Y)[(number + 3 * map_id + k * 3) % 10]
                enemies = TEAM_Y if attacker in TEAM_X else TEAM_X
                kill(where, side, attacker, enemies[(number + k) % 5], False)

            for player in TEAM_X + TEAM_Y:
                flashed = {6: 2, 4: 1}.get(player, 1 if (number + player) % 4 == 0 else 0)
                player_rounds.append(
                    {
                        "map_played_id": map_id,
                        "round_number": number,
                        "player_id": player,
                        "side": side[player],
                        "flashes_thrown": 1,
                        "enemies_flashed": flashed,
                    }
                )
                for second in (10, 20, 30):
                    if side[player] == "ct":
                        options = CT_SETUPS[player]
                        place, (x, y, z) = options[number % len(options)]
                    elif player == 3:
                        place, (x, y, z) = "Underpass", (4000.0, 3000.0, 0.0)  # far from his team
                    else:
                        slot = t_team.index(player)
                        place = "TSpawn"
                        x, y, z = 300.0 * math.cos(slot * 1.3), 300.0 * math.sin(slot * 1.3), 0.0
                    positions.append(
                        {
                            "map_played_id": map_id,
                            "map_name": map_name,
                            "round_number": number,
                            "player_id": player,
                            "side": side[player],
                            "seconds_into_round": second,
                            "place": place,
                            "x": x,
                            "y": y,
                            "z": z,
                            "is_alive": True,
                        }
                    )
    return kills, positions, player_rounds


def build_frames(
    *, weapons: bool = True, with_positions: bool = True, with_rounds: bool = True
) -> RoleFrames:
    kills, positions, player_rounds = build_rows(weapons=weapons)
    frames = RoleFrames(kills=pd.DataFrame(kills))
    if with_positions:
        frames.positions = pd.DataFrame(positions)
    if with_rounds:
        frames.player_rounds = pd.DataFrame(player_rounds)
    return frames


EXPECTED = {
    1: PlayerRole.AWPER,
    2: PlayerRole.ENTRY,
    3: PlayerRole.LURKER,
    4: PlayerRole.ANCHOR,
    5: PlayerRole.ROTATOR,
    6: PlayerRole.SUPPORT,
    7: PlayerRole.ANCHOR,
    8: PlayerRole.RIFLER,
    9: PlayerRole.RIFLER,
    10: PlayerRole.ENTRY,
}


def test_metrics_from_handcrafted_frames():
    metrics = role_metrics(build_frames())
    assert metrics.index.tolist() == list(range(1, 11))
    assert (metrics["rounds"] == 96).all() and (metrics["t_rounds"] == 48).all()
    # AWP share: two of three kills of player 1, nobody else
    assert metrics.loc[1, "awp_share"] == pytest.approx(2 / 3, abs=0.02)
    assert (metrics.loc[2:, "awp_share"] == 0).all()
    # T-side opening duel participation = scheduled share of the 48 T rounds
    assert metrics.loc[2, "t_opening_rate"] == pytest.approx(18 / 48)
    assert metrics.loc[3, "t_opening_rate"] == pytest.approx(2 / 48)
    assert metrics.loc[6, "t_opening_rate"] == pytest.approx(4 / 48)
    assert metrics.loc[10, "t_opening_rate"] == pytest.approx(14 / 48)
    assert metrics["t_opening_attempts"].sum() == 96  # one T player per round
    # late kills: only the lurker's T-side kills happen late
    assert metrics.loc[3, "late_kill_share"] == 1.0 and metrics.loc[2, "late_kill_share"] == 0.0
    # CT setups: "Market" is learned to belong to the B site from its coordinates
    assert metrics.loc[4, "ct_site_share"] == 1.0 and metrics.loc[7, "ct_site_share"] == 1.0
    assert metrics.loc[5, "ct_site_share"] == pytest.approx(0.25)
    assert metrics.loc[2, "ct_site_share"] == pytest.approx(0.6, abs=0.03)
    assert (metrics["ct_setup_samples"] == 48).all()
    # spacing: the lurker is 2.5x as far from his team as the average player, the others 0.625x
    assert metrics.loc[3, "t_isolation"] == pytest.approx(2.5, abs=0.15)
    assert metrics.loc[[1, 2, 4, 5], "t_isolation"].max() < 0.9
    assert metrics.loc[1:5, "t_isolation"].mean() == pytest.approx(1.0)  # relative to the team average
    assert metrics.loc[6:10, "t_isolation"].between(0.6, 1.35).all()  # nobody stands apart in team Y
    assert (metrics["t_isolation_rounds"] == 48).all()
    assert metrics.loc[6, "flashed_per_round"] == 2.0


def test_roles_from_handcrafted_frames():
    roles = infer_roles(build_frames())
    assert {p: r for p, (r, _) in roles.items()} == EXPECTED
    confidence = {p: c for p, (_, c) in roles.items()}
    assert all(0.0 < c <= 1.0 for c in confidence.values())
    sample = math.sqrt(96 / 250)  # 96 rounds each: the sample-size factor caps every confidence
    assert confidence[1] == pytest.approx(sample * 1.0, abs=1e-3)  # AWP share far above "full"
    assert confidence[4] == pytest.approx(sample * 1.0, abs=1e-3)
    assert confidence[5] == pytest.approx(sample * 1.0, abs=1e-3)
    assert confidence[2] == pytest.approx(sample * 1.0, abs=1e-3)
    assert confidence[10] < confidence[2]  # a weaker entry signal
    assert confidence[8] == pytest.approx(sample * 0.30, abs=1e-3)  # fallback label: low confidence
    assert confidence[8] < confidence[6] < confidence[10]
    # IGL is never inferred
    assert PlayerRole.IGL not in {r for r, _ in roles.values()}


def test_roles_degrade_gracefully_with_less_data():
    # no weapons on the kills (like the synthetic generator): nobody can be called an AWPer
    no_weapons = infer_roles(build_frames(weapons=False))
    assert PlayerRole.AWPER not in {r for r, _ in no_weapons.values()}
    assert no_weapons[2][0] is PlayerRole.ENTRY and no_weapons[4][0] is PlayerRole.ANCHOR

    # no position samples: site and spacing rules are silent; late kills alone can still say lurker
    cfg = RoleConfig(min_kills=10)
    kills_only = infer_roles(build_frames(with_positions=False), config=cfg)
    assert kills_only[1][0] is PlayerRole.AWPER and kills_only[2][0] is PlayerRole.ENTRY
    assert kills_only[3][0] is PlayerRole.LURKER
    assert kills_only[3][1] <= 0.75 * math.sqrt(96 / 250) + 1e-4  # weaker evidence is capped
    assert kills_only[4][0] in (PlayerRole.RIFLER, PlayerRole.SUPPORT)
    assert kills_only[5][0] is PlayerRole.RIFLER

    # kills only, no per-round rows: rounds per side are reconstructed from the kill events
    bare = build_frames(with_positions=False, with_rounds=False)
    metrics = role_metrics(bare)
    assert (metrics["rounds"] == 96).all() and (metrics["t_rounds"] == 48).all()
    assert infer_roles(bare, config=cfg)[2][0] is PlayerRole.ENTRY

    # too few rounds: no role at all
    few = build_frames()
    few.kills = few.kills[few.kills["map_played_id"] == 1]
    few.positions = few.positions[few.positions["map_played_id"] == 1]
    few.player_rounds = few.player_rounds[few.player_rounds["map_played_id"] == 1]
    assert infer_roles(few) == {}
    assert len(infer_roles(few, config=RoleConfig(min_rounds=20, min_side_rounds=10, min_kills=5))) == 10
    assert infer_roles(RoleFrames()) == {}


def test_decide_role_thresholds():
    cfg = RoleConfig()
    base = {"rounds": 250.0, "t_rounds": 125.0, "ct_rounds": 125.0, "t_opening_rate": 0.20}
    assert decide_role({**base, "rounds": 59.0}, cfg) is None
    assert decide_role(base, cfg) == (PlayerRole.RIFLER, pytest.approx(0.30))
    assert decide_role({**base, "t_opening_rate": 0.10}, cfg)[0] is PlayerRole.SUPPORT
    # a quiet player who flashes less than the median is not called a support
    assert decide_role({**base, "t_opening_rate": 0.10, "flashed_per_round": 0.1}, cfg, flash_median=0.4)[
        0
    ] is (PlayerRole.RIFLER)
    awp = {**base, "weapon_kills": 100.0, "awp_share": 0.35}
    assert decide_role(awp, cfg) == (PlayerRole.AWPER, pytest.approx(0.6))
    assert decide_role({**awp, "awp_share": 0.34}, cfg)[0] is PlayerRole.RIFLER
    assert decide_role({**awp, "weapon_kills": 10.0, "awp_share": 0.9}, cfg)[0] is PlayerRole.RIFLER
    # the AWP rule wins over everything else
    busy = {**awp, "t_opening_rate": 0.40, "ct_setup_samples": 100.0, "ct_site_share": 1.0}
    assert decide_role(busy, cfg)[0] is PlayerRole.AWPER
    # otherwise the strongest evidence wins: a marginal entry who anchors one site every round
    del busy["awp_share"]
    assert decide_role({**busy, "t_opening_rate": 0.26}, cfg)[0] is PlayerRole.ANCHOR
    assert decide_role({**busy, "ct_site_share": 0.72}, cfg)[0] is PlayerRole.ENTRY
    assert (
        decide_role({**base, "ct_setup_samples": 100.0, "ct_site_share": 0.60}, cfg)[0] is PlayerRole.RIFLER
    )
    assert (
        decide_role({**base, "ct_setup_samples": 100.0, "ct_site_share": 0.40}, cfg)[0] is PlayerRole.ROTATOR
    )
    lurk = {**base, "t_isolation": 1.5, "t_isolation_rounds": 100.0}
    assert decide_role(lurk, cfg)[0] is PlayerRole.LURKER
    assert decide_role({**lurk, "t_isolation": 1.2}, cfg)[0] is PlayerRole.RIFLER
    # confidence grows with the sample
    small = decide_role({**awp, "rounds": 62.5}, cfg)
    assert small == (PlayerRole.AWPER, pytest.approx(0.3))


# ------------------------------------------------------------------------------------- database


def _persist(session: Session) -> None:
    kills, positions, player_rounds = build_rows()
    session.add_all([Team(id=1, name="X"), Team(id=2, name="Y")])
    session.flush()
    for player in TEAM_X + TEAM_Y:
        session.add(Player(id=player, nickname=f"p{player}", current_team_id=1 if player in TEAM_X else 2))
    session.flush()
    for map_id, map_name in enumerate(MAPS, start=1):
        when = T0 + timedelta(days=map_id)
        session.add(Match(id=map_id, team1_id=1, team2_id=2, scheduled_at=when, status=MatchStatus.FINISHED))
        session.flush()
        session.add(
            MapPlayed(
                id=map_id,
                match_id=map_id,
                map_number=1,
                map_name=map_name,
                status=MatchStatus.FINISHED,
                team1_rounds=13,
                team2_rounds=11,
                played_at=when,
            )
        )
    session.flush()
    for row in kills:
        session.add(
            Kill(
                map_played_id=row["map_played_id"],
                round_number=row["round_number"],
                attacker_player_id=row["attacker_player_id"],
                victim_player_id=row["victim_player_id"],
                attacker_side=Side(row["attacker_side"]),
                victim_side=Side(row["victim_side"]),
                weapon=row["weapon"],
                is_opening=row["is_opening"],
                seconds_into_round=row["seconds_into_round"],
            )
        )
    for row in positions:
        session.add(
            PlayerPositionSample(
                map_played_id=row["map_played_id"],
                round_number=row["round_number"],
                player_id=row["player_id"],
                side=Side(row["side"]),
                seconds_into_round=row["seconds_into_round"],
                place=row["place"],
                x=row["x"],
                y=row["y"],
                z=row["z"],
            )
        )
    for row in player_rounds:
        session.add(
            PlayerRoundStats(
                map_played_id=row["map_played_id"],
                round_number=row["round_number"],
                player_id=row["player_id"],
                team_id=1 if row["player_id"] in TEAM_X else 2,
                side=Side(row["side"]),
                flashes_thrown=row["flashes_thrown"],
                enemies_flashed=row["enemies_flashed"],
            )
        )
    session.flush()


def test_roles_from_the_database_and_update_rules(session: Session):
    _persist(session)
    now = T0 + timedelta(days=10)
    assert datasets.available_position_seconds(session) == [10, 20, 30]
    from_db = infer_roles(session, as_of=now)
    assert from_db == infer_roles(build_frames())
    # the window is respected: only the last two maps -> too few rounds for anybody
    assert infer_roles(session, window_days=7, as_of=now + timedelta(days=4)) == {}
    assert infer_roles(session, as_of=T0) == {}

    players = {p: session.get(Player, p) for p in TEAM_X + TEAM_Y}
    players[1].primary_role, players[1].role_confidence = PlayerRole.RIFLER, 0.99  # confident stored label
    players[2].primary_role, players[2].role_confidence = PlayerRole.SUPPORT, 0.10  # weak stored label
    players[3].primary_role = PlayerRole.IGL  # cannot be inferred: always kept
    players[4].primary_role = PlayerRole.RIFLER  # from elsewhere, no confidence: counts as 0.5
    players[5].primary_role = PlayerRole.UNKNOWN
    players[6].extra = {"hltv_team_seen_at": "2025-01-01"}
    session.flush()

    changed = update_player_roles(session, as_of=now)
    assert changed == 8  # everybody except the confident label (1) and the IGL (3)
    assert (players[1].primary_role, players[1].role_confidence) == (PlayerRole.RIFLER, 0.99)
    assert players[2].primary_role is PlayerRole.ENTRY and players[2].role_confidence == from_db[2][1]
    assert players[3].primary_role is PlayerRole.IGL and players[3].role_confidence is None
    assert players[4].primary_role is PlayerRole.ANCHOR  # 0.62 beats the assumed 0.5
    assert players[5].primary_role is PlayerRole.ROTATOR
    assert players[6].primary_role is PlayerRole.SUPPORT
    assert players[6].extra == {"hltv_team_seen_at": "2025-01-01", ROLE_INFERRED_AT_KEY: now.isoformat()}
    assert players[8].primary_role is PlayerRole.RIFLER and players[8].role_confidence == from_db[8][1]
    # idempotent
    assert update_player_roles(session, as_of=now) == 0
    # a low-confidence fallback label does not replace a role of unknown confidence
    players[9].primary_role, players[9].role_confidence = PlayerRole.LURKER, None
    session.flush()
    assert update_player_roles(session, as_of=now) == 0
    assert update_player_roles(session, as_of=now, unknown_stored_confidence=0.0) == 1
    assert players[9].primary_role is PlayerRole.RIFLER

    # an old confident label ages: half a year later the same evidence is allowed to replace it
    players[1].extra = {ROLE_INFERRED_AT_KEY: (now - timedelta(days=400)).isoformat()}
    session.flush()
    roles = infer_roles(build_frames())
    assert update_player_roles(session, as_of=now, roles=roles, confidence_half_life_days=None) == 0
    assert update_player_roles(session, as_of=now, roles=roles) == 1
    assert players[1].primary_role is PlayerRole.AWPER
    # explicit roles: IGL / UNKNOWN entries and unknown player ids are ignored
    odd = {7: (PlayerRole.IGL, 1.0), 8: (PlayerRole.UNKNOWN, 1.0), 999: (PlayerRole.AWPER, 1.0)}
    assert update_player_roles(session, as_of=now, roles=odd) == 0


def test_roles_on_synthetic_kills_without_weapons(learning_db: Session, learning_universe: SynthUniverse):
    as_of = learning_universe.matches[-1].played_at + timedelta(days=1)
    frames = datasets.load_role_frames(learning_db, window_days=120, as_of=as_of)
    assert frames.positions.empty and frames.player_rounds.empty and len(frames.kills) > 5000
    assert frames.kills["weapon"].isna().all()
    metrics = role_metrics(frames)
    # rounds are reconstructed from kill events: close to the rounds really played in the window
    start = as_of - timedelta(days=120)
    played: dict[int, int] = {}
    for match in learning_universe.matches:
        for mp in match.maps:
            if start <= match.played_at + timedelta(hours=mp.map_number - 1) < as_of:
                for team in (match.team1_id, match.team2_id):
                    for player in learning_universe.teams[team].players:
                        played[player.id] = played.get(player.id, 0) + len(mp.rounds)
    for player_id, rounds in played.items():
        assert metrics.loc[player_id, "rounds"] == pytest.approx(rounds, rel=0.03)
    assert metrics["awp_share"].isna().all() and metrics["ct_site_share"].isna().all()
    # opening duels are shared out among five players
    busy = metrics[metrics["t_rounds"] > 100]
    assert busy["t_opening_rate"].mean() == pytest.approx(0.2, abs=0.03)

    roles = infer_roles(learning_db, window_days=120, as_of=as_of)
    assert len(roles) >= 40
    assert {r for r, _ in roles.values()} <= {PlayerRole.ENTRY, PlayerRole.SUPPORT, PlayerRole.RIFLER}
    assert all(0.0 < c <= 1.0 for _, c in roles.values())
