"""Store a :class:`ParsedDemo` in the database and link demo players to ``players`` rows.

Orientation
-----------
The demo only knows "the five that started CT" (roster ``a``) and "the other five" (roster ``b``);
the database is oriented by ``matches.team1_id`` / ``team2_id``. :func:`persist_parsed_demo` decides
which roster is team1 from, in this order: the resolved players' teams (match lineup / HLTV map
stats), the clan names in the demo, the map's known ``team1_start_side`` and finally the known final
score. If none of them gives an answer it raises :class:`DemoPersistError` - a guessed orientation
would silently poison every side-dependent statistic.

Steam ids
---------
A player can own several accounts (tournament organisers hand out LAN accounts, and such accounts
are occasionally reused by other players at later events). Therefore ``players.extra`` keeps
``"steamid"`` (the first id seen, as a string because 17-digit integers do not survive JavaScript)
plus ``"steamids"`` (all ids seen), and a known id is trusted only *within the match's own players*,
never against a contradicting exact nickname.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from cs2bot.db.models import (
    Kill,
    MapPlayed,
    Match,
    MatchLineup,
    Player,
    PlayerMapStats,
    PlayerPositionSample,
    PlayerRoundStats,
    Round,
    Team,
)
from cs2bot.domain.enums import BuyType, DataSource, RoundEndReason, Side
from cs2bot.ingest.demos._values import records, to_bool, to_float, to_int, to_steamid, to_str
from cs2bot.ingest.demos.errors import DemoPersistError
from cs2bot.ingest.demos.parse import ParsedDemo
from cs2bot.utils.logging import get_logger
from cs2bot.utils.names import normalize_map_name, normalize_player_name, team_name_similarity
from cs2bot.utils.time import utcnow

log = get_logger(__name__)

EXTRA_KEY = "demo"  # maps_played.extra["demo"]: summary of what the demo parser stored
MIN_NAME_SIMILARITY = 85.0  # fuzzy nickname match (0..100)
NAME_AMBIGUITY_MARGIN = 6.0  # the best candidate must beat the runner-up by this much
MIN_TEAM_NAME_SIMILARITY = 90.0
TEAM_NAME_MARGIN = 40.0


# ------------------------------------------------------------------------------------------- steam ids


def known_steamids(player: Player) -> set[int]:
    """Every steam id recorded for a player (``extra["steamid"]`` and ``extra["steamids"]``)."""
    extra = player.extra or {}
    raw = [extra.get("steamid")]
    more = extra.get("steamids")
    if isinstance(more, list | tuple):
        raw.extend(more)
    return {steamid for value in raw if (steamid := to_steamid(value)) is not None}


def remember_steamid(player: Player, steamid: int) -> bool:
    """Record ``steamid`` on the player. Returns True when something changed.

    A *new* dict is assigned to ``player.extra``: SQLAlchemy does not notice in-place changes of a
    plain JSON column.
    """
    known = known_steamids(player)
    if steamid in known:
        return False
    extra = dict(player.extra or {})
    if to_steamid(extra.get("steamid")) is None:
        extra["steamid"] = str(steamid)
    extra["steamids"] = sorted({str(s) for s in known} | {str(steamid)})
    player.extra = extra
    return True


def _match_players(session: Session, match_id: int) -> dict[int, tuple[Player, int | None]]:
    """``{player_id: (Player, team_id)}`` for everyone known to have played the match."""
    team_of: dict[int, int | None] = {}
    for lineup in session.scalars(select(MatchLineup).where(MatchLineup.match_id == match_id)):
        team_of[lineup.player_id] = lineup.team_id
    stats = session.execute(
        select(PlayerMapStats.player_id, PlayerMapStats.team_id)
        .join(MapPlayed, MapPlayed.id == PlayerMapStats.map_played_id)
        .where(MapPlayed.match_id == match_id)
    )
    for player_id, team_id in stats:
        team_of.setdefault(player_id, team_id)
    if not team_of:
        return {}
    players = session.scalars(select(Player).where(Player.id.in_(sorted(team_of))))
    return {player.id: (player, team_of[player.id]) for player in players}


def name_similarity(demo_name: str, nickname: str) -> float:
    """0..100 similarity between an in-game name and a nickname, both normalised.

    In-game names often carry a tag or sponsor around the nickname (``"NAVI s1mple"``,
    ``"ZywOo-Vitality"``): a nickname of three or more characters contained in the name scores 95.
    """
    a, b = normalize_player_name(demo_name), normalize_player_name(nickname)
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 3 and shorter in longer:
        return 95.0
    return float(fuzz.ratio(a, b))


def resolve_steamids(session: Session, match_id: int, parsed: ParsedDemo) -> dict[int, int]:
    """Map the demo's steam ids to ``players.id`` for one match.

    Order of evidence, restricted to the match's own players (lineups, HLTV map stats):

    1. a steam id already stored on a player (unless the in-game name is exactly another lineup
       player's nickname, which means a reused account);
    2. exact normalised nickname;
    3. fuzzy nickname (unambiguous and at least :data:`MIN_NAME_SIMILARITY`);
    4. elimination: one unmatched demo player in a roster whose other members all belong to one
       team that has exactly one unmatched lineup player.

    A demo player outside the lineup is matched by a stored steam id against all players only when
    the nickname is similar as well. Newly learned ids are stored in ``Player.extra``. Unmatched steam
    ids are simply missing from the result; the function never raises for bad names.
    """
    candidates = _match_players(session, match_id)
    demo_players: list[tuple[int, str, str]] = []
    for row in records(parsed.players):
        steamid = to_steamid(row.get("steamid"))
        if steamid is not None:
            demo_players.append((steamid, to_str(row.get("name")) or "", to_str(row.get("roster")) or ""))

    result: dict[int, int] = {}
    taken: set[int] = set()
    learned: list[tuple[Player, int]] = []
    nick_exact: dict[str, list[int]] = {}
    for player_id, (player, _team) in candidates.items():
        nick_exact.setdefault(normalize_player_name(player.nickname), []).append(player_id)

    def assign(steamid: int, player_id: int, *, learn: bool) -> None:
        result[steamid] = player_id
        taken.add(player_id)
        if learn:
            learned.append((candidates[player_id][0], steamid))

    # 1) steam ids already known for the match's players
    by_steamid: dict[int, int] = {}
    for player_id, (player, _team) in candidates.items():
        for steamid in known_steamids(player):
            by_steamid.setdefault(steamid, player_id)
    for steamid, name, _roster in demo_players:
        owner_id = by_steamid.get(steamid)
        if owner_id is None or owner_id in taken:
            continue
        exact = nick_exact.get(normalize_player_name(name), []) if name else []
        if exact and owner_id not in exact:
            log.warning(
                "steam id %s is stored for player %s but is used by '%s' in this demo",
                steamid,
                owner_id,
                name,
            )
            continue
        assign(steamid, owner_id, learn=False)

    # 2) exact nickname
    for steamid, name, _roster in demo_players:
        if steamid in result or not name:
            continue
        exact = [pid for pid in nick_exact.get(normalize_player_name(name), []) if pid not in taken]
        if len(exact) == 1:
            assign(steamid, exact[0], learn=True)

    # 3) fuzzy nickname, best pairs first, skipping ambiguous ones
    scored: list[tuple[float, int, int]] = []
    for steamid, name, _roster in demo_players:
        if steamid in result or not name:
            continue
        options = sorted(
            (
                (name_similarity(name, player.nickname), pid)
                for pid, (player, _t) in candidates.items()
                if pid not in taken
            ),
            reverse=True,
        )
        if not options or options[0][0] < MIN_NAME_SIMILARITY:
            continue
        if len(options) > 1 and options[0][0] - options[1][0] < NAME_AMBIGUITY_MARGIN:
            continue
        scored.append((options[0][0], steamid, options[0][1]))
    by_player: Counter[int] = Counter(pid for _score, _steamid, pid in scored)
    for _score, steamid, player_id in sorted(scored, reverse=True):
        if by_player[player_id] == 1 and player_id not in taken:
            assign(steamid, player_id, learn=True)

    # 4) elimination inside a roster
    roster_of = {steamid: roster for steamid, _name, roster in demo_players}
    for roster in sorted(set(roster_of.values())):
        members = [steamid for steamid, key in roster_of.items() if key == roster]
        missing = [steamid for steamid in members if steamid not in result]
        teams = Counter(candidates[result[steamid]][1] for steamid in members if steamid in result)
        if len(missing) != 1 or len(teams) != 1 or len(members) < 2:
            continue
        ((team_id, votes),) = teams.items()
        free = [pid for pid, (_player, team) in candidates.items() if team == team_id and pid not in taken]
        if team_id is not None and votes == len(members) - 1 and votes >= 3 and len(free) == 1:
            assign(missing[0], free[0], learn=True)

    # 5) players outside the lineup: a stored steam id plus a matching nickname
    unresolved = {steamid: name for steamid, name, _roster in demo_players if steamid not in result}
    if unresolved:
        for player in session.scalars(select(Player).where(Player.id.not_in(sorted(candidates) or [0]))):
            for steamid in known_steamids(player) & set(unresolved):
                if steamid in result or player.id in taken:
                    continue
                if name_similarity(unresolved[steamid], player.nickname) >= MIN_NAME_SIMILARITY:
                    result[steamid] = player.id
                    taken.add(player.id)

    changed = False
    for player, steamid in learned:
        changed = remember_steamid(player, steamid) or changed
    if changed:
        session.flush()
    return result


# ----------------------------------------------------------------------------------------- orientation


def _other(roster: str) -> str:
    return "b" if roster == "a" else "a"


def _player_teams(session: Session, match: Match, map_played: MapPlayed) -> dict[int, int | None]:
    team_of: dict[int, int | None] = {}
    for lineup in session.scalars(select(MatchLineup).where(MatchLineup.match_id == match.id)):
        team_of[lineup.player_id] = lineup.team_id
    for stat in session.scalars(select(PlayerMapStats).where(PlayerMapStats.map_played_id == map_played.id)):
        team_of.setdefault(stat.player_id, stat.team_id)
    return team_of


def _demo_score(parsed: ParsedDemo, team1_roster: str) -> tuple[int, int]:
    return (
        int(parsed.roster_scores.get(team1_roster, 0)),
        int(parsed.roster_scores.get(_other(team1_roster), 0)),
    )


def determine_team1_roster(
    session: Session,
    match: Match,
    map_played: MapPlayed,
    parsed: ParsedDemo,
    steamid_to_player_id: dict[int, int],
) -> tuple[str, str]:
    """Which demo roster (``"a"`` / ``"b"``) is ``matches.team1`` and how that was decided.

    Raises :class:`DemoPersistError` when no evidence is available (see the module docstring).
    """
    team_of = _player_teams(session, match, map_played)
    votes: Counter[str] = Counter()
    for row in records(parsed.players):
        steamid, roster = to_steamid(row.get("steamid")), to_str(row.get("roster"))
        player_id = steamid_to_player_id.get(steamid) if steamid is not None else None
        team_id = team_of.get(player_id) if player_id is not None else None
        if roster not in ("a", "b") or team_id is None:
            continue
        if team_id == match.team1_id:
            votes[roster] += 1
        elif team_id == match.team2_id:
            votes[_other(roster)] += 1
    if votes["a"] != votes["b"]:
        return ("a" if votes["a"] > votes["b"] else "b"), "lineup"

    team1 = session.get(Team, match.team1_id) if match.team1_id is not None else None
    team2 = session.get(Team, match.team2_id) if match.team2_id is not None else None
    name_a, name_b = parsed.roster_names.get("a"), parsed.roster_names.get("b")

    def similarity(clan: str | None, team: Team | None) -> float:
        return team_name_similarity(clan, team.name) if clan and team is not None else 0.0

    direct = (similarity(name_a, team1), similarity(name_b, team2))
    swapped = (similarity(name_a, team2), similarity(name_b, team1))
    if abs(sum(direct) - sum(swapped)) >= TEAM_NAME_MARGIN:
        best, roster = (direct, "a") if sum(direct) > sum(swapped) else (swapped, "b")
        if max(best) >= MIN_TEAM_NAME_SIMILARITY:
            return roster, "clan_name"

    rounds = records(parsed.rounds)
    if map_played.team1_start_side is not None and rounds:
        first = rounds[0]
        number, ct_roster = to_int(first.get("round_number")), to_str(first.get("ct_roster"))
        if number is not None and number <= 12 and ct_roster in ("a", "b"):
            return (ct_roster if map_played.team1_start_side is Side.CT else _other(ct_roster)), "start_side"

    if map_played.team1_rounds is not None and map_played.team2_rounds is not None:
        known = (map_played.team1_rounds, map_played.team2_rounds)
        if known[0] != known[1]:
            for roster in ("a", "b"):
                if _demo_score(parsed, roster) == known:
                    return roster, "score"

    raise DemoPersistError(
        f"map {map_played.id}: cannot tell which demo roster is team1 "
        f"(no resolved players, clan names {name_a!r}/{name_b!r}, no start side, no matching score)"
    )


# ----------------------------------------------------------------------------------------- persistence


def _enum_or_none[E: (Side, BuyType, RoundEndReason)](enum_cls: type[E], value: Any) -> E | None:
    text = to_str(value)
    if text is None:
        return None
    try:
        return enum_cls(text)
    except ValueError:
        return None


def _clip(value: Any, length: int) -> str | None:
    text = to_str(value)
    return text[:length] if text is not None else None


def persist_parsed_demo(
    session: Session, map_played_id: int, parsed: ParsedDemo, steamid_to_player_id: dict[int, int]
) -> int:
    """Replace the demo-derived rows of one map with ``parsed``; returns the number of rounds stored.

    ``Round`` (any source: demo rows are the richer data and replace HLTV round-history rows),
    ``Kill``, ``PlayerRoundStats`` and ``PlayerPositionSample`` rows of the map are deleted and
    rewritten, so the call is idempotent. Steam ids missing from ``steamid_to_player_id`` become
    ``NULL`` player ids on kills and are skipped for per-player rows (those need a player). The
    session is flushed, not committed. A summary is written to ``maps_played.extra["demo"]``.

    Raises :class:`DemoPersistError` - before anything is deleted - when the map row is missing, the
    demo is of another map, has no rounds, or it cannot be told which demo roster is team1.
    """
    map_played = session.get(MapPlayed, map_played_id)
    if map_played is None:
        raise DemoPersistError(f"maps_played row {map_played_id} does not exist")
    match = session.get(Match, map_played.match_id)
    if match is None:
        raise DemoPersistError(f"match {map_played.match_id} of map {map_played_id} does not exist")
    round_rows = records(parsed.rounds)
    if not round_rows:
        raise DemoPersistError(f"map {map_played_id}: the parsed demo has no rounds")
    if parsed.map_name and normalize_map_name(map_played.map_name) != normalize_map_name(parsed.map_name):
        raise DemoPersistError(
            f"map {map_played_id} is {map_played.map_name!r} but the demo was played on {parsed.map_name!r}"
        )
    team1_roster, method = determine_team1_roster(session, match, map_played, parsed, steamid_to_player_id)
    team_of_roster = {team1_roster: match.team1_id, _other(team1_roster): match.team2_id}
    warnings = list(parsed.warnings)

    for model in (Round, Kill, PlayerRoundStats, PlayerPositionSample):
        session.execute(
            delete(model)
            .where(model.map_played_id == map_played_id)
            .execution_options(synchronize_session="fetch")
        )

    def player_id(raw: Any) -> int | None:
        steamid = to_steamid(raw)
        return steamid_to_player_id.get(steamid) if steamid is not None else None

    stored_rounds = 0
    seen_rounds: set[int] = set()
    for row in round_rows:
        number = to_int(row.get("round_number"))
        winner_side = _enum_or_none(Side, row.get("winner_side"))
        ct_roster = to_str(row.get("ct_roster"))
        if number is None or winner_side is None or ct_roster not in ("a", "b") or number in seen_rounds:
            warnings.append(
                f"round row skipped (number={number}, winner={winner_side}, ct_roster={ct_roster})"
            )
            continue
        seen_rounds.add(number)
        team1_side = Side.CT if ct_roster == team1_roster else Side.T
        session.add(
            Round(
                map_played_id=map_played_id,
                round_number=number,
                team1_side=team1_side,
                winner_side=winner_side,
                winner_team_id=match.team1_id if winner_side is team1_side else match.team2_id,
                source=DataSource.DEMO,
                end_reason=_enum_or_none(RoundEndReason, row.get("end_reason")) or RoundEndReason.OTHER,
                ct_equipment_value=to_int(row.get("ct_equipment_value")),
                t_equipment_value=to_int(row.get("t_equipment_value")),
                ct_money_start=to_int(row.get("ct_money_start")),
                t_money_start=to_int(row.get("t_money_start")),
                ct_buy_type=_enum_or_none(BuyType, row.get("ct_buy_type")),
                t_buy_type=_enum_or_none(BuyType, row.get("t_buy_type")),
                bomb_planted=to_bool(row.get("bomb_planted")),
                bomb_site=_clip(row.get("bomb_site"), 8),
                ct_survivors=to_int(row.get("ct_survivors")),
                t_survivors=to_int(row.get("t_survivors")),
                duration_s=to_float(row.get("duration_s")),
            )
        )
        stored_rounds += 1

    for row in records(parsed.kills):
        number = to_int(row.get("round_number"))
        if number is None or number not in seen_rounds:
            continue
        session.add(
            Kill(
                map_played_id=map_played_id,
                round_number=number,
                tick=to_int(row.get("tick")),
                seconds_into_round=to_float(row.get("seconds_into_round")),
                attacker_player_id=player_id(row.get("attacker_steamid")),
                victim_player_id=player_id(row.get("victim_steamid")),
                assister_player_id=player_id(row.get("assister_steamid")),
                attacker_side=_enum_or_none(Side, row.get("attacker_side")),
                victim_side=_enum_or_none(Side, row.get("victim_side")),
                weapon=_clip(row.get("weapon"), 32),
                headshot=bool(to_bool(row.get("headshot"))),
                penetrated=to_bool(row.get("penetrated")),
                through_smoke=to_bool(row.get("through_smoke")),
                noscope=to_bool(row.get("noscope")),
                attacker_blind=to_bool(row.get("attacker_blind")),
                flash_assisted=to_bool(row.get("flash_assisted")),
                attacker_place=_clip(row.get("attacker_place"), 48),
                victim_place=_clip(row.get("victim_place"), 48),
                attacker_x=to_float(row.get("attacker_x")),
                attacker_y=to_float(row.get("attacker_y")),
                attacker_z=to_float(row.get("attacker_z")),
                victim_x=to_float(row.get("victim_x")),
                victim_y=to_float(row.get("victim_y")),
                victim_z=to_float(row.get("victim_z")),
                distance=to_float(row.get("distance")),
                is_opening=bool(to_bool(row.get("is_opening"))),
                is_trade=bool(to_bool(row.get("is_trade"))),
                was_traded=bool(to_bool(row.get("was_traded"))),
            )
        )

    unresolved: set[int] = set()
    seen_player_rounds: set[tuple[int, int]] = set()
    for row in records(parsed.player_rounds):
        number = to_int(row.get("round_number"))
        side = _enum_or_none(Side, row.get("side"))
        steamid = to_steamid(row.get("steamid"))
        if number is None or number not in seen_rounds or side is None or steamid is None:
            continue
        pid = steamid_to_player_id.get(steamid)
        if pid is None:
            unresolved.add(steamid)
            continue
        if (number, pid) in seen_player_rounds:
            warnings.append(f"round {number}: two demo players map to player {pid}; second one skipped")
            continue
        seen_player_rounds.add((number, pid))
        session.add(
            PlayerRoundStats(
                map_played_id=map_played_id,
                round_number=number,
                player_id=pid,
                team_id=team_of_roster.get(to_str(row.get("roster")) or ""),
                side=side,
                kills=to_int(row.get("kills")) or 0,
                deaths=to_int(row.get("deaths")) or 0,
                assists=to_int(row.get("assists")) or 0,
                damage=to_int(row.get("damage")) or 0,
                utility_damage=to_int(row.get("utility_damage")) or 0,
                enemies_flashed=to_int(row.get("enemies_flashed")) or 0,
                flashes_thrown=to_int(row.get("flashes_thrown")) or 0,
                smokes_thrown=to_int(row.get("smokes_thrown")) or 0,
                he_thrown=to_int(row.get("he_thrown")) or 0,
                molotovs_thrown=to_int(row.get("molotovs_thrown")) or 0,
                equipment_value=to_int(row.get("equipment_value")),
                money_start=to_int(row.get("money_start")),
                survived=bool(to_bool(row.get("survived"))),
                kast=to_bool(row.get("kast")),
                opening_kill=bool(to_bool(row.get("opening_kill"))),
                opening_death=bool(to_bool(row.get("opening_death"))),
                clutch_situation=_clip(row.get("clutch_situation"), 8),
                clutch_won=to_bool(row.get("clutch_won")),
                primary_weapon=_clip(row.get("primary_weapon"), 32),
            )
        )

    for row in records(parsed.positions):
        number = to_int(row.get("round_number"))
        side = _enum_or_none(Side, row.get("side"))
        pid = player_id(row.get("steamid"))
        second = to_int(row.get("seconds_into_round"))
        if number is None or number not in seen_rounds or side is None or pid is None or second is None:
            continue
        session.add(
            PlayerPositionSample(
                map_played_id=map_played_id,
                round_number=number,
                player_id=pid,
                side=side,
                seconds_into_round=second,
                place=_clip(row.get("place"), 48),
                x=to_float(row.get("x")),
                y=to_float(row.get("y")),
                z=to_float(row.get("z")),
                is_alive=True,
            )
        )

    score = _demo_score(parsed, team1_roster)
    score_matches: bool | None = None
    if map_played.team1_rounds is not None and map_played.team2_rounds is not None:
        score_matches = score == (map_played.team1_rounds, map_played.team2_rounds)
        if not score_matches:
            warnings.append(
                f"demo score {score[0]}-{score[1]} differs from the recorded result "
                f"{map_played.team1_rounds}-{map_played.team2_rounds}"
            )
    if unresolved:
        warnings.append(f"{len(unresolved)} demo player(s) not linked to a player row")
    summary: dict[str, Any] = {
        "parser_version": parsed.parser_version,
        "rounds": stored_rounds,
        "score": [score[0], score[1]],
        "score_matches_result": score_matches,
        "orientation": method,
        "team1_roster": team1_roster,
        "unresolved_steamids": sorted(str(s) for s in unresolved),
        "dropped_rounds": dict(parsed.dropped_rounds),
        "warnings": warnings[:25],
        "persisted_at": utcnow().isoformat(),
    }
    map_played.extra = {**(map_played.extra or {}), EXTRA_KEY: summary}
    session.flush()
    for message in warnings[len(parsed.warnings) :]:
        log.warning("map %s: %s", map_played_id, message)
    return stored_rounds
