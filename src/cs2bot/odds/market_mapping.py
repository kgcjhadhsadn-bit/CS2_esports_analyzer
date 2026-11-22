"""Map Betclic markets/selections onto the internal taxonomy (:mod:`cs2bot.domain.markets`).

Rules live in ``config/betclic_markets.yaml`` (ordered regexes over *folded* market names, see the
file header for the full syntax). :meth:`MarketMapper.map` returns a
:class:`~cs2bot.domain.odds.MappedSelection` in **our** orientation or ``None`` (stored unmapped,
never bet).

Orientation
-----------
A selection is first read in the *bookmaker's* orientation: team selections are identified by
fuzzy-matching the selection text against the event's ``team1_raw`` / ``team2_raw`` (or the
literal ``"1"`` / ``"2"``) and become bookmaker slot 1 or 2. Scores are read with the bookmaker's
first team first. If the event is swapped (bookmaker team1 == our team2) slots and scores are
flipped. Team-specific markets (``team=...``) are flipped the same way.

Handicaps (documented in :mod:`cs2bot.domain.markets`): ``spec.line`` is **our team1's**
handicap. Internally the line is first normalised to the bookmaker's first team (``L``), then:

* not swapped: ``spec.line = L``; swapped: ``spec.line = -L``;
* the selected team's own handicap is ``spec.line`` for team1 and ``-spec.line`` for team2.

So ``"Team2 -1.5"`` (our team2, not swapped) -> ``L = +1.5`` -> spec line ``+1.5`` with
selection ``team2``; ``"Team1 -1.5"`` -> spec line ``-1.5`` with selection ``team1``. A line given
only in the market name (``"Handicap map (-1,5)"``) is the bookmaker's first team's handicap.

Players
-------
Player names are resolved to internal player ids with an injected :data:`PlayerResolver`
(``nickname -> player id`` among the match lineups, see :func:`lineup_player_resolver`). Unknown
players -> unmapped. Head-to-head specs store the two ids sorted ascending (``player_id <
opponent_player_id``) so the key does not depend on the bookmaker's order.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast, get_args

import yaml
from rapidfuzz import fuzz
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from cs2bot.db.models import BookmakerMarket, BookmakerSelection, Match, MatchLineup, Player
from cs2bot.domain.markets import (
    SELECTION_DRAW,
    SELECTION_EVEN,
    SELECTION_NO,
    SELECTION_ODD,
    SELECTION_OVER,
    SELECTION_TEAM1,
    SELECTION_TEAM2,
    SELECTION_UNDER,
    SELECTION_YES,
    MarketSpec,
    MarketType,
    player_selection,
    score_selection,
)
from cs2bot.domain.odds import CollectedEvent, MappedSelection
from cs2bot.odds.text import fold_text, parse_decimal
from cs2bot.utils.names import (
    normalize_player_name,
    normalize_team_name,
    normalize_text,
    team_name_similarity,
)
from cs2bot.utils.paths import resolve_path

logger = logging.getLogger(__name__)

#: ``nickname -> internal player id`` (``None`` = unknown player -> selection stays unmapped).
PlayerResolver = Callable[[str], int | None]

SelectionKind = Literal[
    "team",
    "team_draw",
    "team_line",
    "over_under",
    "yes_no",
    "odd_even",
    "map_score",
    "round_score",
    "player",
    "player_draw",
    "player_over_under",
]
TeamInMarket = Literal["no", "required", "optional"]

_EXTRACTORS = frozenset({"map", "maps", "half", "player", "players"})
_REQUIREMENTS = frozenset({"map", "line", "team", "player", "players", "half", "round"})
_PARAM_ATTRS = {"map": "map_number", "half": "half", "round": "round_number", "line": "line"}
_HANDICAP_TYPES = frozenset({MarketType.MAP_HANDICAP, MarketType.ROUND_HANDICAP})

# --- numbers inside folded labels (never glued to letters: "s1mple", "9ine" are names) -------------
_PAREN_NUM_RE = re.compile(r"\(\s*([+-]?\d+(?:[.,]\d+)?)\s*\)")
_SIGNED_NUM_RE = re.compile(r"(?<![a-z0-9.,])([+-]\d+(?:[.,]\d+)?|\d+[.,]\d+)(?![a-z0-9]|[.,]\d)")
_ANY_NUM_RE = re.compile(r"(?<![a-z0-9.,])([+-]?\d+(?:[.,]\d+)?)(?![a-z0-9]|[.,]\d)")
_HALF_LINE_RE = re.compile(r"(?<![a-z0-9.,])([+-]?\d+[.,]5)(?![0-9])")
_SCORE_RE = re.compile(r"(?<![\d.,])(\d{1,2})\s*[:\-]\s*(\d{1,2})(?![\d.,])")

# --- map / half references ----------------------------------------------------------------------
# "mapy 1-2", "mapy 1 i 2", "maps 1, 2 and 3" - but not "mapa 1 - 2. polowa" (an ordinal follows)
_MAPS_SCOPE_RE = re.compile(
    r"\bmap[a-z]*\s*(?:nr\.?\s*)?(\d)\s*(-|do|to|i|&|and|,)\s*(\d)(?:\s*(?:,|i|&|and)\s*(\d))?(?![\d]|[.,]\d|\s*\.)"
)
_MAP_NUM_RES = (
    re.compile(r"\bmap[a-z]*\s*(?:nr\.?\s*|#\s*)?(\d)(?![\d]|[.,]\d)"),
    re.compile(r"(?<![\d.,])(\d)\s*\.?\s*map[a-z]*\b"),
    re.compile(r"\b(\d)(?:st|nd|rd|th)\s+map\b"),
)
_ORDINALS = {
    "pierwsz": 1,
    "drug": 2,
    "trzeci": 3,
    "czwart": 4,
    "piat": 5,
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
}
_MAP_ORDINAL_RE = re.compile(
    r"\b(pierwsz|drug|trzeci|czwart|piat|first|second|third|fourth|fifth)[a-z]*\s+map"
)
_HALF_RES = (
    re.compile(r"(?<![\d.,])([12])\s*\.?\s*polow"),
    re.compile(r"\b([12])(?:st|nd)\s+half\b"),
)
_HALF_ORDINAL_RE = re.compile(r"\b(pierwsz|drug|first|second)[a-z]*\s+(?:polow|half)")
_MAP_REF_ANY_RE = re.compile(r"\bmap[a-z]*\s*(?:nr\.?\s*)?\d|(?<![\d.,])\d\s*\.?\s*map[a-z]*\b|\bmap[a-z]*$")
_SEGMENT_SPLIT_RE = re.compile(r"\s+-\s+|:|\(|\)|\||/|,|;")
# "Natus Vincere / Remis", "NAVI i Tak", "NAVI & Powyżej 2,5": combined outcomes, never a plain team
_COMBO_RE = re.compile(r"\s(?:i|lub|oraz|or|and)\s|[/&]")
_PAIR_SPLIT_RE = re.compile(r"\s+(?:vs\.?|v\.?|czy|kontra|-)\s+")
_TEAM_TAG_RE = re.compile(r"\s*[\(\[][^)\]]*[\)\]]\s*")

DEFAULT_WORDS: dict[str, tuple[str, ...]] = {
    "over": ("powyzej", "over", "wiecej niz", "ponad"),
    "under": ("ponizej", "under", "mniej niz"),
    "yes": ("tak", "yes"),
    "no": ("nie", "no"),
    "odd": ("nieparzysta", "nieparzyste", "nieparzysty", "nieparzystych", "odd"),
    "even": ("parzysta", "parzyste", "parzysty", "parzystych", "even"),
    "draw": ("x", "remis", "draw", "tie"),
}


def _words_re(words: Iterable[str]) -> re.Pattern[str]:
    alternatives = sorted({fold_text(w) for w in words if w and str(w).strip()}, key=len, reverse=True)
    if not alternatives:
        return re.compile(r"(?!x)x")  # matches nothing
    body = "|".join(re.escape(a).replace(r"\ ", r"\s+") for a in alternatives)
    return re.compile(rf"(?<![a-z0-9])(?:{body})(?![a-z0-9])")


def _strip_punct(text: str) -> str:
    return text.strip(" \t-:;,.()[]!?\"'")


def _clean_name(text: str) -> str:
    return " ".join(_strip_punct(_TEAM_TAG_RE.sub(" ", text)).split())


def _norm_line(value: Decimal) -> float:
    as_float = float(value)
    return 0.0 if as_float == 0 else as_float


@dataclass(frozen=True)
class Vocabulary:
    """Selection words (folded) and fuzzy-match thresholds."""

    over: re.Pattern[str]
    under: re.Pattern[str]
    yes: re.Pattern[str]
    no: re.Pattern[str]
    odd: re.Pattern[str]
    even: re.Pattern[str]
    draw: frozenset[str]
    team_threshold: float = 72.0
    team_margin: float = 6.0
    player_threshold: float = 90.0

    @classmethod
    def build(
        cls,
        words: Mapping[str, Sequence[str]] | None = None,
        *,
        team_threshold: float = 72.0,
        team_margin: float = 6.0,
        player_threshold: float = 90.0,
    ) -> Vocabulary:
        merged: dict[str, tuple[str, ...]] = dict(DEFAULT_WORDS)
        for key, values in (words or {}).items():
            merged[str(key).lower()] = tuple(str(v) for v in values)
        return cls(
            over=_words_re(merged["over"]),
            under=_words_re(merged["under"]),
            yes=_words_re(merged["yes"]),
            no=_words_re(merged["no"]),
            odd=_words_re(merged["odd"]),
            even=_words_re(merged["even"]),
            draw=frozenset(fold_text(w) for w in merged["draw"]),
            team_threshold=team_threshold,
            team_margin=team_margin,
            player_threshold=player_threshold,
        )

    def is_draw(self, folded: str) -> bool:
        return _strip_punct(folded) in self.draw


@dataclass(frozen=True)
class MappingRule:
    id: str
    market_type: MarketType
    patterns: tuple[re.Pattern[str], ...]
    selection: SelectionKind
    exclude: tuple[re.Pattern[str], ...] = ()
    extract: frozenset[str] = frozenset()
    requires: frozenset[str] = frozenset()
    defaults: Mapping[str, int] = field(default_factory=dict)
    allowed: Mapping[str, frozenset[int]] = field(default_factory=dict)
    team_in_market: TeamInMarket = "no"
    segment_stopwords: tuple[re.Pattern[str], ...] = ()

    def market_matches(self, folded_market: str) -> re.Match[str] | None:
        if any(x.search(folded_market) for x in self.exclude):
            return None
        for pattern in self.patterns:
            m = pattern.search(folded_market)
            if m is not None:
                return m
        return None


@dataclass
class _Raw:
    """A mapping in the BOOKMAKER's orientation (before swap handling and player resolution)."""

    market_type: MarketType
    map_number: int | None = None
    maps_scope: tuple[int, ...] | None = None
    line: Decimal | None = None  # handicaps: bookmaker team1's handicap
    team_slot: int | None = None
    half: int | None = None
    round_number: int | None = None
    player: str | None = None
    opponent: str | None = None
    sel: str | None = None  # slot1 / slot2 / draw / over / under / yes / no / odd / even / score / player
    score: tuple[int, int] | None = None  # bookmaker team1 first
    sel_player: str | None = None

    def signature(self) -> str:
        players = sorted(p for p in (self.player, self.opponent) if p)
        parts = [
            self.market_type.value,
            f"map={self.map_number}",
            "maps=" + ",".join(str(m) for m in self.maps_scope or ()),
            f"line={'' if self.line is None else format(self.line.normalize(), 'f')}",
            f"team={self.team_slot}",
            f"half={self.half}",
            f"round={self.round_number}",
            "players=" + ",".join(normalize_player_name(p) for p in players),
        ]
        return "|".join(parts)


