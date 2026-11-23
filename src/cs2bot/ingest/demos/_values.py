"""Defensive scalar coercion for values coming out of demoparser2 DataFrames.

demoparser2 returns pandas DataFrames whose cells may be numpy scalars, ``None``, ``NaN``, ``pd.NA`` or
strings, depending on the event and on the parser version (for example steam ids are strings in event
frames but unsigned integers in tick frames). Everything in :mod:`cs2bot.ingest.demos.parse` goes
through these helpers so that one odd cell never aborts a whole demo.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd


def is_missing(value: Any) -> bool:
    """True for ``None`` / ``NaN`` / ``pd.NA`` / ``NaT`` (containers and strings are never missing)."""
    if value is None:
        return True
    if isinstance(value, str | bytes | list | tuple | dict | set):
        return False
    if isinstance(value, float):
        return math.isnan(value)
    try:
        result = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return bool(result) if isinstance(result, bool) or getattr(result, "ndim", 1) == 0 else False


def to_int(value: Any) -> int | None:
    if is_missing(value) or isinstance(value, list | tuple | dict | set):
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            return int(text) if text.lstrip("+-").isdigit() else int(float(text))
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def to_float(value: Any) -> float | None:
    if is_missing(value) or isinstance(value, list | tuple | dict | set):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def to_bool(value: Any) -> bool | None:
    if is_missing(value) or isinstance(value, list | tuple | dict | set):
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes"):
            return True
        if text in ("false", "0", "no", ""):
            return False
        return None
    try:
        return bool(value)
    except (TypeError, ValueError):
        return None


def to_str(value: Any) -> str | None:
    """Stripped text, ``None`` for missing / empty values."""
    if is_missing(value) or isinstance(value, list | tuple | dict | set):
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value).strip()
    return text or None


def to_steamid(value: Any) -> int | None:
    """64-bit steam id as ``int``; ``None`` for missing ids, ``0`` (bots / world) and garbage.

    Strings are parsed exactly. Floats are accepted but may already have lost precision upstream
    (a float64 cannot represent every 17-digit id), which is why demoparser2 uses strings/uint64.
    """
    if is_missing(value) or isinstance(value, bool | list | tuple | dict | set):
        return None
    try:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            steamid = int(text) if text.isdigit() else int(float(text))
        else:
            steamid = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return steamid if steamid > 0 else None


def records(frame: Any) -> list[dict[str, Any]]:
    """Rows of a DataFrame as dicts (empty list for ``None`` / empty / non-DataFrame input)."""
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    rows: list[dict[str, Any]] = frame.to_dict("records")
    return rows
