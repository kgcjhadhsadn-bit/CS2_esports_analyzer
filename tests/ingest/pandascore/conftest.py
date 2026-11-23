"""Fixtures for the PandaScore package tests.

The JSON files under ``fixtures/`` were written from the PandaScore reference (developers.pandascore.co:
``/csgo/matches/*``, ``/csgo/matches/{id}/games``, ``/tournaments/{id}/rosters``, ``/teams/{id}``,
``/tournaments/{id}``) - no live token was available. They describe the same matches as the HLTV
fixtures (Vitality 2-1 Natus Vincere at IEM Cologne 2026, G2 vs Spirit upcoming) so both sources can
be run against one database.

:class:`FakePandaScore` is an in-memory PandaScore API mounted behind ``respx``: list endpoints paginate
(``page[number]`` / ``page[size]``, ``X-Total`` and ``Link`` headers), honour ``range[begin_at]``,
``range[modified_at]``, ``filter[videogame_title]``, ``filter[id]`` and ``sort``, and require the bearer
token. Nothing here touches the network.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from cs2bot.config import PandaScoreSettings, Settings
from cs2bot.ingest.pandascore import PandaScoreClient
from cs2bot.utils.time import parse_datetime

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://api.pandascore.co"
TOKEN = "test-token-do-not-leak"
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)

FINISHED_ID = 1287001  # Natus Vincere 1-2 Vitality (PandaScore lists NAVI first, HLTV lists Vitality first)
UPCOMING_ID = 1287050  # Team Spirit vs G2
PS_NAVI, PS_VITALITY, PS_G2, PS_SPIRIT = 3216, 3455, 3210, 124523
COLOGNE_PLAYOFFS_ID = 17201
MAJOR_PLAYOFFS_ID = 17260

Json = dict[str, Any]
Reply = httpx.Response | Callable[[httpx.Request], httpx.Response]


def load_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def finished_match() -> Json:
    data: Json = load_json("match_finished_bo3.json")
    return data


def upcoming_match() -> Json:
    data: Json = load_json("match_upcoming.json")
    return data


def finished_games() -> list[Json]:
    data: list[Json] = load_json("games_finished_bo3.json")
    return data


def ps_team(team_id: int, name: str, acronym: str | None = None, location: str | None = None) -> Json:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return {
        "acronym": acronym,
        "dark_mode_image_url": None,
        "id": team_id,
        "image_url": None,
        "location": location,
        "modified_at": "2026-11-01T00:00:00Z",
        "name": name,
        "slug": slug,
    }


def make_match(
    match_id: int,
    team_a: Json | None,
    team_b: Json | None,
    *,
    scheduled_at: str = "2026-11-23T15:00:00Z",
    status: str = "not_started",
    tier: str | None = "s",
    tournament_id: int | None = None,
    number_of_games: int = 3,
    match_type: str = "best_of",
    scores: tuple[int, int] | None = None,
    **overrides: Any,
) -> Json:
    """A match payload derived from ``match_upcoming.json`` (missing teams = TBD opponents)."""
    match = upcoming_match()
    opponents = [t for t in (team_a, team_b) if t is not None]
    match.update(
        {
            "id": match_id,
            "name": " vs ".join(t["acronym"] or t["name"] for t in opponents) or "TBD vs TBD",
            "slug": f"match-{match_id}",
            "status": status,
            "scheduled_at": scheduled_at,
            "original_scheduled_at": scheduled_at,
            "begin_at": scheduled_at,
            "number_of_games": number_of_games,
            "match_type": match_type,
            "opponents": [{"opponent": t, "type": "Team"} for t in opponents],
            "results": [{"score": 0, "team_id": t["id"]} for t in opponents],
            "games": [
                {
                    **copy.deepcopy(match["games"][0]),
                    "id": match_id * 10 + n,
                    "match_id": match_id,
                    "position": n,
                }
                for n in range(1, number_of_games + 1)
            ],
        }
    )
    tournament = copy.deepcopy(match["tournament"])
    tournament["tier"] = tier
    if tournament_id is not None:
        tournament["id"] = tournament_id
        tournament["slug"] = f"tournament-{tournament_id}"
        match["tournament_id"] = tournament_id
    match["tournament"] = tournament
    if scores is not None and len(opponents) == 2:
        match["results"] = [
            {"score": scores[0], "team_id": opponents[0]["id"]},
            {"score": scores[1], "team_id": opponents[1]["id"]},
        ]
        if scores[0] != scores[1]:
            winner = opponents[0] if scores[0] > scores[1] else opponents[1]
            match["winner"], match["winner_id"] = winner, winner["id"]
    match.update(overrides)
    return match


class NoWait:
    """Rate limiter stand-in that only counts calls."""

    def __init__(self) -> None:
        self.calls = 0

    def wait(self) -> float:
        self.calls += 1
        return 0.0


class FakePandaScore:
    """In-memory PandaScore REST API (see the module docstring)."""

    def __init__(self) -> None:
        self.upcoming: list[Json] = []
        self.running: list[Json] = []
        self.past: list[Json] = []
        self.matches: dict[int, Json] = {}  # extra single matches for /csgo/matches/{id}
        self.games: dict[int, list[Json]] = {}
        self.rosters: dict[int, Json] = {}
        self.teams: dict[int, Json] = {}
        self.tournaments: dict[int, Json] = {}
        self.requests: list[httpx.Request] = []
        # path regex -> replies served (in order) before the normal handler; a callable reply stays
        self.overrides: list[tuple[re.Pattern[str], list[Reply]]] = []

    # ---------------------------------------------------------------- test helpers

    def override(self, path_pattern: str, *replies: Reply) -> None:
        self.overrides.append((re.compile(path_pattern), list(replies)))

    def paths(self, pattern: str = "") -> list[str]:
        return [r.url.path for r in self.requests if re.search(pattern, r.url.path)]

    def params(self, pattern: str = "") -> list[dict[str, str]]:
        return [dict(r.url.params) for r in self.requests if re.search(pattern, r.url.path)]

    def all_matches(self) -> list[Json]:
        seen: dict[int, Json] = {}
        for match in [*self.past, *self.running, *self.upcoming, *self.matches.values()]:
            seen.setdefault(match["id"], match)
        return list(seen.values())

    # ---------------------------------------------------------------- request handling

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        for pattern, replies in self.overrides:
            if pattern.search(path) and replies:
                reply = replies[0]
                if callable(reply):
                    return reply(request)
                replies.pop(0)
                return reply
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "Unauthorized", "message": "Invalid token"})
        if path == "/csgo/matches/upcoming":
            return self._page(request, self.upcoming)
        if path == "/csgo/matches/running":
            return self._page(request, self.running)
        if path == "/csgo/matches/past":
            return self._page(request, self.past)
        if path == "/csgo/matches":
            return self._page(request, self.all_matches())
        found = re.fullmatch(r"/csgo/matches/(\d+)/games", path)
        if found:
            games = self.games.get(int(found.group(1)))
            return self._page(request, games) if games is not None else self._not_found()
        found = re.fullmatch(r"/csgo/matches/(\d+)", path)
        if found:
            wanted = int(found.group(1))
            match = next((m for m in self.all_matches() if m["id"] == wanted), None)
            return httpx.Response(200, json=match) if match is not None else self._not_found()
        found = re.fullmatch(r"/tournaments/(\d+)/rosters", path)
        if found:
            return self._one(self.rosters.get(int(found.group(1))))
        found = re.fullmatch(r"/tournaments/(\d+)", path)
        if found:
            return self._one(self.tournaments.get(int(found.group(1))))
        found = re.fullmatch(r"/teams/(\d+)", path)
        if found:
            return self._one(self.teams.get(int(found.group(1))))
        return self._not_found()

    @staticmethod
    def _not_found() -> httpx.Response:
        return httpx.Response(404, json={"error": "Not Found", "message": "Not Found"})

    def _one(self, payload: Json | None) -> httpx.Response:
        return httpx.Response(200, json=payload) if payload is not None else self._not_found()

    @staticmethod
    def _in_range(item: Json, field: str, bounds: str) -> bool:
        lo, hi = (parse_datetime(part) for part in bounds.split(","))
        try:
            value = parse_datetime(item.get(field))
        except ValueError:
            return True  # a corrupt timestamp is the client's problem, not the fake server's
        return value is not None and lo is not None and hi is not None and lo <= value <= hi

    def _page(self, request: httpx.Request, items: list[Json]) -> httpx.Response:
        params = request.url.params
        rows = list(items)
        for field in ("begin_at", "modified_at", "scheduled_at"):
            bounds = params.get(f"range[{field}]")
            if bounds:
                rows = [r for r in rows if self._in_range(r, field, bounds)]
        title = params.get("filter[videogame_title]")
        if title:
            rows = [r for r in rows if (r.get("videogame_title") or {}).get("slug") == title]
        ids = params.get("filter[id]")
        if ids:
            rows = [r for r in rows if str(r.get("id")) in ids.split(",")]
        sort = params.get("sort")
        if sort:
            field = sort.split(",")[0]
            rows.sort(key=lambda r: str(r.get(field.lstrip("-")) or ""), reverse=field.startswith("-"))
        number = int(params.get("page[number]", "1"))
        size = int(params.get("page[size]", "50"))
        chunk = rows[(number - 1) * size : number * size]
        last = max(1, -(-len(rows) // size))
        url = f"{BASE_URL}{request.url.path}"
        links = [f'<{url}?page={last}>; rel="last"', f'<{url}?page=1>; rel="first"']
        if number < last:
            links.append(f'<{url}?page={number + 1}>; rel="next"')
        if number > 1:
            links.append(f'<{url}?page={number - 1}>; rel="previous"')
        headers = {
            "X-Page": str(number),
            "X-Per-Page": str(size),
            "X-Total": str(len(rows)),
            "Link": ", ".join(links),
            "X-Rate-Limit-Remaining": str(1000 - len(self.requests)),
        }
        return httpx.Response(200, json=chunk, headers=headers)


@pytest.fixture
def api() -> Iterator[FakePandaScore]:
    fake = FakePandaScore()
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        router.route(url__startswith=BASE_URL).mock(side_effect=fake.handle)
        yield fake


@pytest.fixture
def ps_settings() -> PandaScoreSettings:
    return PandaScoreSettings(api_token=TOKEN)


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def limiter() -> NoWait:
    return NoWait()


@pytest.fixture
def client(
    api: FakePandaScore, ps_settings: PandaScoreSettings, sleeps: list[float], limiter: NoWait
) -> Iterator[PandaScoreClient]:
    """Client against :class:`FakePandaScore` with a no-op sleep and limiter (tests never wait)."""
    with PandaScoreClient(ps_settings, sleep=sleeps.append, rate_limiter=limiter, now=lambda: NOW) as c:
        yield c


@pytest.fixture
def app_settings(settings: Settings, ps_settings: PandaScoreSettings) -> Settings:
    return settings.model_copy(update={"pandascore": ps_settings})
