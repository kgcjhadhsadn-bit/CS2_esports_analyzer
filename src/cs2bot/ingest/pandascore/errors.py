"""Exceptions raised by the PandaScore client.

Messages never contain the API token: it only travels in the ``Authorization`` header and is never
interpolated into an exception, a log line or a ``repr``.
"""

from __future__ import annotations


class PandaScoreError(Exception):
    """Base class for everything this package raises."""


class PandaScoreAuthError(PandaScoreError):
    """No API token configured, or PandaScore rejected it (HTTP 401)."""


class PandaScorePlanError(PandaScoreError):
    """HTTP 403: the endpoint is not part of the subscribed PandaScore plan."""


class PandaScoreNotFoundError(PandaScoreError):
    """HTTP 404: the resource does not exist (any more)."""


class PandaScoreHTTPError(PandaScoreError):
    """Any other non-success HTTP status (after retries for transient ones)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PandaScoreRateLimitError(PandaScoreHTTPError):
    """HTTP 429 that did not clear within the retry budget; ``retry_after_s`` when the API said so."""

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class PandaScoreTransportError(PandaScoreError):
    """Connection / timeout problems that survived the retries."""


class PandaScoreResponseError(PandaScoreError):
    """The response body is not the JSON shape the endpoint documents."""
