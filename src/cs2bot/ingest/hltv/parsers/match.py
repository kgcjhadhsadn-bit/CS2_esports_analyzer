"""Parser for the HLTV match overview page (``/matches/{id}/{slug}``).

Page facts relied on (see :mod:`cs2bot.ingest.hltv.selectors` for provenance):

* ``.team1-gradient`` is always the left team and ``.results-left`` on every map holder is team1;
* the first ``.veto-box`` holds "Best of N (LAN|Online)" plus "* stage" and "** note" lines, the
  second one holds the numbered veto lines ("1. X removed Y", "3. X picked Y", "7. Y was left over");
* map holders are in series order; unplayed maps carry ``.optional`` and a "-" score, forfeited maps
  are named "Default" and have no stats link;
* half scores are ``<span class="ct|t">`` pairs (team1 first); overtime groups carry no side class;
* lineups: two ``.lineup`` blocks (team1 first), players carry ``data-player-id``;
* demo downloads are ``/download/demo/{id}`` links (``href`` or ``data-demo-link``), sometimes one per map.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

from bs4 import BeautifulSoup, Tag

from cs2bot.domain.enums import Side, TeamSlot, VetoAction
from cs2bot.ingest.hltv.errors import HLTVParseError
from cs2bot.ingest.hltv.models import (
    BEST_OF_TO_FORMAT,
    DemoLink,
    EventRef,
    HalfScore,
    HLTVMatchStatus,
    LineupPlayerRow,
    MapResult,
    MapState,
    MatchPage,
    TeamRef,
    VetoEntry,
)
from cs2bot.ingest.hltv.parsers.common import (
    attr,
    classes,
    clean_text,
    has_class,
    id_from,
    make_soup,
    parse_int,
    resolve_slot,
    team_ref,
    text_of,
    unix_ms,
)
from cs2bot.ingest.hltv.selectors import (
    BEST_OF_RE,
    COUNTDOWN_STATUS,
    DEMO_ID_RE,
    EVENT_ID_RE,
    FORFEIT_MAP_NAME,
    FORFEIT_NOTE_RE,
    LINEUP_FULL_NAME_RE,
    LOCATION_RE,
    MAPSTATS_ID_RE,
    MATCH_ID_RE,
    PLAYER_ID_RE,
    RANK_RE,
    SCORE_PAIR_RE,
    STANDIN_RE,
    TEAM_ID_RE,
    VETO_LEFTOVER_RE,
    VETO_PICK_BAN_RE,
    MatchSel,
)
from cs2bot.ingest.hltv.urls import HLTV_BASE_URL, absolute_url, ranking_date_from_url, strip_fragment
from cs2bot.utils.names import normalize_map_name, normalize_text

logger = logging.getLogger(__name__)

# (team1 lineup, team2 lineup, team1 rank, team2 rank, ranking date of those ranks)
_Lineups = tuple[list[LineupPlayerRow], list[LineupPlayerRow], int | None, int | None, date | None]


def parse_match_page(html: str, url: str, *, base_url: str = HLTV_BASE_URL) -> MatchPage:
    """Parse a match page. Missing sections (no veto yet, TBA maps, forfeits) are tolerated.

    Raises :class:`HLTVParseError` only when the HTML is not a match page at all.
    """
    soup = make_soup(html)
    canonical = attr(soup.select_one(MatchSel.CANONICAL), "href")
    match_id = id_from(url, MATCH_ID_RE) or id_from(canonical, MATCH_ID_RE)
    if match_id is None:
        raise HLTVParseError(f"no HLTV match id in {url!r}")
    has_teams_box = (
        soup.select_one(".team1-gradient") is not None or soup.select_one(".team2-gradient") is not None
    )
    if not has_teams_box and soup.select_one(MatchSel.MAPHOLDER) is None:
        raise HLTVParseError(f"{url!r} does not look like an HLTV match page")

    warnings: list[str] = []
    team1, score1, won1 = _parse_team_box(soup, 1)
    team2, score2, won2 = _parse_team_box(soup, 2)
    scheduled_at = unix_ms(attr(soup.select_one(MatchSel.DATE), "data-unix")) or unix_ms(
        attr(soup.select_one(MatchSel.TIME), "data-unix")
    )
    event = _parse_event(soup)
    best_of, is_lan, stage, notes = _parse_format_box(soup)
    status, status_text = _parse_status(soup)

    no_maps: list[MapResult] = []
    maps = _section(warnings, "maps", lambda: _parse_maps(soup, base_url, status), no_maps)
    if (
        status is HLTVMatchStatus.SCHEDULED
        and not status_text
        and any(m.state is MapState.PLAYED for m in maps)
    ):
        status = HLTVMatchStatus.FINISHED  # no countdown element, but results are shown
    no_vetoes: list[VetoEntry] = []
    vetoes = _section(warnings, "veto", lambda: _parse_vetoes(soup, team1, team2, warnings), no_vetoes)
    _apply_veto_to_maps(maps, vetoes, best_of)
    no_lineups: _Lineups = ([], [], None, None, None)
    lineup1, lineup2, rank1, rank2, rank_date = _section(
        warnings, "lineups", lambda: _parse_lineups(soup, team1, team2, warnings), no_lineups
    )
    no_demos: list[DemoLink] = []
    demos = _section(warnings, "demos", lambda: _parse_demos(soup, base_url, maps), no_demos)

    forfeit_maps = [m for m in maps if m.state is MapState.FORFEIT]
    forfeit_note = next((n for n in notes if FORFEIT_NOTE_RE.search(n)), None)
    is_forfeit = bool(forfeit_maps) or forfeit_note is not None

    winner: TeamSlot | None = TeamSlot.TEAM1 if won1 else TeamSlot.TEAM2 if won2 else None
    if winner is None and score1 is not None and score2 is not None and score1 != score2:
        winner = TeamSlot.TEAM1 if score1 > score2 else TeamSlot.TEAM2
    if winner is None and is_forfeit and len(forfeit_maps) == 1 and forfeit_maps[0].winner is not None:
        winner = forfeit_maps[0].winner  # full forfeit: the "Default" map carries the result
    if score1 is None and score2 is None and status is HLTVMatchStatus.FINISHED:
        decided = [m for m in maps if m.state in (MapState.PLAYED, MapState.FORFEIT) and m.winner is not None]
        if decided:
            score1 = sum(1 for m in decided if m.winner is TeamSlot.TEAM1)
            score2 = sum(1 for m in decided if m.winner is TeamSlot.TEAM2)

    return MatchPage(
        hltv_match_id=match_id,
        url=strip_fragment(canonical or url),
        team1=team1,
        team2=team2,
        team1_rank=rank1,
        team2_rank=rank2,
        rank_date=rank_date,
        scheduled_at=scheduled_at,
        status=status,
        status_text=status_text,
        best_of=best_of,
        format=BEST_OF_TO_FORMAT.get(best_of) if best_of else None,
        is_lan=is_lan,
        event=event,
        stage=stage,
        notes=notes,
        team1_score=score1,
        team2_score=score2,
        winner=winner,
        maps=maps,
        vetoes=vetoes,
        team1_lineup=lineup1,
        team2_lineup=lineup2,
        demos=demos,
        is_forfeit=is_forfeit,
        forfeit_note=forfeit_note,
        warnings=warnings,
    )


def _section[T](warnings: list[str], name: str, fn: Callable[[], T], default: T) -> T:
    """Run one section parser; a broken section becomes a warning instead of failing the page."""
    try:
        return fn()
    except Exception as exc:
        logger.debug("HLTV match page: %s section failed: %s", name, exc, exc_info=True)
        warnings.append(f"{name} section could not be parsed: {type(exc).__name__}: {exc}")
        return default


# ---------------------------------------------------------------------------------------- header


def _parse_team_box(soup: BeautifulSoup, n: int) -> tuple[TeamRef | None, int | None, bool]:
    box = soup.select_one(MatchSel.TEAM_GRADIENT.format(n=n))
    if box is None:
        return None, None, False
    link = box.select_one(MatchSel.TEAM_LINK)
    name = text_of(box.select_one(MatchSel.TEAM_NAME)) or attr(box.select_one("img[title]"), "title")
    team = team_ref(name, id_from(attr(link, "href"), TEAM_ID_RE))
    score_node = box.select_one(MatchSel.TEAM_SCORE)
    score = parse_int(text_of(score_node))
    return team, score, has_class(score_node, "won")


def _parse_event(soup: BeautifulSoup) -> EventRef | None:
    link = soup.select_one(MatchSel.EVENT_LINK)
    if link is None:
        return None
    name = text_of(link) or attr(link, "title") or ""
    event_id = id_from(attr(link, "href"), EVENT_ID_RE)
    if not name and event_id is None:
        return None
    return EventRef(name=clean_text(name), hltv_id=event_id)


def _parse_format_box(soup: BeautifulSoup) -> tuple[int | None, bool | None, str | None, list[str]]:
    node = soup.select_one(f"{MatchSel.VETO_BOX} {MatchSel.PREFORMATTED}") or soup.select_one(
        MatchSel.PREFORMATTED
    )
    if node is None:
        return None, None, None, []
    lines = [clean_text(line) for line in node.get_text("\n").splitlines()]
    lines = [line for line in lines if line]
    best_of: int | None = None
    is_lan: bool | None = None
    stage: str | None = None
    notes: list[str] = []
    for line in lines:
        bo = BEST_OF_RE.search(line)
        if bo and best_of is None:
            best_of = int(bo.group(1))
            loc = LOCATION_RE.search(line)
            if loc:
                is_lan = loc.group(1).lower() == "lan"
            continue
        if line.startswith("**"):
            notes.append(line.lstrip("*").strip())
        elif line.startswith("*"):
            text = line.lstrip("*").strip()
            if stage is None:
                stage = text
            else:
                notes.append(text)
        elif not VETO_PICK_BAN_RE.match(line) and not VETO_LEFTOVER_RE.match(line):
            notes.append(line)
    return best_of, is_lan, stage, notes


def _parse_status(soup: BeautifulSoup) -> tuple[HLTVMatchStatus, str | None]:
    node = soup.select_one(MatchSel.COUNTDOWN)
    text = text_of(node)
    if not text:
        return HLTVMatchStatus.SCHEDULED, None
    mapped = COUNTDOWN_STATUS.get(text.lower())
    return (HLTVMatchStatus(mapped) if mapped else HLTVMatchStatus.SCHEDULED), text


# ---------------------------------------------------------------------------------------- maps


def _parse_maps(soup: BeautifulSoup, base_url: str, status: HLTVMatchStatus) -> list[MapResult]:
    maps: list[MapResult] = []
    for index, holder in enumerate(soup.select(MatchSel.MAPHOLDER), start=1):
        raw = (
            text_of(holder.select_one(MatchSel.MAP_NAME))
            or attr(holder.select_one("img[title]"), "title")
            or ""
        )
        normalized = normalize_map_name(raw) if raw else "tba"
        left = holder.select_one(MatchSel.MAP_LEFT)
        right = holder.select_one(MatchSel.MAP_RIGHT)
        s1 = parse_int(text_of(left.select_one(MatchSel.MAP_TEAM_SCORE) if left else None))
        s2 = parse_int(text_of(right.select_one(MatchSel.MAP_TEAM_SCORE) if right else None))
        stats_link = holder.select_one(MatchSel.MAP_STATS_LINK)
        stats_href = attr(stats_link, "href")
        mapstats_id = id_from(stats_href, MAPSTATS_ID_RE)
        optional = any(MatchSel.MAP_OPTIONAL_CLASS in classes(el) for el in holder.find_all(True))
        winner: TeamSlot | None = None
        if has_class(left, "won"):
            winner = TeamSlot.TEAM1
        elif has_class(right, "won"):
            winner = TeamSlot.TEAM2
        if normalized == FORFEIT_MAP_NAME:
            state = MapState.FORFEIT
        elif s1 is None or s2 is None or (optional and mapstats_id is None):
            state = MapState.UNPLAYED
        elif mapstats_id is None and status is HLTVMatchStatus.LIVE:
            state = MapState.LIVE
        else:
            state = MapState.PLAYED
        halves = (
            _parse_half_scores(holder.select_one(MatchSel.MAP_HALF_SCORE))
            if state is not MapState.UNPLAYED
            else []
        )
        if winner is None and state is MapState.PLAYED and s1 is not None and s2 is not None and s1 != s2:
            winner = TeamSlot.TEAM1 if s1 > s2 else TeamSlot.TEAM2
        regulation = [h for h in halves if not h.is_overtime]
        maps.append(
            MapResult(
                map_number=index,
                map_name=normalized,
                raw_name=raw,
                state=state,
                team1_rounds=s1 if state is not MapState.UNPLAYED else None,
                team2_rounds=s2 if state is not MapState.UNPLAYED else None,
                winner=winner if state is not MapState.UNPLAYED else None,
                halves=halves,
                team1_start_side=regulation[0].team1_side if regulation else None,
                mapstats_id=mapstats_id,
                mapstats_url=strip_fragment(absolute_url(stats_href, base_url)) if stats_href else None,
            )
        )
    return maps


def _parse_half_scores(node: Tag | None) -> list[HalfScore]:
    """``(7:5; 6:6) (7:5)`` spans -> halves; side classes mark regulation, plain groups are overtime."""
    if node is None:
        return []
    numbers: list[tuple[int, Side | None]] = []
    for span in node.find_all("span"):
        if span.find("span") is not None:
            continue  # only leaf spans carry one number
        value = parse_int(span.get_text())
        if value is None:
            continue
        cls = classes(span)
        side = Side.CT if "ct" in cls else Side.T if "t" in cls else None
        numbers.append((value, side))
    if not numbers:  # text-only fallback: "(7:5; 6:6) (7:5)"
        for a, b in SCORE_PAIR_RE.findall(node.get_text(" ")):
            numbers += [(int(a), None), (int(b), None)]
    halves: list[HalfScore] = []
    for i in range(0, len(numbers) - 1, 2):
        (a, side_a), (b, side_b) = numbers[i], numbers[i + 1]
        team1_side = side_a if side_a is not None else (side_b.other if side_b is not None else None)
        index = i // 2
        halves.append(
            HalfScore(
                team1_rounds=a,
                team2_rounds=b,
                team1_side=team1_side if index < 2 else None,
                is_overtime=index >= 2,
            )
        )
    # Regulation halves without side classes: infer from the other regulation half when possible.
    if len(halves) >= 2 and not halves[1].is_overtime:
        h1, h2 = halves[0], halves[1]
        if h1.team1_side is None and h2.team1_side is not None:
            halves[0] = h1.model_copy(update={"team1_side": h2.team1_side.other})
        elif h2.team1_side is None and h1.team1_side is not None:
            halves[1] = h2.model_copy(update={"team1_side": h1.team1_side.other})
    return halves


# ---------------------------------------------------------------------------------------- veto


def _veto_lines(soup: BeautifulSoup) -> list[str]:
    lines: list[str] = []
    for box in soup.select(MatchSel.VETO_BOX):
        divs = box.select(MatchSel.VETO_LINE_DIVS)
        candidates = [text_of(d) for d in divs] if divs else []
        if not candidates:
            # older layout: veto lines inside the preformatted text of the first box
            candidates = [clean_text(line) for line in box.get_text("\n").splitlines()]
        lines.extend(c for c in candidates if VETO_PICK_BAN_RE.match(c) or VETO_LEFTOVER_RE.match(c))
    return lines


def _parse_vetoes(
    soup: BeautifulSoup, team1: TeamRef | None, team2: TeamRef | None, warnings: list[str]
) -> list[VetoEntry]:
    entries: dict[int, VetoEntry] = {}
    for line in _veto_lines(soup):
        leftover = VETO_LEFTOVER_RE.match(line)
        if leftover:
            step = int(leftover.group(1))
            entry = VetoEntry(
                step=step,
                action=VetoAction.LEFTOVER,
                team=None,
                team_name=None,
                map_name=normalize_map_name(leftover.group(2)),
                raw=line,
            )
        else:
            m = VETO_PICK_BAN_RE.match(line)
            if m is None:
                continue
            step = int(m.group(1))
            team_name = clean_text(m.group(2))
            slot = resolve_slot(team_name, team1, team2)
            if slot is None:
                warnings.append(f"veto step {step}: team {team_name!r} matches neither team")
            entry = VetoEntry(
                step=step,
                action=VetoAction.PICK if m.group(3).lower() == "picked" else VetoAction.BAN,
                team=slot,
                team_name=team_name,
                map_name=normalize_map_name(m.group(4)),
                raw=line,
            )
        if step not in entries:
            entries[step] = entry
    return [entries[k] for k in sorted(entries)]


def _apply_veto_to_maps(maps: list[MapResult], vetoes: list[VetoEntry], best_of: int | None) -> None:
    """Mark who picked each map and which map is the decider (leftover)."""
    picks = {v.map_name: v for v in vetoes if v.action is VetoAction.PICK}
    leftover = {v.map_name for v in vetoes if v.action is VetoAction.LEFTOVER}
    for i, m in enumerate(maps):
        pick = picks.get(m.map_name)
        if pick is not None:
            maps[i] = m.model_copy(update={"picked_by": pick.team, "is_decider": False})
        elif m.map_name in leftover:
            maps[i] = m.model_copy(update={"picked_by": None, "is_decider": True})
        elif best_of == 1 and len(maps) == 1 and m.state is not MapState.FORFEIT and m.map_name != "tba":
            maps[i] = m.model_copy(update={"is_decider": True})


# ---------------------------------------------------------------------------------------- lineups


def _split_full_name(full: str | None) -> str | None:
    if not full:
        return None
    m = LINEUP_FULL_NAME_RE.match(full)
    if not m:
        return None
    real = clean_text(f"{m.group('first')} {m.group('last')}")
    return real or None


def _looks_standin(node: Tag) -> bool:
    """Best-effort stand-in marker (class / title / text containing "stand-in") near the player cell.

    HLTV's exact stand-in markup is not documented by the reference scrapers; this heuristic only
    fires on explicit "stand-in" tokens so a regular lineup is never flagged.
    """
    scope = node.find_parent("td")
    scope = scope if isinstance(scope, Tag) else node
    elements = [scope, *scope.find_all(True)]
    for el in elements:
        if any(STANDIN_RE.search(c) for c in classes(el)):
            return True
        for name in ("title", "alt", "data-tooltip", "aria-label"):
            value = attr(el, name)
            if value and STANDIN_RE.search(value):
                return True
    return bool(STANDIN_RE.search(text_of(scope)))


def _parse_lineup_block(block: Tag) -> list[LineupPlayerRow]:
    full_names: dict[int, str] = {}
    for link in block.select(MatchSel.PLAYER_LINK):
        pid = id_from(attr(link, "href"), PLAYER_ID_RE)
        img = link.select_one(MatchSel.PLAYER_PHOTO)
        name = attr(img, "alt") or attr(img, "title")
        if pid is not None and name and "'" in name:
            full_names.setdefault(pid, clean_text(name))
    players: list[LineupPlayerRow] = []
    seen: set[int] = set()
    for el in block.select(MatchSel.PLAYER_WITH_ID):
        pid = parse_int(attr(el, MatchSel.PLAYER_ID_ATTR))
        if pid is None or pid in seen:
            continue
        nick = text_of(el.select_one(MatchSel.PLAYER_NAME)) or text_of(el)
        if not nick:
            continue
        seen.add(pid)
        full = full_names.get(pid)
        players.append(
            LineupPlayerRow(
                nickname=nick,
                hltv_id=pid,
                is_standin=_looks_standin(el),
                full_name=full,
                real_name=_split_full_name(full),
                country=attr(el.select_one(MatchSel.PLAYER_FLAG), "title"),
            )
        )
    if players:
        return players
    # fallback: plain player links (layouts without data-player-id)
    for link in block.select(MatchSel.PLAYER_LINK):
        pid = id_from(attr(link, "href"), PLAYER_ID_RE)
        nick = text_of(link)
        if pid is None or pid in seen or not nick:
            continue
        seen.add(pid)
        players.append(LineupPlayerRow(nickname=nick, hltv_id=pid, is_standin=_looks_standin(link)))
    return players


def _parse_lineups(
    soup: BeautifulSoup, team1: TeamRef | None, team2: TeamRef | None, warnings: list[str]
) -> _Lineups:
    container = soup.select_one(MatchSel.LINEUPS)
    if container is None:
        return [], [], None, None, None
    blocks = [
        b
        for b in container.select(MatchSel.LINEUP_BLOCK)
        if b.select_one(MatchSel.PLAYERS) or b.select_one(MatchSel.PLAYER_LINK)
    ]
    out: dict[TeamSlot, list[LineupPlayerRow]] = {TeamSlot.TEAM1: [], TeamSlot.TEAM2: []}
    ranks: dict[TeamSlot, int | None] = {TeamSlot.TEAM1: None, TeamSlot.TEAM2: None}
    rank_date: date | None = None
    for index, block in enumerate(blocks[:2]):
        slot = TeamSlot.TEAM1 if index == 0 else TeamSlot.TEAM2
        team_id = id_from(attr(block.select_one(MatchSel.LINEUP_TEAM_LINK), "href"), TEAM_ID_RE)
        if team_id is not None:
            if team1 is not None and team1.hltv_id == team_id:
                slot = TeamSlot.TEAM1
            elif team2 is not None and team2.hltv_id == team_id:
                slot = TeamSlot.TEAM2
        rank_link = block.select_one(MatchSel.LINEUP_RANK)
        if rank_link is not None:
            m = RANK_RE.search(text_of(rank_link))
            ranks[slot] = int(m.group(1)) if m else None
            rank_date = rank_date or ranking_date_from_url(attr(rank_link, "href"))
        if out[slot]:
            warnings.append("two lineup blocks resolved to the same team; keeping the first")
            continue
        out[slot] = _parse_lineup_block(block)
    return out[TeamSlot.TEAM1], out[TeamSlot.TEAM2], ranks[TeamSlot.TEAM1], ranks[TeamSlot.TEAM2], rank_date


# ---------------------------------------------------------------------------------------- demos


def _guess_demo_map(el: Tag, maps: list[MapResult]) -> str | None:
    """Map a per-map demo button to its map by the map name written on/next to it (BO1: the only map)."""
    names = {m.map_name for m in maps if m.map_name not in ("tba", FORFEIT_MAP_NAME)}
    if len(names) <= 1:
        return next(iter(names)) if names and len(maps) == 1 else None
    texts = [text_of(el), attr(el, "title") or ""]
    parent = el.parent
    if isinstance(parent, Tag) and len(text_of(parent)) < 120:
        texts.append(text_of(parent))
    blob = f" {normalize_text(' '.join(texts))} "
    hits = []
    for name in names:
        variants = {name, "dust 2", "dust ii"} if name == "dust2" else {name}
        if any(f" {variant} " in blob for variant in variants):
            hits.append(name)
    return hits[0] if len(hits) == 1 else None


def _parse_demos(soup: BeautifulSoup, base_url: str, maps: list[MapResult]) -> list[DemoLink]:
    found: dict[int, DemoLink] = {}
    elements = soup.select(MatchSel.DEMO_LINK) + soup.select(MatchSel.DEMO_ATTR_ELEMENTS)
    for el in elements:
        for raw in (attr(el, "href"), attr(el, MatchSel.DEMO_ATTR)):
            demo_id = id_from(raw, DEMO_ID_RE)
            if demo_id is None or demo_id in found:
                continue
            found[demo_id] = DemoLink(
                hltv_demo_id=demo_id,
                url=absolute_url(f"/download/demo/{demo_id}", base_url),
                map_name=_guess_demo_map(el, maps),
            )
    return list(found.values())
