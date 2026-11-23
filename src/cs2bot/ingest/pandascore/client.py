"""PandaScore REST client: bearer auth, hourly rate limit, retries, pagination, typed results.

API facts used here (developers.pandascore.co, read November 2026):

* Base URL ``https://api.pandascore.co``; CS2 lives under the legacy ``/csgo/`` prefix. Single teams and
  tournaments are only served by the generic ``/teams/{id}`` and ``/tournaments/{id}`` endpoints.
* Auth: ``Authorization: Bearer <token>``. 401 = missing/invalid token, 403 = endpoint not in the plan.
* Pagination: ``page[number]`` (from 1) and ``page[size]`` (default 50, max 100). Responses carry
  ``X-Page``, ``X-Per-Page``, ``X-Total`` and a ``Link`` header with ``rel="next"`` while more pages exist.
* Filters: ``filter[field]=a,b``, ``range[field]=lo,hi`` (also for timestamps), ``sort=field,-other``.
  ``filter[videogame_title]=cs-2`` separates CS2 from CS:GO.
* Rate limit: 1 000 requests/hour on the entry plan, 10 000 on the historical/real-time plans;
  ``X-Rate-Limit-Remaining`` reports what is left, 429 is returned when it is exhausted.

The client never logs or raises the token.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from cs2bot.config import PandaScoreSettings
from cs2bot.ingest.pandascore.errors import (
    PandaScoreAuthError,
    PandaScoreHTTPError,
    PandaScoreNotFoundError,
    PandaScorePlanError,
    PandaScoreRateLimitError,
    PandaScoreResponseError,
    PandaScoreTransportError,
)
from cs2bot.ingest.pandascore.models import (
    PSGame,
    PSMatch,
    PSMatchOpponents,
    PSModel,
    PSTeam,
    PSTournament,
    PSTournamentRosters,
)
from cs2bot.utils.http import DEFAULT_USER_AGENT, RateLimiter
from cs2bot.utils.time import ensure_utc, utcnow

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100
_TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

Params = Mapping[str, str | int | float | bool]
InvalidHandler = Callable[[dict[str, Any], Exception], None]


class SupportsWait(Protocol):
    def wait(self) -> float: ...


@dataclass
class ClientStats:
    """Counters for diagnostics (``cs2bot doctor`` / job details)."""

    requests: int = 0
    retries: int = 0
    pages: int = 0
    invalid_items: int = 0
    rate_limit_remaining: int | None = None  # last ``X-Rate-Limit-Remaining`` seen


def format_timestamp(value: datetime) -> str:
    """ISO-8601 UTC with a ``Z`` suffix, as PandaScore's range/filter parameters expect."""
    return ensure_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _retry_after_seconds(response: httpx.Response, now: datetime) -> float | None:
    """``Retry-After`` as seconds (delta-seconds or HTTP-date form); None when absent/unparseable."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, (ensure_utc(when) - now).total_seconds())


def _int_header(response: httpx.Response, name: str) -> int | None:
    raw = response.headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _error_text(response: httpx.Response) -> str:
    """Short server-side explanation (``{"error": ..., "message": ...}``), never request data."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        parts = [str(body[k]) for k in ("error", "message") if body.get(k)]
        return ": ".join(parts)[:300]
    return ""


