"""Synthetic CS2 universe with *known* ground truth, for tests, backtests and demos.

Teams have latent overall strength + per-map offsets; maps have CT-side biases; rounds are played
with a simplified CS2 economy (MR12, MR3 overtime, loss bonus, buy types); kills are assigned to
players according to their skill. Because the truth is known, model tests can check that ratings
recover team ordering, that simulators are calibrated, and that backtests detect real edges.

Nothing here touches the network. ``populate_db`` writes the universe into a database using the
real ORM models (ids are set explicitly to the synthetic ids - use an empty database).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy.orm import Session

from cs2bot.config import DEFAULT_ACTIVE_MAP_POOL
from cs2bot.domain.enums import (
    BuyType,
    DataSource,
    MatchFormat,
    MatchStatus,
    PlayerRole,
    RoundEndReason,
    Side,
    TeamSlot,
    Tier,
    VetoAction,
)
from cs2bot.domain.records import MapResultRecord, PlayerMapRecord, RoundRecord, VetoRecord, VetoStep

DEFAULT_CT_BIAS: dict[str, float] = {
    "ancient": 0.15,
    "anubis": -0.05,
    "dust2": 0.0,
    "inferno": 0.10,
    "mirage": 0.05,
    "nuke": 0.25,
    "train": 0.20,
}

# Relative round-win logit contribution of each buy type (team-level).
BUY_VALUE: dict[BuyType, float] = {
    BuyType.PISTOL: 0.0,
    BuyType.ECO: -1.7,
    BuyType.FORCE: -0.75,
    BuyType.FULL: 0.0,
}

LOSS_BONUS = [1400, 1900, 2400, 2900, 3400]
WIN_REWARD = 3250
BOMB_REWARD = 3500
KILL_REWARD = 300
START_MONEY = 800
OT_START_MONEY = 10_000
MAX_MONEY = 16_000


@dataclass
class SynthPlayer:
    id: int
    nickname: str
    team_id: int
    role: PlayerRole
    skill: float  # relative fragging weight (~1.0)


@dataclass
class SynthTeam:
    id: int
    name: str
    strength: float  # round-logit units
    map_offsets: dict[str, float]
    players: list[SynthPlayer] = field(default_factory=list)
    drift: float = 0.0  # time-varying component (updated during generation)

    def map_strength(self, map_name: str) -> float:
        return self.strength + self.drift + self.map_offsets.get(map_name, 0.0)


@dataclass
class SynthRound:
    round_number: int
    team1_side: Side
    winner: TeamSlot
    end_reason: RoundEndReason
    team1_buy: BuyType
    team2_buy: BuyType
    team1_equipment: int
    team2_equipment: int
    team1_money_start: int
    team2_money_start: int
    bomb_planted: bool
    team1_survivors: int
    team2_survivors: int
    team1_score_before: int
    team2_score_before: int
    kills: list[tuple[int, int]]  # (attacker player id, victim player id), first = opening kill


@dataclass
class SynthMap:
    map_number: int
    map_name: str
    picked_by: TeamSlot | None
    team1_start_side: Side
    team1_rounds: int
    team2_rounds: int
    team1_ct_rounds: int
    team1_t_rounds: int
    team2_ct_rounds: int
    team2_t_rounds: int
    overtime_rounds: int
    rounds: list[SynthRound]


@dataclass
class SynthMatch:
    id: int
    played_at: datetime
    format: MatchFormat
    team1_id: int
    team2_id: int
    tier: Tier
    is_lan: bool
    veto: list[VetoStep]
    maps: list[SynthMap]

    @property
    def team1_maps(self) -> int:
        return sum(1 for m in self.maps if m.team1_rounds > m.team2_rounds)

    @property
    def team2_maps(self) -> int:
        return sum(1 for m in self.maps if m.team2_rounds > m.team1_rounds)


@dataclass
class SynthUniverse:
    teams: dict[int, SynthTeam]
    players: dict[int, SynthPlayer]
    map_pool: list[str]
    ct_bias: dict[str, float]
    matches: list[SynthMatch]
    round_scale: float = 1.0
    seed: int = 0

    # ---------------------------------------------------------------- records
    def map_results(self) -> list[MapResultRecord]:
        out: list[MapResultRecord] = []
        for m in self.matches:
            t1 = tuple(p.id for p in self.teams[m.team1_id].players)
            t2 = tuple(p.id for p in self.teams[m.team2_id].players)
            for mp in m.maps:
                out.append(
                    MapResultRecord(
                        match_id=m.id,
                        map_number=mp.map_number,
                        map_name=mp.map_name,
                        played_at=m.played_at + timedelta(hours=mp.map_number - 1),
                        team1_id=m.team1_id,
                        team2_id=m.team2_id,
                        team1_rounds=mp.team1_rounds,
                        team2_rounds=mp.team2_rounds,
                        team1_start_side=mp.team1_start_side,
                        team1_ct_rounds=mp.team1_ct_rounds,
                        team1_t_rounds=mp.team1_t_rounds,
                        team2_ct_rounds=mp.team2_ct_rounds,
                        team2_t_rounds=mp.team2_t_rounds,
                        overtime_rounds=mp.overtime_rounds,
                        picked_by=mp.picked_by,
                        is_lan=m.is_lan,
                        tier=m.tier,
                        format=m.format,
                        team1_lineup=t1,
                        team2_lineup=t2,
                    )
                )
        return out

    def veto_records(self) -> list[VetoRecord]:
        return [
            VetoRecord(
                match_id=m.id,
                played_at=m.played_at,
                format=m.format,
                team1_id=m.team1_id,
                team2_id=m.team2_id,
                steps=tuple(m.veto),
                map_pool=tuple(self.map_pool),
            )
            for m in self.matches
        ]

    def round_records(self) -> list[RoundRecord]:
        out: list[RoundRecord] = []
        for m in self.matches:
            for mp in m.maps:
                for r in mp.rounds:
                    out.append(
                        RoundRecord(
                            match_id=m.id,
                            map_number=mp.map_number,
                            map_name=mp.map_name,
                            played_at=m.played_at,
                            round_number=r.round_number,
                            team1_id=m.team1_id,
                            team2_id=m.team2_id,
                            team1_side=r.team1_side,
                            winner=r.winner,
                            end_reason=r.end_reason,
                            team1_equipment_value=r.team1_equipment,
                            team2_equipment_value=r.team2_equipment,
                            team1_money_start=r.team1_money_start,
                            team2_money_start=r.team2_money_start,
                            team1_buy=r.team1_buy,
                            team2_buy=r.team2_buy,
                            bomb_planted=r.bomb_planted,
                            team1_survivors=r.team1_survivors,
                            team2_survivors=r.team2_survivors,
                            team1_score_before=r.team1_score_before,
                            team2_score_before=r.team2_score_before,
                            is_lan=m.is_lan,
                        )
                    )
        return out

    def player_map_records(self) -> list[PlayerMapRecord]:
        out: list[PlayerMapRecord] = []
        for m in self.matches:
            for mp in m.maps:
                kills: dict[int, int] = {}
                deaths: dict[int, int] = {}
                first_k: dict[int, int] = {}
                first_d: dict[int, int] = {}
                for r in mp.rounds:
                    for i, (a, v) in enumerate(r.kills):
                        kills[a] = kills.get(a, 0) + 1
                        deaths[v] = deaths.get(v, 0) + 1
                        if i == 0:
                            first_k[a] = first_k.get(a, 0) + 1
                            first_d[v] = first_d.get(v, 0) + 1
                n_rounds = len(mp.rounds)
                own_rounds = {m.team1_id: mp.team1_rounds, m.team2_id: mp.team2_rounds}
                for team_id, opp_id in ((m.team1_id, m.team2_id), (m.team2_id, m.team1_id)):
                    for p in self.teams[team_id].players:
                        out.append(
                            PlayerMapRecord(
                                match_id=m.id,
                                map_number=mp.map_number,
                                map_name=mp.map_name,
                                played_at=m.played_at,
                                player_id=p.id,
                                team_id=team_id,
                                opponent_team_id=opp_id,
                                kills=kills.get(p.id, 0),
                                deaths=deaths.get(p.id, 0),
                                rounds_played=n_rounds,
                                team_rounds_won=own_rounds[team_id],
                                opponent_rounds_won=own_rounds[opp_id],
                                first_kills=first_k.get(p.id, 0),
                                first_deaths=first_d.get(p.id, 0),
                                role=p.role,
                                is_lan=m.is_lan,
                                tier=m.tier,
                            )
                        )
        return out

    # ---------------------------------------------------------------- truth
    def true_map_win_prob(
        self, team1_id: int, team2_id: int, map_name: str, *, n_sims: int = 4000, seed: int = 0
    ) -> float:
        """Ground-truth P(team1 wins the map), estimated by simulating the generator itself."""
        rng = np.random.default_rng(seed)
        wins = 0
        for _ in range(n_sims):
            start = Side.CT if rng.random() < 0.5 else Side.T
            mp = _play_map(self, rng, team1_id, team2_id, map_name, 1, None, start, with_kills=False)
            wins += mp.team1_rounds > mp.team2_rounds
        return wins / n_sims


# -------------------------------------------------------------------- generation


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _buy_decision(team_money: int, is_pistol: bool, rng: np.random.Generator) -> tuple[BuyType, int, int]:
    """Return (buy type, money spent, equipment value) for a 5-player team."""
    if is_pistol:
        spend = min(team_money, 5 * START_MONEY)
        return BuyType.PISTOL, spend, spend
    avg = team_money / 5
    if avg >= 4100:
        spend = int(5 * min(avg, 4300 + rng.normal(300, 150)))
        spend = min(spend, team_money)
        return BuyType.FULL, spend, int(spend + 5 * 200)
    if avg >= 2000 and rng.random() < 0.55:
        spend = min(team_money, int(5 * (2000 + rng.uniform(0, 600))))
        return BuyType.FORCE, spend, spend
    spend = min(team_money, int(5 * rng.uniform(150, 450)))
    return BuyType.ECO, spend, spend + 5 * 200  # pistols carried / default gear


def _play_map(
    uni: SynthUniverse,
    rng: np.random.Generator,
    team1_id: int,
    team2_id: int,
    map_name: str,
    map_number: int,
    picked_by: TeamSlot | None,
    team1_start: Side,
    *,
    with_kills: bool = True,
) -> SynthMap:
    t1, t2 = uni.teams[team1_id], uni.teams[team2_id]
    diff = uni.round_scale * (t1.map_strength(map_name) - t2.map_strength(map_name))
    ct_bias = uni.ct_bias.get(map_name, 0.0)
    score = [0, 0]
    side_rounds = {
        (TeamSlot.TEAM1, Side.CT): 0,
        (TeamSlot.TEAM1, Side.T): 0,
        (TeamSlot.TEAM2, Side.CT): 0,
        (TeamSlot.TEAM2, Side.T): 0,
    }
    money = [5 * START_MONEY, 5 * START_MONEY]
    losses = [1, 1]  # mp_starting_losses = 1
    rounds: list[SynthRound] = []
    rnd = 0
    team1_side = team1_start

    def reset_half(start_money: int) -> None:
        money[0] = money[1] = 5 * start_money
        losses[0] = losses[1] = 1

    while True:
        rnd += 1
        in_ot = rnd > 24
        if rnd == 13:
            team1_side = team1_side.other
            reset_half(START_MONEY)
        if in_ot:
            ot_round = (rnd - 25) % 6  # 0..5 within this overtime
            if ot_round == 0:
                reset_half(OT_START_MONEY)  # sides continue from previous half
            elif ot_round == 3:
                team1_side = team1_side.other
                reset_half(OT_START_MONEY)
        is_pistol = rnd in (1, 13)
        b1, s1, e1 = _buy_decision(money[0], is_pistol, rng)
        b2, s2, e2 = _buy_decision(money[1], is_pistol, rng)
        start_money = (money[0], money[1])
        money[0] -= s1
        money[1] -= s2
        side_term = ct_bias if team1_side is Side.CT else -ct_bias
        logit = diff + side_term + BUY_VALUE[b1] - BUY_VALUE[b2]
        team1_wins = rng.random() < _sigmoid(logit)
        winner = TeamSlot.TEAM1 if team1_wins else TeamSlot.TEAM2
        winner_side = team1_side if team1_wins else team1_side.other
        # end reason / deaths
        u = rng.random()
        if winner_side is Side.T:
            reason = RoundEndReason.ELIMINATION if u < 0.6 else RoundEndReason.BOMB_EXPLODED
        else:
            reason = (
                RoundEndReason.ELIMINATION
                if u < 0.7
                else RoundEndReason.BOMB_DEFUSED
                if u < 0.9
                else RoundEndReason.TIME_EXPIRED
            )
        bomb_planted = reason in (RoundEndReason.BOMB_EXPLODED, RoundEndReason.BOMB_DEFUSED) or (
            winner_side is Side.CT and reason is RoundEndReason.ELIMINATION and rng.random() < 0.3
        )
        loser_deaths = 5 if reason is RoundEndReason.ELIMINATION else int(rng.binomial(5, 0.55))
        winner_deaths = int(min(4, rng.binomial(4, 0.33)))
        w_idx, l_idx = (0, 1) if team1_wins else (1, 0)
        surv = [0, 0]
        surv[w_idx] = 5 - winner_deaths
        surv[l_idx] = 5 - loser_deaths
        kills: list[tuple[int, int]] = []
        if with_kills:
            win_team = t1 if team1_wins else t2
            lose_team = t2 if team1_wins else t1
            kills = _assign_kills(rng, win_team, lose_team, loser_deaths, winner_deaths)
        rounds.append(
            SynthRound(
                round_number=rnd,
                team1_side=team1_side,
                winner=winner,
                end_reason=reason,
                team1_buy=b1,
                team2_buy=b2,
                team1_equipment=e1,
                team2_equipment=e2,
                team1_money_start=start_money[0],
                team2_money_start=start_money[1],
                bomb_planted=bomb_planted,
                team1_survivors=surv[0],
                team2_survivors=surv[1],
                team1_score_before=score[0],
                team2_score_before=score[1],
                kills=kills,
            )
        )
        score[w_idx] += 1
        side_rounds[(winner, winner_side)] += 1
        # economy update
        reward = (
            BOMB_REWARD
            if reason in (RoundEndReason.BOMB_EXPLODED, RoundEndReason.BOMB_DEFUSED)
            else WIN_REWARD
        )
        money[w_idx] += 5 * reward + KILL_REWARD * loser_deaths
        losses[w_idx] = max(0, losses[w_idx] - 1)
        losses[l_idx] = min(5, losses[l_idx] + 1)
        bonus = LOSS_BONUS[min(losses[l_idx], 5) - 1]
        money[l_idx] += 5 * bonus + KILL_REWARD * winner_deaths
        if winner_side is Side.CT and bomb_planted:
            money[l_idx] += 800  # plant bonus to the T team
        money[0] = min(money[0], 5 * MAX_MONEY)
        money[1] = min(money[1], 5 * MAX_MONEY)
        # termination
        if rnd <= 24:
            if score[0] == 13 or score[1] == 13:
                break
        else:
            ot_index = (rnd - 25) // 6
            base = 12 + 3 * ot_index
            if score[0] - base == 4 or score[1] - base == 4:
                break
    return SynthMap(
        map_number=map_number,
        map_name=map_name,
        picked_by=picked_by,
        team1_start_side=team1_start,
        team1_rounds=score[0],
        team2_rounds=score[1],
        team1_ct_rounds=side_rounds[(TeamSlot.TEAM1, Side.CT)],
        team1_t_rounds=side_rounds[(TeamSlot.TEAM1, Side.T)],
        team2_ct_rounds=side_rounds[(TeamSlot.TEAM2, Side.CT)],
        team2_t_rounds=side_rounds[(TeamSlot.TEAM2, Side.T)],
        overtime_rounds=max(0, rnd - 24),
        rounds=rounds,
    )


def _assign_kills(
    rng: np.random.Generator, win_team: SynthTeam, lose_team: SynthTeam, loser_deaths: int, winner_deaths: int
) -> list[tuple[int, int]]:
    def pick(team: SynthTeam, weights_fn: str, exclude: set[int]) -> int:
        cands = [p for p in team.players if p.id not in exclude] or team.players
        w = np.array([p.skill if weights_fn == "skill" else 1.0 / p.skill for p in cands], dtype=float)
        return cands[int(rng.choice(len(cands), p=w / w.sum()))].id

    events: list[tuple[int, int]] = []
    dead_w: set[int] = set()
    dead_l: set[int] = set()
    w_kills, l_kills = loser_deaths, winner_deaths
    # interleave: opening kill goes to the round winner ~70% of the time
    order = ["w"] * w_kills + ["l"] * l_kills
    rng.shuffle(order)
    if w_kills and l_kills and order[0] == "l" and rng.random() < 0.4:
        i = order.index("w")
        order[0], order[i] = order[i], order[0]
    for who in order:
        if who == "w":
            a = pick(win_team, "skill", dead_w)
            v = pick(lose_team, "inv", dead_l)
            dead_l.add(v)
        else:
            a = pick(lose_team, "skill", dead_l)
            v = pick(win_team, "inv", dead_w)
            dead_w.add(v)
        events.append((a, v))
    return events


def _veto(
    uni: SynthUniverse, rng: np.random.Generator, fmt: MatchFormat, t1: SynthTeam, t2: SynthTeam
) -> tuple[list[VetoStep], list[tuple[str, TeamSlot | None]]]:
    """Teams ban their relatively worst maps and pick their best (with noise). Team1 starts."""
    remaining = list(uni.map_pool)
    steps: list[VetoStep] = []
    played: list[tuple[str, TeamSlot | None]] = []
    if fmt is MatchFormat.BO1:
        template = [("ban", TeamSlot.TEAM1), ("ban", TeamSlot.TEAM2)] * 3
    elif fmt is MatchFormat.BO5:
        template = [
            ("ban", TeamSlot.TEAM1),
            ("ban", TeamSlot.TEAM2),
            ("pick", TeamSlot.TEAM1),
            ("pick", TeamSlot.TEAM2),
            ("pick", TeamSlot.TEAM1),
            ("pick", TeamSlot.TEAM2),
        ]
    else:  # BO3 (and BO2 uses the same opening)
        template = [
            ("ban", TeamSlot.TEAM1),
            ("ban", TeamSlot.TEAM2),
            ("pick", TeamSlot.TEAM1),
            ("pick", TeamSlot.TEAM2),
            ("ban", TeamSlot.TEAM1),
            ("ban", TeamSlot.TEAM2),
        ]
    for i, (action, slot) in enumerate(template, start=1):
        me, opp = (t1, t2) if slot is TeamSlot.TEAM1 else (t2, t1)
        adv = np.array([me.map_strength(m) - opp.map_strength(m) + rng.normal(0, 0.08) for m in remaining])
        idx = int(np.argmax(adv)) if action == "pick" else int(np.argmin(adv))
        m = remaining.pop(idx)
        steps.append(VetoStep(step=i, action=VetoAction(action), team=slot, map_name=m))
        if action == "pick":
            played.append((m, slot))
    leftover = remaining[0]
    steps.append(VetoStep(step=len(steps) + 1, action=VetoAction.LEFTOVER, team=None, map_name=leftover))
    if fmt is not MatchFormat.BO2:
        played.append((leftover, None))
    return steps, played


def generate_universe(
    *,
    n_teams: int = 16,
    n_matches: int = 400,
    seed: int = 0,
    start: datetime | None = None,
    days: int = 365,
    map_pool: list[str] | None = None,
    strength_sd: float = 0.35,
    map_offset_sd: float = 0.15,
    drift_sd_per_30d: float = 0.0,
    with_kills: bool = True,
) -> SynthUniverse:
    rng = np.random.default_rng(seed)
    start = start or datetime(2024, 1, 1, tzinfo=UTC)
    pool = list(map_pool or DEFAULT_ACTIVE_MAP_POOL)
    teams: dict[int, SynthTeam] = {}
    players: dict[int, SynthPlayer] = {}
    roles = [PlayerRole.AWPER, PlayerRole.ENTRY, PlayerRole.IGL, PlayerRole.SUPPORT, PlayerRole.LURKER]
    role_skill = {
        PlayerRole.AWPER: 1.18,
        PlayerRole.ENTRY: 1.05,
        PlayerRole.IGL: 0.82,
        PlayerRole.SUPPORT: 0.9,
        PlayerRole.LURKER: 1.05,
    }
    pid = 1
    for tid in range(1, n_teams + 1):
        team = SynthTeam(
            id=tid,
            name=f"Synth Team {tid}",
            strength=float(rng.normal(0, strength_sd)),
            map_offsets={m: float(rng.normal(0, map_offset_sd)) for m in pool},
        )
        for role in roles:
            p = SynthPlayer(
                id=pid,
                nickname=f"s{tid}_{role.value}",
                team_id=tid,
                role=role,
                skill=float(role_skill[role] * math.exp(rng.normal(0, 0.12))),
            )
            team.players.append(p)
            players[pid] = p
            pid += 1
        teams[tid] = team

    uni = SynthUniverse(
        teams=teams,
        players=players,
        map_pool=pool,
        ct_bias={m: DEFAULT_CT_BIAS.get(m, 0.05) for m in pool},
        matches=[],
        seed=seed,
    )
    times = sorted(
        start + timedelta(minutes=float(x)) for x in rng.uniform(0, days * 24 * 60, size=n_matches)
    )
    last_drift_t = start
    for mid, t in enumerate(times, start=1):
        if drift_sd_per_30d > 0:
            dt_days = (t - last_drift_t).total_seconds() / 86400
            for tm in teams.values():
                tm.drift += float(rng.normal(0, drift_sd_per_30d * math.sqrt(max(dt_days, 0) / 30)))
            last_drift_t = t
        a, b = (int(x) for x in rng.choice(list(teams), size=2, replace=False))
        u = rng.random()
        fmt = MatchFormat.BO1 if u < 0.25 else MatchFormat.BO5 if u > 0.95 else MatchFormat.BO3
        tier = Tier.S if rng.random() < 0.3 else Tier.A if rng.random() < 0.6 else Tier.B
        steps, to_play = _veto(uni, rng, fmt, teams[a], teams[b])
        maps: list[SynthMap] = []
        wins = [0, 0]
        need = fmt.maps_to_win
        for k, (map_name, picker) in enumerate(to_play, start=1):
            if need is not None and max(wins) >= need:
                break
            # the team that did NOT pick chooses the side; decider: coin flip
            start_side = Side.CT if rng.random() < 0.5 else Side.T
            if picker is not None:
                chooser = picker.other
                prefers_ct = uni.ct_bias.get(map_name, 0.0) > 0
                chooser_side = Side.CT if prefers_ct else Side.T
                start_side = chooser_side if chooser is TeamSlot.TEAM1 else chooser_side.other
            mp = _play_map(uni, rng, a, b, map_name, k, picker, start_side, with_kills=with_kills)
            maps.append(mp)
            wins[0 if mp.team1_rounds > mp.team2_rounds else 1] += 1
        uni.matches.append(
            SynthMatch(
                id=mid,
                played_at=t,
                format=fmt,
                team1_id=a,
                team2_id=b,
                tier=tier,
                is_lan=bool(rng.random() < 0.4),
                veto=steps,
                maps=maps,
            )
        )
    return uni


def synthetic_two_way_odds(
    p_team1: float, *, margin: float = 0.07, noise_sd: float = 0.03, rng: np.random.Generator | None = None
) -> tuple[float, float]:
    """Bookmaker-like decimal odds around a true probability: noisy belief + proportional margin."""
    rng = rng or np.random.default_rng()
    belief = float(np.clip(p_team1 + rng.normal(0, noise_sd), 0.02, 0.98))
    over = 1.0 + margin
    return round(1.0 / (belief * over), 3), round(1.0 / ((1.0 - belief) * over), 3)


def populate_db(
    session: Session, uni: SynthUniverse, *, with_rounds: bool = True, with_kills: bool = False
) -> None:
    """Write the universe into the DB (empty DB expected; synthetic ids are used as primary keys)."""
    from cs2bot.db.models import (
        Kill,
        MapPlayed,
        Match,
        MatchLineup,
        Player,
        PlayerMapStats,
        Round,
        Team,
        TeamAlias,
        Tournament,
        Veto,
    )
    from cs2bot.utils.names import normalize_team_name

    for t in uni.teams.values():
        session.add(Team(id=t.id, name=t.name, slug=f"synth-{t.id}"))
    session.flush()
    for t in uni.teams.values():
        session.add(TeamAlias(team_id=t.id, alias=normalize_team_name(t.name), source=DataSource.SYNTHETIC))
        for p in t.players:
            session.add(Player(id=p.id, nickname=p.nickname, current_team_id=t.id, primary_role=p.role))
    tours = {
        tier: Tournament(name=f"Synthetic {tier.value.upper()}-tier", tier=tier)
        for tier in (Tier.S, Tier.A, Tier.B)
    }
    for tour in tours.values():
        session.add(tour)
    session.flush()
    pm_by_key = {(r.match_id, r.map_number, r.player_id): r for r in uni.player_map_records()}
    map_row_id = 0
    for m in uni.matches:
        winner = (
            m.team1_id if m.team1_maps > m.team2_maps else m.team2_id if m.team2_maps > m.team1_maps else None
        )
        session.add(
            Match(
                id=m.id,
                tournament_id=tours[m.tier].id if m.tier in tours else None,
                team1_id=m.team1_id,
                team2_id=m.team2_id,
                scheduled_at=m.played_at,
                status=MatchStatus.FINISHED,
                format=m.format,
                team1_score=m.team1_maps,
                team2_score=m.team2_maps,
                winner_team_id=winner,
                is_lan=m.is_lan,
                tier=m.tier,
                in_scope=True,
                data_completeness=1.0,
            )
        )
        session.flush()
        for st in m.veto:
            session.add(
                Veto(
                    match_id=m.id,
                    step=st.step,
                    team_id=(m.team1_id if st.team is TeamSlot.TEAM1 else m.team2_id if st.team else None),
                    action=st.action,
                    map_name=st.map_name,
                )
            )
        for team_id in (m.team1_id, m.team2_id):
            for p in uni.teams[team_id].players:
                session.add(
                    MatchLineup(match_id=m.id, team_id=team_id, player_id=p.id, source=DataSource.SYNTHETIC)
                )
        for mp in m.maps:
            map_row_id += 1
            picked = m.team1_id if mp.picked_by is TeamSlot.TEAM1 else m.team2_id if mp.picked_by else None
            session.add(
                MapPlayed(
                    id=map_row_id,
                    match_id=m.id,
                    map_number=mp.map_number,
                    map_name=mp.map_name,
                    status=MatchStatus.FINISHED,
                    team1_rounds=mp.team1_rounds,
                    team2_rounds=mp.team2_rounds,
                    winner_team_id=m.team1_id if mp.team1_rounds > mp.team2_rounds else m.team2_id,
                    team1_start_side=mp.team1_start_side,
                    team1_ct_rounds=mp.team1_ct_rounds,
                    team1_t_rounds=mp.team1_t_rounds,
                    team2_ct_rounds=mp.team2_ct_rounds,
                    team2_t_rounds=mp.team2_t_rounds,
                    overtime_rounds=mp.overtime_rounds,
                    picked_by_team_id=picked,
                    is_decider=mp.picked_by is None,
                    played_at=m.played_at + timedelta(hours=mp.map_number - 1),
                )
            )
            session.flush()
            for team_id in (m.team1_id, m.team2_id):
                for p in uni.teams[team_id].players:
                    rec = pm_by_key[(m.id, mp.map_number, p.id)]
                    session.add(
                        PlayerMapStats(
                            map_played_id=map_row_id,
                            player_id=p.id,
                            team_id=team_id,
                            source=DataSource.SYNTHETIC,
                            kills=rec.kills,
                            deaths=rec.deaths,
                            rounds_played=rec.rounds_played,
                            first_kills=rec.first_kills,
                            first_deaths=rec.first_deaths,
                        )
                    )
            if with_rounds:
                for r in mp.rounds:
                    ct_is_team1 = r.team1_side is Side.CT
                    session.add(
                        Round(
                            map_played_id=map_row_id,
                            round_number=r.round_number,
                            team1_side=r.team1_side,
                            winner_side=r.team1_side if r.winner is TeamSlot.TEAM1 else r.team1_side.other,
                            winner_team_id=m.team1_id if r.winner is TeamSlot.TEAM1 else m.team2_id,
                            source=DataSource.SYNTHETIC,
                            end_reason=r.end_reason,
                            ct_equipment_value=r.team1_equipment if ct_is_team1 else r.team2_equipment,
                            t_equipment_value=r.team2_equipment if ct_is_team1 else r.team1_equipment,
                            ct_money_start=r.team1_money_start if ct_is_team1 else r.team2_money_start,
                            t_money_start=r.team2_money_start if ct_is_team1 else r.team1_money_start,
                            ct_buy_type=r.team1_buy if ct_is_team1 else r.team2_buy,
                            t_buy_type=r.team2_buy if ct_is_team1 else r.team1_buy,
                            bomb_planted=r.bomb_planted,
                            ct_survivors=r.team1_survivors if ct_is_team1 else r.team2_survivors,
                            t_survivors=r.team2_survivors if ct_is_team1 else r.team1_survivors,
                        )
                    )
                    if with_kills:
                        for i, (a, v) in enumerate(r.kills):
                            a_team1 = uni.players[a].team_id == m.team1_id
                            a_side = r.team1_side if a_team1 else r.team1_side.other
                            session.add(
                                Kill(
                                    map_played_id=map_row_id,
                                    round_number=r.round_number,
                                    attacker_player_id=a,
                                    victim_player_id=v,
                                    attacker_side=a_side,
                                    victim_side=a_side.other,
                                    is_opening=i == 0,
                                )
                            )
    session.flush()