# --------------------------------------------------------------------------- extractors


def extract_map_number(folded: str) -> int | None:
    """``"mapa 1"`` / ``"1. mapa"`` / ``"mapie 2"`` / ``"1st map"`` / ``"pierwsza mapa"`` -> 1-based map."""
    for regex in _MAP_NUM_RES:
        m = regex.search(folded)
        if m:
            number = int(m.group(1))
            return number if 1 <= number <= 7 else None
    m = _MAP_ORDINAL_RE.search(folded)
    return _ORDINALS[m.group(1)] if m else None


def extract_maps_scope(folded: str) -> tuple[int, ...] | None:
    """``"mapy 1-2"`` -> (1, 2); ``"mapy 1, 2 i 3"`` -> (1, 2, 3); ``None`` if not a multi-map scope."""
    m = _MAPS_SCOPE_RE.search(folded)
    if not m:
        return None
    first, sep, second, third = m.group(1), m.group(2), m.group(3), m.group(4)
    a, b = int(first), int(second)
    if sep in ("-", "do", "to") and third is None:
        if a >= b:
            return None
        return tuple(range(a, b + 1))
    values = [a, b] + ([int(third)] if third else [])
    if len(set(values)) != len(values) or any(not 1 <= v <= 7 for v in values):
        return None
    return tuple(sorted(values))


