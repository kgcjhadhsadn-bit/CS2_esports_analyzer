"""Pure translation helpers: PandaScore vocabulary -> cs2bot domain values (no I/O, no DB)."""

from __future__ import annotations

import re

from cs2bot.domain.enums import MatchFormat, MatchStatus, RoundEndReason, Tier
from cs2bot.ingest.pandascore.models import PSGame, PSMatch
from cs2bot.utils.names import normalize_map_name

CS2_REGULATION_ROUNDS = 24  # MR12
CSGO_REGULATION_ROUNDS = 30  # MR15
CSGO_TITLE_SLUG = "cs-go"

_MATCH_STATUS: dict[str, MatchStatus] = {
    "not_started": MatchStatus.SCHEDULED,
    "running": MatchStatus.LIVE,
    "finished": MatchStatus.FINISHED,
    "canceled": MatchStatus.CANCELED,
    "cancelled": MatchStatus.CANCELED,
    "postponed": MatchStatus.POSTPONED,
}

# Game (= map) statuses. "not_played" is a map that will never be played (series already decided).
_GAME_STATUS: dict[str, MatchStatus] = {
    "not_started": MatchStatus.SCHEDULED,
    "running": MatchStatus.LIVE,
    "finished": MatchStatus.FINISHED,
    "not_played": MatchStatus.CANCELED,
}

_BEST_OF: dict[int, MatchFormat] = {
    1: MatchFormat.BO1,
    2: MatchFormat.BO2,
    3: MatchFormat.BO3,
    5: MatchFormat.BO5,
}
# first_to N = first team to win N maps
_FIRST_TO: dict[int, MatchFormat] = {1: MatchFormat.BO1, 2: MatchFormat.BO3, 3: MatchFormat.BO5}
# all_games_played = every map is played whatever the score (a draw is possible with 2)
_ALL_GAMES_PLAYED: dict[int, MatchFormat] = {1: MatchFormat.BO1, 2: MatchFormat.BO2}

_ROUND_OUTCOME: dict[str, RoundEndReason] = {
    "eliminated": RoundEndReason.ELIMINATION,
    "planted_eliminated": RoundEndReason.ELIMINATION,  # T side wiped out the CTs after the plant
    "exploded": RoundEndReason.BOMB_EXPLODED,
    "defused": RoundEndReason.BOMB_DEFUSED,
    "timeout": RoundEndReason.TIME_EXPIRED,
}

_PLACEHOLDER_MAPS = frozenset({"", "tba", "tbd", "tbc", "unknown", "default", "none", "na"})

_USD_NAMES = ("united states dollar", "us dollar", "usd")


def map_match_status(status: str | None) -> MatchStatus:
    """``not_started -> scheduled``, ``running -> live``, ...; anything else -> ``unknown``."""
    return _MATCH_STATUS.get((status or "").strip().lower(), MatchStatus.UNKNOWN)


def map_game_status(status: str | None) -> MatchStatus:
    return _GAME_STATUS.get((status or "").strip().lower(), MatchStatus.UNKNOWN)


def map_match_format(match_type: str | None, number_of_games: int | None) -> MatchFormat | None:
    """PandaScore ``match_type`` + ``number_of_games`` -> :class:`MatchFormat` (None = not a format we price).

    * ``best_of``: ``number_of_games`` is the maximum number of maps (1, 2, 3, 5).
    * ``first_to``: ``number_of_games`` is the number of map wins needed (2 -> BO3, 3 -> BO5).
    * ``all_games_played``: every map is played; two maps = BO2 (a 1-1 draw is possible).
    * ``custom`` and anything unknown -> None (the caller keeps whatever format it already has).
    """
    if number_of_games is None or number_of_games <= 0:
        return None
    kind = (match_type or "").strip().lower()
    if kind == "best_of":
        return _BEST_OF.get(number_of_games)
    if kind == "first_to":
        return _FIRST_TO.get(number_of_games)
    if kind == "all_games_played":
        return _ALL_GAMES_PLAYED.get(number_of_games)
    return None


