"""PandaScoreClient: auth, pagination, retries, rate limiting, typed results (respx, no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest
import respx

from cs2bot.config import PandaScoreSettings
from cs2bot.ingest.pandascore import (
    PandaScoreAuthError,
    PandaScoreClient,
    PandaScoreError,
    PandaScoreHTTPError,
    PandaScoreNotFoundError,
    PandaScorePlanError,
    PandaScoreRateLimitError,
    PandaScoreResponseError,
    PandaScoreTransportError,
    PSMatch,
    format_timestamp,
)
from cs2bot.utils.http import RateLimiter
from tests.ingest.pandascore.conftest import (
    BASE_URL,
    COLOGNE_PLAYOFFS_ID,
    FINISHED_ID,
    NOW,
    PS_VITALITY,
    TOKEN,
    UPCOMING_ID,
    FakePandaScore,
    NoWait,
    finished_games,
    finished_match,
    load_json,
    make_match,
    ps_team,
    upcoming_match,
)


def many_matches(n: int) -> list[dict[str, Any]]:
    return [
        make_match(
            9000 + i,
            ps_team(500 + 2 * i, f"Left {i:03d}"),
            ps_team(501 + 2 * i, f"Right {i:03d}"),
            scheduled_at=format_timestamp(NOW + timedelta(minutes=10 * (i + 1))),
        )
        for i in range(n)
    ]


def make_client(
    settings: PandaScoreSettings, sleeps: list[float] | None = None, **kwargs: Any
) -> PandaScoreClient:
    sink = sleeps if sleeps is not None else []
    kwargs.setdefault("rate_limiter", NoWait())
    return PandaScoreClient(settings, sleep=sink.append, now=lambda: NOW, **kwargs)


# ------------------------------------------------------------------------------------- auth


def test_bearer_token_is_sent_and_never_shown(client: PandaScoreClient, api: FakePandaScore) -> None:
    api.upcoming = [upcoming_match()]
    matches = client.upcoming_matches()
    assert [m.id for m in matches] == [UPCOMING_ID]
    request = api.requests[0]
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Accept"] == "application/json"
    assert "token" not in dict(request.url.params)  # never in the URL (it would end up in logs)
    assert TOKEN not in repr(client) and "has_token=True" in repr(client)


@pytest.mark.parametrize("token", [None, "", "   "])
def test_missing_token_raises_before_any_request(api: FakePandaScore, token: str | None) -> None:
    client = make_client(PandaScoreSettings(api_token=token))
    assert client.has_token is False
    with pytest.raises(PandaScoreAuthError) as excinfo:
        client.upcoming_matches()
    assert "CS2BOT_PANDASCORE__API_TOKEN" in str(excinfo.value)
    with pytest.raises(PandaScoreAuthError):
        client.match(1)
    assert api.requests == []


def test_rejected_token_raises_auth_error_without_leaking_it(
    api: FakePandaScore, sleeps: list[float]
) -> None:
    secret = "wrong-secret-token"
    client = make_client(PandaScoreSettings(api_token=secret), sleeps)
    with pytest.raises(PandaScoreAuthError) as excinfo:
        client.running_matches()
    assert isinstance(excinfo.value, PandaScoreError)
    assert secret not in str(excinfo.value) and secret not in repr(excinfo.value)
    assert len(api.requests) == 1 and sleeps == []  # a 401 is not retried


# ------------------------------------------------------------------------------------- pagination


def test_pagination_walks_every_page(api: FakePandaScore, ps_settings: PandaScoreSettings) -> None:
    api.upcoming = many_matches(7)
    limiter = NoWait()
    client = make_client(ps_settings.model_copy(update={"page_size": 3}), rate_limiter=limiter)
    matches = client.upcoming_matches()
    assert [m.id for m in matches] == [9000 + i for i in range(7)]
    assert all(isinstance(m, PSMatch) for m in matches)
    pages = [(p["page[number]"], p["page[size]"]) for p in api.params("/csgo/matches/upcoming")]
    assert pages == [("1", "3"), ("2", "3"), ("3", "3")]
    assert client.stats.pages == 3 and client.stats.requests == 3 and limiter.calls == 3
    assert client.stats.rate_limit_remaining == 997


def test_pagination_is_lazy_and_can_be_capped(api: FakePandaScore, ps_settings: PandaScoreSettings) -> None:
    api.upcoming = many_matches(7)
    client = make_client(ps_settings.model_copy(update={"page_size": 2}))
    iterator = client.iter_upcoming_matches()
    assert api.requests == []  # nothing is fetched before the first item is asked for
    assert next(iterator).id == 9000 and len(api.requests) == 1
    assert len(list(client.paginate("csgo/matches/upcoming", max_pages=2))) == 4
    assert len(list(client.paginate("csgo/matches/upcoming", page_size=500))) == 7
    assert api.params()[-1]["page[size]"] == "100"  # the API maximum


@pytest.mark.parametrize("headers", ["x-total", "none"])
def test_pagination_without_link_header(ps_settings: PandaScoreSettings, headers: str) -> None:
    rows = [{"id": i} for i in range(1, 6)]

    def handler(request: httpx.Request) -> httpx.Response:
        number, size = int(request.url.params["page[number]"]), int(request.url.params["page[size]"])
        extra = {"X-Total": str(len(rows))} if headers == "x-total" else {}
        return httpx.Response(200, json=rows[(number - 1) * size : number * size], headers=extra)

    client = make_client(ps_settings.model_copy(update={"page_size": 2}))
    with respx.mock(assert_all_mocked=True) as router:
        route = router.get(f"{BASE_URL}/csgo/players").mock(side_effect=handler)
        assert [r["id"] for r in client.paginate("csgo/players")] == [1, 2, 3, 4, 5]
        assert route.call_count == 3  # the short third page ends the walk


def test_rows_shifting_between_pages_are_not_duplicated(ps_settings: PandaScoreSettings) -> None:
    pages = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 2}, {"id": 3}], 3: ["junk", {"id": 4}], 4: []}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[int(request.url.params["page[number]"])])

    client = make_client(ps_settings.model_copy(update={"page_size": 2}))
    with respx.mock(assert_all_mocked=True) as router:
        router.get(f"{BASE_URL}/csgo/players").mock(side_effect=handler)
        assert [r["id"] for r in client.paginate("csgo/players")] == [1, 2, 3, 4]
    assert client.stats.invalid_items == 1


def test_malformed_items_are_skipped_not_fatal(client: PandaScoreClient, api: FakePandaScore) -> None:
    broken = make_match(9101, ps_team(1, "A"), ps_team(2, "B"))
    broken["games"] = {"not": "a list"}
    api.upcoming = [broken, upcoming_match()]
    seen: list[tuple[Any, str]] = []
    matches = list(
        client.iter_upcoming_matches(on_invalid=lambda raw, exc: seen.append((raw["id"], type(exc).__name__)))
    )
    assert [m.id for m in matches] == [UPCOMING_ID]
    assert seen == [(9101, "ValidationError")] and client.stats.invalid_items == 1
    assert [m.id for m in client.upcoming_matches()] == [UPCOMING_ID]  # default handler just logs


# ------------------------------------------------------------------------------------- retries


def test_429_honours_retry_after_then_succeeds(
    client: PandaScoreClient, api: FakePandaScore, sleeps: list[float], limiter: NoWait
) -> None:
    api.upcoming = [upcoming_match()]
    api.override(
        r"/csgo/matches/upcoming",
        httpx.Response(429, headers={"Retry-After": "7"}, json={"error": "Too Many Requests"}),
        httpx.Response(
            429, headers={"Retry-After": format_datetime(NOW + timedelta(seconds=42), usegmt=True)}
        ),
    )
    assert [m.id for m in client.upcoming_matches()] == [UPCOMING_ID]
    assert sleeps == [7.0, 42.0]
    assert client.stats.requests == 3 and client.stats.retries == 2
    assert limiter.calls == 3  # every attempt goes through the rate limiter


def test_5xx_and_transport_errors_back_off_exponentially(
    client: PandaScoreClient, api: FakePandaScore, sleeps: list[float]
) -> None:
    api.running = [upcoming_match()]

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    calls = iter([boom, lambda r: httpx.Response(503), lambda r: httpx.Response(502)])

    def flaky(request: httpx.Request) -> httpx.Response:
        step = next(calls, None)
        return step(request) if step is not None else api._page(request, api.running)

    api.override(r"/csgo/matches/running", flaky)
    assert len(client.running_matches()) == 1
    assert sleeps == [2.0, 4.0, 8.0]


def test_retries_are_bounded(api: FakePandaScore, ps_settings: PandaScoreSettings) -> None:
    sleeps: list[float] = []
    client = make_client(ps_settings, sleeps, max_attempts=3)
    api.override(r"/csgo/matches/running", lambda r: httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(PandaScoreHTTPError) as excinfo:
        client.running_matches()
    assert excinfo.value.status_code == 500 and len(api.requests) == 3 and sleeps == [2.0, 4.0]

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    api.override(r"/teams/1$", timeout)
    with pytest.raises(PandaScoreTransportError, match="ReadTimeout after 3 attempts"):
        client.team(1)


def test_rate_limit_that_does_not_clear(api: FakePandaScore, ps_settings: PandaScoreSettings) -> None:
    sleeps: list[float] = []
    client = make_client(ps_settings, sleeps, max_attempts=2)
    api.override(r"/csgo/matches/upcoming", lambda r: httpx.Response(429))
    with pytest.raises(PandaScoreRateLimitError) as excinfo:
        client.upcoming_matches()
    assert excinfo.value.status_code == 429 and excinfo.value.retry_after_s is None
    assert sleeps == [2.0]

    # an hour-long Retry-After is not slept through: the scheduler simply runs the job again later
    sleeps.clear()
    api.overrides.clear()
    api.override(r"/csgo/matches/upcoming", lambda r: httpx.Response(429, headers={"Retry-After": "3600"}))
    with pytest.raises(PandaScoreRateLimitError) as excinfo:
        client.upcoming_matches()
    assert excinfo.value.retry_after_s == 3600.0 and sleeps == []


def test_default_rate_limiter_spaces_requests(api: FakePandaScore, ps_settings: PandaScoreSettings) -> None:
    api.upcoming = many_matches(3)
    sleeps: list[float] = []
    settings = ps_settings.model_copy(update={"page_size": 1, "max_requests_per_hour": 1800})
    client = PandaScoreClient(settings, sleep=sleeps.append)  # default limiter, injected sleep
    assert isinstance(client._limiter, RateLimiter) and client._limiter.min_interval_s == 2.0
    assert len(client.upcoming_matches()) == 3
    # 3 requests -> 2 waits; the first is the full 2 s gap (the injected sleep does not advance the clock)
    assert len(sleeps) == 2 and sleeps[0] == pytest.approx(2.0, abs=0.5)


# ------------------------------------------------------------------------------------- errors


def test_http_errors_map_to_typed_exceptions(client: PandaScoreClient, api: FakePandaScore) -> None:
    with pytest.raises(PandaScoreNotFoundError):
        client.match(404404)
    api.override(r"/teams/5$", httpx.Response(403, json={"error": "Forbidden", "message": "Plan too low"}))
    with pytest.raises(PandaScorePlanError, match="Plan too low"):
        client.team(5)
    api.override(r"/teams/6$", httpx.Response(400, json={"error": "Bad Request", "message": "bad filter"}))
    with pytest.raises(PandaScoreHTTPError, match="HTTP 400 Bad Request: bad filter") as excinfo:
        client.team(6)
    assert excinfo.value.status_code == 400
    api.override(r"/teams/7$", httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(PandaScoreResponseError, match="not JSON"):
        client.team(7)
    api.override(r"/teams/8$", httpx.Response(200, json=[1, 2]))
    with pytest.raises(PandaScoreResponseError, match="expected a JSON object"):
        client.team(8)
    api.override(r"/csgo/matches/running", httpx.Response(200, json={"error": "nope"}))
    with pytest.raises(PandaScoreResponseError, match="expected a JSON list"):
        client.running_matches()
    api.override(r"/teams/9$", httpx.Response(200, json={"id": "not-a-number"}))
    with pytest.raises(PandaScoreResponseError, match="malformed PSTeam"):
        client.team(9)


# ------------------------------------------------------------------------------------- endpoints


def test_upcoming_running_past_parameters(client: PandaScoreClient, api: FakePandaScore) -> None:
    api.upcoming = many_matches(3)  # +10, +20, +30 minutes
    late = make_match(9200, ps_team(1, "A"), ps_team(2, "B"), scheduled_at="2026-11-30T12:00:00Z")
    api.upcoming.append(late)
    assert len(client.upcoming_matches()) == 4
    assert "range[begin_at]" not in api.params()[-1] and api.params()[-1]["sort"] == "begin_at"
    assert len(client.upcoming_matches(hours_ahead=48, videogame_title="cs-2")) == 3
    params = api.params()[-1]
    assert params["range[begin_at]"] == "2026-11-22T11:00:00Z,2026-11-24T12:00:00Z"
    assert params["filter[videogame_title]"] == "cs-2"

    api.past = [finished_match()]
    since = datetime(2026, 11, 21, tzinfo=UTC)
    assert [m.id for m in client.past_matches(since)] == [FINISHED_ID]
    assert api.params()[-1]["range[begin_at]"] == "2026-11-21T00:00:00Z,2026-11-22T12:00:00Z"
    assert client.past_matches(since, datetime(2026, 11, 21, 12, tzinfo=UTC)) == []
    assert api.params()[-1]["range[begin_at]"] == "2026-11-21T00:00:00Z,2026-11-21T12:00:00Z"
    # naive / non-UTC datetimes are normalised to UTC
    warsaw = datetime.fromisoformat("2026-11-21T01:00:00+01:00")
    assert format_timestamp(warsaw) == "2026-11-21T00:00:00Z"

    api.running = [upcoming_match()]
    assert [m.id for m in client.running_matches()] == [UPCOMING_ID]
    assert api.paths()[-1] == "/csgo/matches/running"


def test_single_resources(client: PandaScoreClient, api: FakePandaScore) -> None:
    api.matches[FINISHED_ID] = finished_match()
    api.games[FINISHED_ID] = list(reversed(finished_games()))
    api.teams[PS_VITALITY] = load_json("team_vitality.json")
    api.tournaments[COLOGNE_PLAYOFFS_ID] = load_json("tournament_iem_cologne_playoffs.json")
    api.rosters[COLOGNE_PLAYOFFS_ID] = load_json("rosters_iem_cologne_playoffs.json")

    match = client.match(FINISHED_ID)
    assert match.id == FINISHED_ID and match.winner_id == PS_VITALITY and len(match.games) == 3
    games = client.games(FINISHED_ID)
    assert [g.position for g in games] == [1, 2, 3]  # sorted by position whatever the API order
    assert games[0].map is not None and games[0].map.name == "Mirage" and len(games[0].rounds) == 24
    team = client.team(PS_VITALITY)
    assert team.name == "Vitality" and len(team.players) == 6 and team.players[0].name == "ZywOo"
    tournament = client.tournament(COLOGNE_PLAYOFFS_ID)
    assert tournament.tier == "s" and tournament.type == "offline" and len(tournament.expected_roster) == 2
    assert tournament.league is not None and tournament.league.name == "Intel Extreme Masters"
    rosters = client.tournament_rosters(COLOGNE_PLAYOFFS_ID)
    assert [t.acronym for t in rosters.rosters] == ["NAVI", "VIT"] and len(rosters.rosters[0].players) == 5
    assert api.paths() == [
        f"/csgo/matches/{FINISHED_ID}",
        f"/csgo/matches/{FINISHED_ID}/games",
        f"/teams/{PS_VITALITY}",
        f"/tournaments/{COLOGNE_PLAYOFFS_ID}",
        f"/tournaments/{COLOGNE_PLAYOFFS_ID}/rosters",
    ]


def test_single_match_falls_back_to_the_list_endpoint(client: PandaScoreClient, api: FakePandaScore) -> None:
    """``/csgo/matches/{id}`` needs a historical plan; the list endpoint with ``filter[id]`` does not."""
    api.past = [finished_match()]
    api.override(r"/csgo/matches/[^/]+$", lambda r: httpx.Response(403, json={"error": "Forbidden"}))
    assert client.match(FINISHED_ID).id == FINISHED_ID
    assert api.paths()[-1] == "/csgo/matches" and api.params()[-1]["filter[id]"] == str(FINISHED_ID)
    api.requests.clear()
    with pytest.raises(PandaScoreNotFoundError):
        client.match(31337)
    assert api.paths() == ["/csgo/matches"]  # the forbidden endpoint is not tried a second time
    with pytest.raises(PandaScorePlanError):
        client.match("some-slug")


def test_custom_base_url_and_videogame(api: FakePandaScore) -> None:
    settings = PandaScoreSettings(
        api_token=TOKEN,
        base_url="https://api.pandascore.co/",
        videogame="/csgo/",
    )
    http = httpx.Client()
    client = PandaScoreClient(settings, http_client=http, sleep=lambda _s: None, rate_limiter=NoWait())
    api.upcoming = [upcoming_match()]
    assert len(client.upcoming_matches()) == 1 and api.paths() == ["/csgo/matches/upcoming"]
    client.close()
    assert http.is_closed is False  # an injected http client stays under the caller's control
    http.close()