def extract_half(folded: str) -> int | None:
    for regex in _HALF_RES:
        m = regex.search(folded)
        if m:
            return int(m.group(1))
    m = _HALF_ORDINAL_RE.search(folded)
    return _ORDINALS[m.group(1)] if m else None


def extract_market_line(folded: str) -> Decimal | None:
    """Line given in a market name: a number in parentheses or an ``x,5`` number."""
    m = _PAREN_NUM_RE.search(folded) or _HALF_LINE_RE.search(folded)
    return parse_decimal(m.group(1)) if m else None


def _pistol_round(value: str) -> int | None:
    low = value.lower()
    if low in ("1", "1st", "first") or low.startswith("pierwsz"):
        return 1
    if low in ("2", "2nd", "second") or low.startswith("drug"):
        return 13
    return None


# --------------------------------------------------------------------------- the mapper


class MarketMapper:
    """Ordered rule set mapping (market name, selection name) -> :class:`MappedSelection`."""

    def __init__(self, rules: Sequence[MappingRule], vocabulary: Vocabulary | None = None) -> None:
        self.rules = list(rules)
        self.vocabulary = vocabulary or Vocabulary.build()

    # ---------------------------------------------------------------- construction
    @classmethod
    def from_yaml(cls, path: str | Path) -> MarketMapper:
        """Load rules from YAML (relative paths: CWD first, then the project root)."""
        resolved = resolve_path(path)
        with resolved.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, Mapping):
            raise ValueError(f"{resolved}: top level must be a mapping")
        return cls.from_dict(data, source=str(resolved))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, source: str = "<dict>") -> MarketMapper:
        settings = data.get("settings") or {}
        vocabulary = Vocabulary.build(
            settings.get("words"),
            team_threshold=float(settings.get("team_match_threshold", 72)),
            team_margin=float(settings.get("team_match_margin", 6)),
            player_threshold=float(settings.get("player_match_threshold", 90)),
        )
        raw_rules = data.get("rules")
        if not isinstance(raw_rules, list) or not raw_rules:
            raise ValueError(f"{source}: 'rules' must be a non-empty list")
        rules: list[MappingRule] = []
        seen: set[str] = set()
        for index, entry in enumerate(raw_rules):
            rule = _parse_rule(entry, index, source)
            if rule.id in seen:
                raise ValueError(f"{source}: duplicate rule id {rule.id!r}")
            seen.add(rule.id)
            rules.append(rule)
        return cls(rules, vocabulary)

    # ---------------------------------------------------------------- public API
    def match_rule(self, market_raw: str) -> str | None:
        """Id of the first rule that can apply to this market name (diagnostics; ignores selections).

        A rule whose patterns match but that requires a map number the name does not contain
        is skipped, mirroring what :meth:`map` does.
        """
        folded = fold_text(market_raw)
        for rule in self.rules:
            m = rule.market_matches(folded)
            if m is None:
                continue
            if (
                "map" in rule.requires
                and m.groupdict().get("map") is None
                and extract_map_number(folded) is None
            ):
                continue
            return rule.id
        return None

    def map(
        self,
        market_raw: str,
        selection_raw: str,
        *,
        event: CollectedEvent,
        swapped: bool,
        player_resolver: PlayerResolver | None = None,
        sibling_selections: Sequence[str] | None = None,
    ) -> MappedSelection | None:
        """Map one selection; ``None`` = unknown market/selection (stored unmapped, never bet).

        ``event`` supplies the bookmaker team names; ``swapped`` is
        ``bookmaker_events.teams_swapped``; ``sibling_selections`` (all selection names of the
        market) lets head-to-head markets find the opponent when the market name omits it.
        """
        try:
            raw = self._match_raw(market_raw, selection_raw, event, sibling_selections)
            if raw is None:
                return None
            return self._orient(raw, swapped, player_resolver)
        except Exception:  # defensive: a weird label must never break a collection run
            logger.debug("mapping failed for %r / %r", market_raw, selection_raw, exc_info=True)
            return None

    def group_key(
        self,
        market_raw: str,
        selection_raw: str,
        *,
        event: CollectedEvent,
        sibling_selections: Sequence[str] | None = None,
    ) -> str | None:
        """Swap- and lineup-independent signature of the spec a selection belongs to.

        Selections of one bookmaker market with different signatures (e.g. a card listing
        several lines) must be stored as separate markets because a market row has one spec.
        """
        try:
            raw = self._match_raw(market_raw, selection_raw, event, sibling_selections)
        except Exception:
            return None
        return raw.signature() if raw is not None else None

    # ---------------------------------------------------------------- matching
    def _match_raw(
        self,
        market_raw: str,
        selection_raw: str,
        event: CollectedEvent,
        siblings: Sequence[str] | None,
    ) -> _Raw | None:
        folded_market = fold_text(market_raw)
        folded_selection = fold_text(selection_raw)
        if not folded_market or not folded_selection:
            return None
        for rule in self.rules:
            m = rule.market_matches(folded_market)
            if m is None:
                continue
            raw = self._apply(rule, m, folded_market, folded_selection, event, siblings)
            if raw is not None:
                return raw
        return None

    def _apply(
        self,
        rule: MappingRule,
        m: re.Match[str],
        fm: str,
        fs: str,
        event: CollectedEvent,
        siblings: Sequence[str] | None,
    ) -> _Raw | None:
        raw = _Raw(rule.market_type)
        groups = {k: v for k, v in m.groupdict().items() if v is not None}
        # ---- maps
        if "map" in groups:
            raw.map_number = int(groups["map"])
        if "map_from" in groups and "map_to" in groups:
            lo, hi = int(groups["map_from"]), int(groups["map_to"])
            if lo < hi:
                raw.maps_scope = tuple(range(lo, hi + 1))
        if raw.map_number is None and raw.maps_scope is None:
            scope = extract_maps_scope(fm) if "maps" in rule.extract else None
            if scope is not None:
                raw.maps_scope = scope
            elif "map" in rule.extract or "maps" in rule.extract:
                raw.map_number = extract_map_number(fm)
        # ---- half / round / race-to
        if "half" in groups:
            raw.half = int(groups["half"]) if groups["half"].isdigit() else _ORDINALS.get(groups["half"])
        elif "half" in rule.extract:
            raw.half = extract_half(fm)
        if "round" in groups:
            raw.round_number = int(groups["round"])
        if "pistol" in groups:
            raw.round_number = _pistol_round(groups["pistol"])
        if "n" in groups:
            raw.line = Decimal(groups["n"])
        market_line = parse_decimal(groups["line"]) if "line" in groups else extract_market_line(fm)
        for key, value in rule.defaults.items():
            attr = _PARAM_ATTRS.get(key)
            if attr and getattr(raw, attr) is None:
                setattr(raw, attr, Decimal(value) if attr == "line" else int(value))
        for key, allowed in rule.allowed.items():
            attr = _PARAM_ATTRS.get(key)
            current: Any = getattr(raw, attr) if attr else None
            if current is not None and int(current) not in allowed:
                return None
        # ---- team named in the market
        if "team" in groups:
            slot = self._team_slot(groups["team"], event)
            if slot is None:
                return None
            raw.team_slot = slot
        elif rule.team_in_market != "no":
            raw.team_slot = self._team_in_text(fm, event)
            if raw.team_slot is None and rule.team_in_market == "required":
                return None
        # ---- players named in the market
        if "player" in groups:
            raw.player = _clean_name(groups["player"]) or None
        elif "player" in rule.extract:
            raw.player = self._segment_player(fm, rule)
        if "player2" in groups and raw.player:
            raw.opponent = _clean_name(groups["player2"]) or None
        elif "players" in rule.extract:
            pair = self._segment_pair(fm, rule)
            if pair is not None:
                raw.player, raw.opponent = pair
        # ---- selection
        if not self._parse_selection(rule, raw, fs, event, market_line, siblings):
            return None
        # ---- requirements
        checks = {
            "map": raw.map_number is not None,
            "line": raw.line is not None,
            "team": raw.team_slot is not None,
            "player": raw.player is not None,
            "players": raw.player is not None and raw.opponent is not None,
            "half": raw.half is not None,
            "round": raw.round_number is not None,
        }
        if not all(checks[req] for req in rule.requires):
            return None
        return raw

    def _parse_selection(
        self,
        rule: MappingRule,
        raw: _Raw,
        fs: str,
        event: CollectedEvent,
        market_line: Decimal | None,
        siblings: Sequence[str] | None,
    ) -> bool:
        kind = rule.selection
        vocab = self.vocabulary
        if kind in ("team", "team_draw", "player", "player_draw") and self._is_compound(fs):
            return False  # a line / score / combined outcome would otherwise be silently dropped
        if kind in ("team", "team_draw"):
            if kind == "team_draw" and vocab.is_draw(fs):
                raw.sel = "draw"
                return True
            slot = self._team_slot(fs, event)
            if slot is None:
                return False
            raw.sel = f"slot{slot}"
            return True
        if kind == "team_line":
            return self._parse_team_line(raw, fs, event, market_line)
        if kind in ("over_under", "player_over_under"):
            found = self._over_under(fs)
            if found is None:
                return False
            word, line, before, after = found
            raw.sel = word
            if line is None:
                line = market_line
            if line is None or line <= 0:
                return False
            raw.line = line
            prefix = _clean_name(before)
            if kind == "player_over_under":
                name = prefix or _clean_name(_ANY_NUM_RE.sub(" ", after))
                if name:
                    raw.player = name
            elif prefix and raw.market_type is MarketType.TEAM_TOTAL_ROUNDS and raw.team_slot is None:
                raw.team_slot = self._team_slot(prefix, event)
            return True
        if kind == "yes_no":
            yes, no = bool(vocab.yes.search(fs)), bool(vocab.no.search(fs))
            if yes == no:
                return False
            raw.sel = "yes" if yes else "no"
            return True
        if kind == "odd_even":
            if vocab.odd.search(fs):
                raw.sel = "odd"
            elif vocab.even.search(fs):
                raw.sel = "even"
            else:
                return False
            return True
        if kind in ("map_score", "round_score"):
            return self._parse_score(raw, fs, event, rounds=kind == "round_score")
        if kind in ("player", "player_draw"):
            if kind == "player_draw" and vocab.is_draw(fs):
                raw.sel = "draw"
                return self._fill_pair_from_siblings(raw, siblings)
            name = _clean_name(fs)
            if not name or name.isdigit():
                return False
            raw.sel = "player"
            raw.sel_player = name
            if raw.market_type is MarketType.PLAYER_KILLS_H2H:
                return self._fill_pair_from_siblings(raw, siblings)
            return True
        return False

    def _fill_pair_from_siblings(self, raw: _Raw, siblings: Sequence[str] | None) -> bool:
        if raw.player is not None and raw.opponent is not None:
            return True
        names = [
            _clean_name(fold_text(s))
            for s in siblings or ()
            if s and not self.vocabulary.is_draw(fold_text(s)) and _clean_name(fold_text(s))
        ]
        unique = list(dict.fromkeys(names))
        if len(unique) != 2:
            return False
        raw.player, raw.opponent = unique[0], unique[1]
        return True

    def _is_compound(self, fs: str) -> bool:
        """Selection text carrying a line, a score, a yes/no/over/under word or a combination."""
        vocab = self.vocabulary
        return bool(
            _PAREN_NUM_RE.search(fs)
            or _SIGNED_NUM_RE.search(fs)
            or _SCORE_RE.search(fs)
            or _COMBO_RE.search(fs)
            or vocab.over.search(fs)
            or vocab.under.search(fs)
            or vocab.yes.search(fs)
            or vocab.no.search(fs)
        )

    def _parse_team_line(
        self, raw: _Raw, fs: str, event: CollectedEvent, market_line: Decimal | None
    ) -> bool:
        if _SCORE_RE.search(fs) or _COMBO_RE.search(fs):
            return False  # "NAVI 2:0" / "NAVI / Remis" are not handicap selections
        m = _PAREN_NUM_RE.search(fs) or _SIGNED_NUM_RE.search(fs)
        handicap = parse_decimal(m.group(1)) if m else None
        text = f"{fs[: m.start()]} {fs[m.end() :]}" if m else fs
        slot = self._team_slot(_clean_name(text), event)
        if slot is None:
            return False
        if handicap is None:
            if market_line is None:
                return False
            line_bm1 = market_line  # a line in the market name is the bookmaker team1's handicap
        else:
            line_bm1 = handicap if slot == 1 else -handicap
        raw.line = line_bm1
        raw.sel = f"slot{slot}"
        return True

    def _parse_score(self, raw: _Raw, fs: str, event: CollectedEvent, *, rounds: bool) -> bool:
        m = _SCORE_RE.search(fs)
        if not m:
            return False
        a, b = int(m.group(1)), int(m.group(2))
        before = _clean_name(fs[: m.start()])
        if before:
            slot = self._team_slot(before, event)
            if slot is None:
                return False
            if slot == 2:  # "<team2> 2:1" = team2 won 2-1 -> bookmaker order 1-2
                a, b = b, a
        if rounds:
            if max(a, b) < 13 or max(a, b) > 60 or a == b:
                return False
        elif max(a, b) > 5 or max(a, b) < 1:
            return False
        raw.sel = "score"
        raw.score = (a, b)
        return True

    def _over_under(self, fs: str) -> tuple[str, Decimal | None, str, str] | None:
        over = self.vocabulary.over.search(fs)
        under = self.vocabulary.under.search(fs)
        if (over is None) == (under is None):
            return None
        hit = over or under
        assert hit is not None
        word = SELECTION_OVER if over is not None else SELECTION_UNDER
        before, after = fs[: hit.start()], fs[hit.end() :]
        m = _ANY_NUM_RE.search(after)
        if m is None:
            m_before = list(_ANY_NUM_RE.finditer(before))
            line = parse_decimal(m_before[-1].group(1)) if m_before else None
            if m_before:
                before = before[: m_before[-1].start()]
        else:
            line = parse_decimal(m.group(1))
        if line is not None:
            line = abs(line)
        return word, line, before, after

    # ---------------------------------------------------------------- team / player helpers
    def _team_slot(self, text: str, event: CollectedEvent) -> int | None:
        """Bookmaker slot (1/2) a selection text refers to, or ``None`` when unclear."""
        token = _strip_punct(text)
        if token in ("1", "2"):
            return int(token)
        name = _clean_name(text)
        if not name:
            return None
        t, n1, n2 = (
            normalize_team_name(name),
            normalize_team_name(event.team1_raw),
            normalize_team_name(event.team2_raw),
        )
        if not t:
            return None
        if t == n1 and t != n2:
            return 1
        if t == n2 and t != n1:
            return 2
        s1, s2 = team_name_similarity(name, event.team1_raw), team_name_similarity(name, event.team2_raw)
        r1, r2 = float(fuzz.ratio(t, n1)), float(fuzz.ratio(t, n2))
        best, s_best, s_other, r_best, r_other = (
            (1, s1, s2, r1, r2) if (s1, r1) >= (s2, r2) else (2, s2, s1, r2, r1)
        )
        if s_best < self.vocabulary.team_threshold:
            return None
        if s_best - s_other >= self.vocabulary.team_margin or r_best - r_other >= self.vocabulary.team_margin:
            return best
        return None

    def _team_in_text(self, folded: str, event: CollectedEvent) -> int | None:
        """Slot of the single event team whose name appears in a (folded) market name."""
        found: set[int] = set()
        for slot, team in ((1, event.team1_raw), (2, event.team2_raw)):
            variants = {fold_text(team), normalize_team_name(team), normalize_text(team)}
            for variant in variants:
                if len(variant) >= 2 and re.search(rf"(?<![a-z0-9]){re.escape(variant)}(?![a-z0-9])", folded):
                    found.add(slot)
                    break
        return found.pop() if len(found) == 1 else None

    def _segments(self, folded: str, rule: MappingRule) -> list[str]:
        out: list[str] = []
        for seg in _SEGMENT_SPLIT_RE.split(folded):
            seg = _strip_punct(seg)
            if not seg or _MAP_REF_ANY_RE.search(seg) or extract_maps_scope(seg):
                continue
            if any(sw.search(seg) for sw in rule.segment_stopwords):
                continue
            out.append(seg)
        return out

    def _segment_player(self, folded: str, rule: MappingRule) -> str | None:
        segments = self._segments(folded, rule)
        return _clean_name(segments[0]) if len(segments) == 1 else None

    def _segment_pair(self, folded: str, rule: MappingRule) -> tuple[str, str] | None:
        segments = self._segments(folded, rule)
        for seg in segments:
            parts = [_clean_name(p) for p in _PAIR_SPLIT_RE.split(seg)]
            parts = [p for p in parts if p]
            if len(parts) == 2 and parts[0] != parts[1]:
                return parts[0], parts[1]
        # "s1mple - zywoo - zabojstwa": the pair was split on " - " into two segments
        names = [_clean_name(s) for s in segments if _clean_name(s)]
        if len(names) == 2 and names[0] != names[1]:
            return names[0], names[1]
        return None

    # ---------------------------------------------------------------- orientation
    def _orient(self, raw: _Raw, swapped: bool, resolver: PlayerResolver | None) -> MappedSelection | None:
        def ours(slot: int) -> str:
            return SELECTION_TEAM1 if (slot == 1) != swapped else SELECTION_TEAM2

        line = raw.line
        if line is not None and raw.market_type in _HANDICAP_TYPES and swapped:
            line = -line  # our team1 = bookmaker team2, whose handicap is -L
        team = ours(raw.team_slot) if raw.team_slot is not None else None
        player_id: int | None = None
        opponent_id: int | None = None
        if raw.player is not None:
            if resolver is None:
                return None
            player_id = resolver(raw.player)
            if player_id is None:
                return None
        if raw.opponent is not None:
            if resolver is None:
                return None
            opponent_id = resolver(raw.opponent)
            if opponent_id is None or opponent_id == player_id:
                return None
        if player_id is not None and opponent_id is not None and opponent_id < player_id:
            player_id, opponent_id = opponent_id, player_id
        sel = raw.sel
        if sel in ("slot1", "slot2"):
            key = ours(int(sel[-1]))
        elif sel == "draw":
            key = SELECTION_DRAW
        elif sel in (SELECTION_OVER, SELECTION_UNDER):
            key = sel
        elif sel == "yes":
            key = SELECTION_YES
        elif sel == "no":
            key = SELECTION_NO
        elif sel == "odd":
            key = SELECTION_ODD
        elif sel == "even":
            key = SELECTION_EVEN
        elif sel == "score" and raw.score is not None:
            a, b = raw.score
            key = score_selection(b, a) if swapped else score_selection(a, b)
        elif sel == "player" and raw.sel_player is not None:
            if resolver is None:
                return None
            selected = resolver(raw.sel_player)
            if selected is None:
                return None
            if raw.market_type is MarketType.PLAYER_KILLS_H2H and selected not in (player_id, opponent_id):
                return None
            key = player_selection(selected)
        else:
            return None
        spec = MarketSpec(
            market_type=raw.market_type,
            map_number=raw.map_number,
            maps_scope=raw.maps_scope,
            line=_norm_line(line) if line is not None else None,
            team=team,
            player_id=player_id,
            player_name=raw.player if raw.market_type is MarketType.PLAYER_KILLS else None,
            opponent_player_id=opponent_id,
            half=raw.half,
            round_number=raw.round_number,
        )
        return MappedSelection(spec=spec, selection_key=key)


