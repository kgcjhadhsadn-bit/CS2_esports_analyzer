"""Query-parameter helpers shared by the routes."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from fastapi import HTTPException, Query

from cs2bot.utils.time import ensure_utc

# ``from`` is a Python keyword, so the parameter is declared with an alias.
FromParam = Annotated[
    datetime | None,
    Query(alias="from", description="Lower bound (inclusive), ISO-8601; no offset = UTC"),
]
ToParam = Annotated[datetime | None, Query(description="Upper bound (inclusive), ISO-8601; no offset = UTC")]
ListParam = Annotated[
    list[str] | None, Query(description="Repeat the parameter or separate values with commas")
]


def as_utc(value: datetime | None) -> datetime | None:
    """Query datetimes without an offset are interpreted as UTC."""
    return ensure_utc(value) if value is not None else None


def validation_error(loc: list[str | int], msg: str, kind: str = "value_error") -> HTTPException:
    """422 with the same body shape FastAPI uses for its own validation errors."""
    detail: list[dict[str, Any]] = [{"loc": loc, "msg": msg, "type": kind}]
    return HTTPException(status_code=422, detail=detail)


def parse_enum_list[E: StrEnum](values: list[str] | None, enum_cls: type[E], name: str) -> list[E]:
    """Accept repeated (``?status=a&status=b``) and comma separated (``?status=a,b``) values."""
    out: list[E] = []
    allowed = ", ".join(m.value for m in enum_cls)
    for raw in values or []:
        for part in raw.split(","):
            token = part.strip().lower()
            if not token:
                continue
            try:
                out.append(enum_cls(token))
            except ValueError:
                raise validation_error(
                    ["query", name], f"invalid value {token!r}; allowed: {allowed}", "enum"
                ) from None
    return out