def map_tier(tier: str | None) -> Tier:
    """``"s"`` / ``"A"`` / ... -> :class:`Tier`; missing or unknown -> ``Tier.UNRANKED``."""
    try:
        return Tier((tier or "").strip().lower())
    except ValueError:
        return Tier.UNRANKED


def map_is_lan(tournament_type: str | None) -> bool | None:
    """Tournament ``type``: ``offline`` -> True, ``online`` -> False, mixed / unknown -> None."""
    kind = (tournament_type or "").strip().lower()
    if kind == "offline":
        return True
    if kind == "online":
        return False
    return None


def map_round_outcome(outcome: str | None) -> RoundEndReason:
    return _ROUND_OUTCOME.get((outcome or "").strip().lower(), RoundEndReason.OTHER)


def game_map_name(game: PSGame) -> str | None:
    """Normalised map name of a game ("mirage"), or None while PandaScore does not know the map."""
    if game.map is None:
        return None
    for raw in (game.map.name, game.map.slug):
        if not raw:
            continue
        name = normalize_map_name(raw)
        if name and name not in _PLACEHOLDER_MAPS:
            return name[:32]
    return None


def parse_prizepool_usd(prizepool: str | None) -> int | None:
    """``"5000 United States Dollar"`` -> 5000. Other currencies -> None (no FX conversion here)."""
    if not prizepool:
        return None
    text = prizepool.strip().lower()
    if not any(name in text for name in _USD_NAMES) and "$" not in text:
        return None
    match = re.search(r"\d[\d\s,.]*", text)
    if match is None:
        return None
    digits = re.sub(r"[\s,]", "", match.group(0))
    digits = digits.split(".")[0] if re.fullmatch(r"\d+\.\d{1,2}", digits) else digits.replace(".", "")
    try:
        value = int(digits)
    except ValueError:
        return None
    return value if 0 < value < 2_000_000_000 else None


def stage_label(tournament_name: str | None, match_name: str | None) -> str | None:
    """Human stage label from the tournament stage and the match name prefix.

    PandaScore names matches ``"Quarterfinal match 2: Liquid vs MOUZ"`` (or just ``"NAVI vs G2"``) and
    the stage lives in the tournament name (``"Playoffs"``, ``"Group A"``). Result: ``"Playoffs -
    Quarterfinal match 2"``, ``"Group A"`` or None.
    """
    parts: list[str] = []
    if tournament_name and tournament_name.strip():
        parts.append(tournament_name.strip())
    if match_name and ":" in match_name:
        prefix = match_name.split(":", 1)[0].strip()
        known = {p.lower() for p in parts}
        if prefix and " vs " not in f" {prefix.lower()} " and prefix.lower() not in known:
            parts.append(prefix)
    return " - ".join(parts)[:128] if parts else None


def tournament_display_name(league: str | None, serie: str | None, stage: str | None) -> str:
    """League + serie + stage, e.g. ``"ESL Pro League Season 16 2022 - Playoffs"``."""
    head = " ".join(p.strip() for p in (league, serie) if p and p.strip())
    tail = (stage or "").strip()
    if head and tail:
        return f"{head} - {tail}"[:256]
    return (head or tail or "Unknown tournament")[:256]


def regulation_rounds(match: PSMatch | None) -> int:
    """24 for CS2 (MR12); 30 when the payload explicitly says the match is CS:GO (MR15)."""
    title = match.videogame_title.slug if match is not None and match.videogame_title is not None else None
    return CSGO_REGULATION_ROUNDS if title == CSGO_TITLE_SLUG else CS2_REGULATION_ROUNDS


def overtime_rounds(
    rounds_a: int | None, rounds_b: int | None, regulation: int = CS2_REGULATION_ROUNDS
) -> int:
    """Rounds played beyond regulation (0 when the map ended in regulation or scores are unknown)."""
    if rounds_a is None or rounds_b is None:
        return 0
    return max(0, rounds_a + rounds_b - regulation)