# --------------------------------------------------------------------------- YAML parsing


def _flatten(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        return [values]
    out: list[str] = []
    if isinstance(values, Iterable):
        for item in values:
            out.extend(_flatten(item))
    return out


def _compile(patterns: Iterable[str], rule_id: str, source: str) -> tuple[re.Pattern[str], ...]:
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise ValueError(f"{source}: rule {rule_id!r}: bad regex {pattern!r}: {exc}") from exc
    return tuple(compiled)


def _parse_rule(entry: Any, index: int, source: str) -> MappingRule:
    if not isinstance(entry, Mapping):
        raise ValueError(f"{source}: rule #{index} must be a mapping")
    rule_id = str(entry.get("id") or f"rule_{index}")
    try:
        market_type = MarketType(str(entry["market_type"]))
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{source}: rule {rule_id!r}: unknown or missing market_type") from exc
    selection = str(entry.get("selection", ""))
    if selection not in get_args(SelectionKind):
        raise ValueError(f"{source}: rule {rule_id!r}: unknown selection kind {selection!r}")
    patterns = _compile(_flatten(entry.get("patterns")), rule_id, source)
    if not patterns:
        raise ValueError(f"{source}: rule {rule_id!r}: needs at least one pattern")
    extract = frozenset(_flatten(entry.get("extract")))
    if extract - _EXTRACTORS:
        raise ValueError(f"{source}: rule {rule_id!r}: unknown extractors {sorted(extract - _EXTRACTORS)}")
    requires = frozenset(_flatten(entry.get("requires")))
    if requires - _REQUIREMENTS:
        raise ValueError(
            f"{source}: rule {rule_id!r}: unknown requirements {sorted(requires - _REQUIREMENTS)}"
        )
    team_in_market = str(entry.get("team_in_market", "no"))
    if team_in_market not in get_args(TeamInMarket):
        raise ValueError(f"{source}: rule {rule_id!r}: team_in_market must be no/required/optional")
    defaults_raw = entry.get("defaults") or {}
    allowed_raw = entry.get("allowed") or {}
    if not isinstance(defaults_raw, Mapping) or not isinstance(allowed_raw, Mapping):
        raise ValueError(f"{source}: rule {rule_id!r}: defaults/allowed must be mappings")
    for key in [*defaults_raw, *allowed_raw]:
        if key not in _PARAM_ATTRS:
            raise ValueError(f"{source}: rule {rule_id!r}: unknown parameter {key!r}")
    return MappingRule(
        id=rule_id,
        market_type=market_type,
        patterns=patterns,
        selection=cast(SelectionKind, selection),
        exclude=_compile(_flatten(entry.get("exclude")), rule_id, source),
        extract=extract,
        requires=requires,
        defaults={str(k): int(v) for k, v in defaults_raw.items()},
        allowed={
            str(k): frozenset(int(v) for v in (vals if isinstance(vals, list) else [vals]))
            for k, vals in allowed_raw.items()
        },
        team_in_market=cast(TeamInMarket, team_in_market),
        segment_stopwords=_compile(_flatten(entry.get("segment_stopwords")), rule_id, source),
    )


def rules_fingerprint(mapper: MarketMapper) -> str:
    """Short hash of the rule set (stored with mappings for traceability)."""
    digest = hashlib.sha1(usedforsecurity=False)
    for rule in mapper.rules:
        digest.update(rule.id.encode())
        digest.update(rule.market_type.value.encode())
        for pattern in rule.patterns:
            digest.update(pattern.pattern.encode())
    return digest.hexdigest()[:12]


# --------------------------------------------------------------------------- DB helpers


def lineup_player_resolver(
    session: Session, match_id: int, *, fuzzy_threshold: float = 90.0
) -> PlayerResolver:
    """Resolver over the match lineups (falls back to both teams' current rosters).

    Exact normalised nickname first, then a whole-token containment ("NAVI s1mple"), then a
    unique fuzzy match above ``fuzzy_threshold``. Ambiguous names resolve to ``None``.
    """
    match = session.get(Match, match_id)
    players: list[Player] = []
    if match is not None:
        players = list(
            session.scalars(
                select(Player)
                .join(MatchLineup, MatchLineup.player_id == Player.id)
                .where(MatchLineup.match_id == match_id)
            )
        )
        if not players:
            team_ids = [t for t in (match.team1_id, match.team2_id) if t is not None]
            if team_ids:
                players = list(session.scalars(select(Player).where(Player.current_team_id.in_(team_ids))))
    index: dict[str, int | None] = {}
    for p in players:
        key = normalize_player_name(p.nickname)
        if not key:
            continue
        index[key] = None if key in index and index[key] != p.id else p.id
    return make_name_resolver(index, fuzzy_threshold=fuzzy_threshold)


def make_name_resolver(index: Mapping[str, int | None], *, fuzzy_threshold: float = 90.0) -> PlayerResolver:
    """Build a resolver from ``normalised nickname -> id`` (``None`` marks an ambiguous nickname)."""
    names = {k: v for k, v in index.items() if k}

    def resolve(name: str) -> int | None:
        key = normalize_player_name(name)
        if not key:
            return None
        if key in names:
            return names[key]
        tokens = {normalize_player_name(tok) for tok in normalize_text(name).split()}
        hits = {names[k] for k in names if k in tokens and names[k] is not None}
        if len(hits) == 1:
            return hits.pop()
        scored = sorted(
            ((float(fuzz.ratio(key, k)), v) for k, v in names.items() if v is not None), reverse=True
        )
        if (
            scored
            and scored[0][0] >= fuzzy_threshold
            and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 5)
        ):
            return scored[0][1]
        return None

    return resolve


