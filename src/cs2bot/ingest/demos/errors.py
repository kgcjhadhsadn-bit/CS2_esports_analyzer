"""Exceptions raised by the demo pipeline (download -> extract -> parse -> persist)."""

from __future__ import annotations


class DemoError(Exception):
    """Base class for every demo-pipeline error."""


class DemoDownloadError(DemoError):
    """The archive could not be downloaded (HTTP error, challenge page, size limit, empty file)."""


class DemoDownloadBlockedError(DemoDownloadError):
    """The source answered with a challenge / block page: stop downloading and try again later."""


class ArchiveError(DemoError):
    """The archive is corrupt, of an unknown type or contains no ``.dem`` file."""


class ArchiveToolMissingError(ArchiveError):
    """A ``.rar`` archive needs an external extraction tool and none was found on this machine."""


class DemoParseError(DemoError):
    """demoparser2 failed on the file or the demo contains no usable match rounds."""


class DemoPersistError(DemoError):
    """Parsed data cannot be stored (unknown map row, or the team orientation is undeterminable)."""