class PandaScoreClient:
    """Rate-limited, retrying, paginated PandaScore client returning typed payload models.

    ``http_client``, ``rate_limiter`` and ``sleep`` are injectable for tests. A client passed in via
    ``http_client`` is not closed by :meth:`close`. Safe to share between threads (``httpx.Client`` and
    the limiter are thread-safe; the counters in ``stats`` are best-effort).
    """

    def __init__(
        self,
        settings: PandaScoreSettings,
        *,
        http_client: httpx.Client | None = None,
        rate_limiter: SupportsWait | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 5,
        backoff_base_s: float = 2.0,
        backoff_max_s: float = 60.0,
        max_retry_after_s: float = 300.0,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._settings = settings
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(settings.timeout_s),
            headers={"User-Agent": DEFAULT_USER_AGENT},
            follow_redirects=True,
        )
        self._sleep = sleep
        self._limiter: SupportsWait = rate_limiter or RateLimiter(
            3600.0 / max(1, settings.max_requests_per_hour), sleep=sleep
        )
        self._max_attempts = max(1, max_attempts)
        self._backoff_base_s = backoff_base_s
        self._backoff_max_s = backoff_max_s
        self._max_retry_after_s = max_retry_after_s
        self._now = now
        self._base_url = settings.base_url.rstrip("/")
        self._game = settings.videogame.strip("/")
        self.stats = ClientStats()
        self._single_match_forbidden = False  # set after a 403 on /matches/{id} (see :meth:`match`)

    def __repr__(self) -> str:  # never show the token
        return f"PandaScoreClient(base_url={self._base_url!r}, has_token={self.has_token})"

    # ------------------------------------------------------------------ lifecycle / auth

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> PandaScoreClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def has_token(self) -> bool:
        token = self._settings.api_token
        return token is not None and bool(token.get_secret_value().strip())

    def ensure_token(self) -> None:
        """Raise :class:`PandaScoreAuthError` when no token is configured (nothing is sent)."""
        if not self.has_token:
            raise PandaScoreAuthError(
                "PandaScore API token is not configured: set CS2BOT_PANDASCORE__API_TOKEN "
                "(the token is shown on the PandaScore dashboard)"
            )

    def _headers(self) -> dict[str, str]:
        self.ensure_token()
        token = self._settings.api_token
        assert token is not None  # ensure_token() checked it
        return {"Authorization": f"Bearer {token.get_secret_value().strip()}", "Accept": "application/json"}

    # ------------------------------------------------------------------ low level

    def _backoff(self, attempt: int) -> float:
        return float(min(self._backoff_max_s, self._backoff_base_s * (2 ** (attempt - 1))))

    def request(self, path: str, params: Params | None = None) -> httpx.Response:
        """GET ``path`` (relative to the base URL) with auth, rate limiting and retries.

        Retries 429 / 5xx / connection errors up to ``max_attempts`` times, waiting ``Retry-After``
        when the API sends it (otherwise exponential backoff). A ``Retry-After`` longer than
        ``max_retry_after_s`` is not slept through: :class:`PandaScoreRateLimitError` is raised so the
        scheduler can simply try again on its next run.
        """
        headers = self._headers()
        url = f"{self._base_url}/{path.lstrip('/')}"
        query = {k: (str(v).lower() if isinstance(v, bool) else str(v)) for k, v in (params or {}).items()}
        what = f"GET /{path.lstrip('/')}"
        for attempt in range(1, self._max_attempts + 1):
            self._limiter.wait()
            last_attempt = attempt == self._max_attempts
            try:
                response = self._http.request("GET", url, params=query, headers=headers)
            except httpx.TransportError as exc:
                self.stats.requests += 1
                if last_attempt:
                    raise PandaScoreTransportError(
                        f"{what}: {type(exc).__name__} after {attempt} attempts"
                    ) from exc
                self.stats.retries += 1
                self._sleep(self._backoff(attempt))
                continue
            self.stats.requests += 1
            remaining = _int_header(response, "X-Rate-Limit-Remaining")
            if remaining is not None:
                self.stats.rate_limit_remaining = remaining
            status = response.status_code
            if status in _TRANSIENT_STATUS:
                retry_after = _retry_after_seconds(response, self._now())
                delay = retry_after if retry_after is not None else self._backoff(attempt)
                if status == 429 and (last_attempt or delay > self._max_retry_after_s):
                    raise PandaScoreRateLimitError(
                        f"{what}: rate limit exceeded (HTTP 429)", retry_after_s=retry_after
                    )
                if last_attempt:
                    raise PandaScoreHTTPError(
                        f"{what}: HTTP {status} after {attempt} attempts", status_code=status
                    )
                self.stats.retries += 1
                logger.warning("PandaScore %s -> HTTP %s; retrying in %.0f s", what, status, delay)
                self._sleep(min(delay, self._max_retry_after_s))
                continue
            if status == 401:
                raise PandaScoreAuthError(
                    f"{what}: PandaScore rejected the API token (HTTP 401); check "
                    "CS2BOT_PANDASCORE__API_TOKEN"
                )
            if status == 403:
                detail = _error_text(response)
                raise PandaScorePlanError(
                    f"{what}: not available on this PandaScore plan (HTTP 403) {detail}".strip()
                )
            if status == 404:
                raise PandaScoreNotFoundError(f"{what}: not found (HTTP 404)")
            if status >= 400:
                raise PandaScoreHTTPError(
                    f"{what}: HTTP {status} {_error_text(response)}".strip(), status_code=status
                )
            return response
        raise AssertionError("unreachable")  # pragma: no cover - the loop always returns or raises

    def get_json(self, path: str, params: Params | None = None) -> Any:
        """GET and decode one JSON document."""
        response = self.request(path, params)
        try:
            return response.json()
        except ValueError as exc:
            raise PandaScoreResponseError(f"GET /{path.lstrip('/')}: response is not JSON") from exc

    def paginate(
        self,
        path: str,
        params: Params | None = None,
        *,
        page_size: int | None = None,
        max_pages: int | None = None,
        start_page: int = 1,
    ) -> Iterator[dict[str, Any]]:
        """Yield every item of a list endpoint, page by page (lazily: one request per page).

        Stops when the ``Link`` header has no ``rel="next"``; without a ``Link`` header, when
        ``X-Total`` is reached or a page comes back shorter than ``page[size]``. Items are de-duplicated
        by ``id``: when the underlying list changes while we page through it, rows shift between pages
        and would otherwise be seen twice. Non-object items are counted in ``stats.invalid_items``.
        """
        size = min(max(1, page_size or self._settings.page_size), MAX_PAGE_SIZE)
        base = dict(params or {})
        page = max(1, start_page)
        fetched = 0
        seen: set[Any] = set()
        while True:
            response = self.request(path, {**base, "page[number]": page, "page[size]": size})
            try:
                data = response.json()
            except ValueError as exc:
                raise PandaScoreResponseError(f"GET /{path.lstrip('/')}: response is not JSON") from exc
            if not isinstance(data, list):
                raise PandaScoreResponseError(f"GET /{path.lstrip('/')}: expected a JSON list")
            self.stats.pages += 1
            fetched += 1
            objects = duplicates = 0
            for item in data:
                if not isinstance(item, dict):
                    self.stats.invalid_items += 1
                    continue
                objects += 1
                key = item.get("id")
                if isinstance(key, int | str):  # a malformed (e.g. list) id is not hashable: no dedup
                    if key in seen:
                        duplicates += 1
                        continue
                    seen.add(key)
                yield item
            if not data:
                return
            if objects and duplicates == objects:
                # A full page of rows we already yielded: the server ignores page[number] (or the
                # list shifted by more than a page). Following ``next`` would loop forever.
                logger.warning("PandaScore GET /%s: page %d repeats earlier rows; stopping", path, page)
                return
            if "link" in response.headers:
                if "next" not in response.links:
                    return
            else:
                total = _int_header(response, "X-Total")
                if total is not None:
                    if page * size >= total:
                        return
                elif len(data) < size:
                    return
            if max_pages is not None and fetched >= max_pages:
                return
            page += 1

    def _typed[M: PSModel](
        self, model: type[M], items: Iterator[dict[str, Any]], on_invalid: InvalidHandler | None
    ) -> Iterator[M]:
        """Validate items one by one; a malformed item is reported and skipped, never fatal.

        Besides pydantic's ``ValidationError`` (a ``ValueError``), any ``TypeError`` / ``ArithmeticError``
        / ``OSError`` a validator lets escape on absurd input is treated the same way.
        """
        for raw in items:
            try:
                parsed = model.model_validate(raw)
            except (ValueError, TypeError, ArithmeticError, OSError) as exc:
                self.stats.invalid_items += 1
                if on_invalid is not None:
                    on_invalid(raw, exc)
                else:
                    logger.warning(
                        "PandaScore: skipping malformed %s id=%r (%d validation errors)",
                        model.__name__,
                        str(raw.get("id"))[:40],
                        exc.error_count() if isinstance(exc, ValidationError) else 1,
                    )
                continue
            yield parsed

    # ------------------------------------------------------------------ matches

    def _match_params(self, extra: Params | None, videogame_title: str | None) -> dict[str, Any]:
        params: dict[str, Any] = dict(extra or {})
        if videogame_title:
            params["filter[videogame_title]"] = videogame_title
        return params

    def iter_matches(
        self,
        kind: str = "",
        params: Params | None = None,
        *,
        videogame_title: str | None = None,
        max_pages: int | None = None,
        on_invalid: InvalidHandler | None = None,
    ) -> Iterator[PSMatch]:
        """Lazily iterate ``/{videogame}/matches[/{kind}]`` (kind: "", upcoming, running, past)."""
        path = f"{self._game}/matches" + (f"/{kind.strip('/')}" if kind else "")
        items = self.paginate(path, self._match_params(params, videogame_title), max_pages=max_pages)
        return self._typed(PSMatch, items, on_invalid)

    def iter_upcoming_matches(
        self,
        hours_ahead: int | None = None,
        *,
        videogame_title: str | None = None,
        max_pages: int | None = None,
        on_invalid: InvalidHandler | None = None,
        now: datetime | None = None,
    ) -> Iterator[PSMatch]:
        """Not-started matches, soonest first; limited to the next ``hours_ahead`` hours when given.

        ``now`` (default: the client's clock) anchors the ``hours_ahead`` window.
        """
        params: dict[str, Any] = {"sort": "begin_at"}
        if hours_ahead is not None:
            now = now or self._now()
            # one hour of slack backwards: a match that is a few minutes late is still "not_started"
            lo = format_timestamp(now - timedelta(hours=1))
            hi = format_timestamp(now + timedelta(hours=hours_ahead))
            params["range[begin_at]"] = f"{lo},{hi}"
        return self.iter_matches(
            "upcoming", params, videogame_title=videogame_title, max_pages=max_pages, on_invalid=on_invalid
        )

    def iter_past_matches(
        self,
        since: datetime,
        until: datetime | None = None,
        *,
        videogame_title: str | None = None,
        max_pages: int | None = None,
        on_invalid: InvalidHandler | None = None,
    ) -> Iterator[PSMatch]:
        """Finished matches that began in ``[since, until]`` (until defaults to now), oldest first."""
        hi = until or self._now()
        params = {
            "sort": "begin_at",
            "range[begin_at]": f"{format_timestamp(since)},{format_timestamp(hi)}",
        }
        return self.iter_matches(
            "past", params, videogame_title=videogame_title, max_pages=max_pages, on_invalid=on_invalid
        )

    def iter_modified_matches(
        self,
        since: datetime,
        until: datetime | None = None,
        *,
        videogame_title: str | None = None,
        max_pages: int | None = None,
        on_invalid: InvalidHandler | None = None,
    ) -> Iterator[PSMatch]:
        """Matches of any status modified in ``[since, until]``, oldest modification first."""
        hi = until or self._now()
        params = {
            "sort": "modified_at",
            "range[modified_at]": f"{format_timestamp(since)},{format_timestamp(hi)}",
        }
        return self.iter_matches(
            "", params, videogame_title=videogame_title, max_pages=max_pages, on_invalid=on_invalid
        )

    def upcoming_matches(
        self, hours_ahead: int | None = None, *, videogame_title: str | None = None
    ) -> list[PSMatch]:
        return list(self.iter_upcoming_matches(hours_ahead, videogame_title=videogame_title))

    def running_matches(self, *, videogame_title: str | None = None) -> list[PSMatch]:
        return list(self.iter_matches("running", {"sort": "begin_at"}, videogame_title=videogame_title))

    def past_matches(
        self, since: datetime, until: datetime | None = None, *, videogame_title: str | None = None
    ) -> list[PSMatch]:
        return list(self.iter_past_matches(since, until, videogame_title=videogame_title))

    def match(self, match_id: int | str) -> PSMatch:
        """One match with per-game ``rounds_score`` (no map names: see :meth:`games`).

        ``/csgo/matches/{id}`` needs a historical / real-time plan. On HTTP 403 the match is read from
        the list endpoint instead (``/csgo/matches?filter[id]=...``, available on every plan; numeric
        ids only), so reconciling single matches keeps working on the entry plan.
        """
        path = f"{self._game}/matches/{match_id}"
        numeric = str(match_id).isdigit()
        if not (numeric and self._single_match_forbidden):
            try:
                return self._one(PSMatch, path)
            except PandaScorePlanError:
                if not numeric:
                    raise
                self._single_match_forbidden = True  # do not ask again: go straight to the list
        raw = next(self.paginate(f"{self._game}/matches", {"filter[id]": str(match_id)}, max_pages=1), None)
        if raw is None:
            raise PandaScoreNotFoundError(f"GET /{path}: not found")
        try:
            return PSMatch.model_validate(raw)
        except ValidationError as exc:
            self.stats.invalid_items += 1
            raise PandaScoreResponseError(
                f"GET /{path}: malformed PSMatch ({exc.error_count()} validation errors)"
            ) from exc

    def games(self, match_id: int | str) -> list[PSGame]:
        """Games (maps) of a match incl. map, round history and player lines, ordered by position.

        Needs a plan with historical data; raises :class:`PandaScorePlanError` otherwise.
        """
        path = f"{self._game}/matches/{match_id}/games"
        games = list(self._typed(PSGame, self.paginate(path, page_size=MAX_PAGE_SIZE), None))
        return sorted(games, key=lambda g: (g.position is None, g.position or 0))

    def game(self, game_id: int) -> PSGame:
        return self._one(PSGame, f"{self._game}/games/{game_id}")

    def match_opponents(self, match_id: int | str) -> PSMatchOpponents:
        """Both teams of a match with their players (``/matches/{id}/opponents``)."""
        return self._one(PSMatchOpponents, f"matches/{match_id}/opponents")

    # ------------------------------------------------------------------ teams / tournaments

    def team(self, team_id: int | str) -> PSTeam:
        """One team with its current players (generic ``/teams/{id}``: there is no ``/csgo/teams/{id}``)."""
        return self._one(PSTeam, f"teams/{team_id}")

    def tournament(self, tournament_id: int | str) -> PSTournament:
        """One tournament stage with tier, type, teams and ``expected_roster``."""
        return self._one(PSTournament, f"tournaments/{tournament_id}")

    def tournament_rosters(self, tournament_id: int | str) -> PSTournamentRosters:
        """Rosters of every team registered for a tournament stage."""
        return self._one(PSTournamentRosters, f"tournaments/{tournament_id}/rosters")

    def _one[M: PSModel](self, model: type[M], path: str) -> M:
        data = self.get_json(path)
        if not isinstance(data, dict):
            raise PandaScoreResponseError(f"GET /{path}: expected a JSON object")
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            self.stats.invalid_items += 1
            raise PandaScoreResponseError(
                f"GET /{path}: malformed {model.__name__} ({exc.error_count()} validation errors)"
            ) from exc