def list_unmapped(session: Session, *, limit: int | None = None) -> list[tuple[str, int]]:
    """Raw market names that were never mapped, with how many market rows carry them (most first)."""
    mapped = func.sum(case((BookmakerMarket.is_mapped.is_(True), 1), else_=0))
    count = func.count(BookmakerMarket.id)
    query = (
        select(BookmakerMarket.raw_name, count)
        .group_by(BookmakerMarket.raw_name)
        .having(mapped == 0)
        .order_by(count.desc(), BookmakerMarket.raw_name)
    )
    if limit is not None:
        query = query.limit(limit)
    return [(name, int(n)) for name, n in session.execute(query).all()]


def list_unmapped_selections(session: Session, *, limit: int | None = 200) -> list[tuple[str, str, int]]:
    """(market name, selection name, count) of unmapped selections inside *mapped* markets."""
    count = func.count(BookmakerSelection.id)
    query = (
        select(BookmakerMarket.raw_name, BookmakerSelection.raw_name, count)
        .join(BookmakerMarket, BookmakerSelection.market_id == BookmakerMarket.id)
        .where(BookmakerMarket.is_mapped.is_(True), BookmakerSelection.is_mapped.is_(False))
        .group_by(BookmakerMarket.raw_name, BookmakerSelection.raw_name)
        .order_by(count.desc())
    )
    if limit is not None:
        query = query.limit(limit)
    return [(m, s, int(n)) for m, s, n in session.execute(query).all()]
