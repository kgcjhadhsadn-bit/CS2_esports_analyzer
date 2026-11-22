"""Exceptions raised by the HLTV fetcher, parsers and sync."""

from __future__ import annotations


class HLTVError(Exception):
    """Base class for every HLTV ingestion error."""


class HLTVFetchError(HLTVError):
    """A page could not be fetched (network error, unexpected HTTP status)."""

    def __init__(self, message: str, *, url: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status = status


class HLTVNotFoundError(HLTVFetchError):
    """HLTV answered 404/410 for the page (deleted match, wrong id)."""


class HLTVBlockedError(HLTVFetchError):
    """A Cloudflare challenge / block page did not clear, or HLTV rate-limited us.

    The fetcher never tries to solve challenges: it raises this error and refuses further network
    fetches until ``retry_after_s`` seconds have passed (exponential cool-down, persisted on disk).
    """

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        status: int | None = None,
        reason: str | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message, url=url, status=status)
        self.reason = reason
        self.retry_after_s = retry_after_s


class HLTVParseError(HLTVError, ValueError):
    """The HTML is not the expected page type (or a mandatory element is missing)."""
