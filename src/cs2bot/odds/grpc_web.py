"""gRPC-web framing and schema-less protobuf decoding for Betclic's offering API.

Since ~2024 the Betclic web apps (betclic.fr / .pt / .pl ...) no longer fetch odds as JSON. The
Angular front-end talks **gRPC-web** (``Content-Type: application/grpc-web+proto``) to
``https://offering.begmedia.com/web/offering.access.api/offering.access.api.<Service>/<Method>``,
e.g. ``MatchService/GetMatchesBySportWithNotifications`` (listing), ``MatchService/
GetMatchWithNotification`` (one event; one *category* of markets per call) and
``SearchService/SearchMatchesWithNotifications``. Calls are server streams: the first data frame
is the snapshot, later frames are price notifications, so readers stop after the first frame.

There is no public ``.proto``; the field numbers below come from three independent open-source
clients that were checked against live traffic in 2025-2026 (TapsHTS/betclic-api,
tomasmourato/bettrackr ``lib/betclicGrpc.ts`` - "confirmed against offering.begmedia.com on
2026-09-19" - and czareq089/value-bets, a Polish project using ``X-Bg-Regulation: PL`` and
``language=pl``). They are *assumptions* to be confirmed on the owner's PC; every number lives in
:class:`BetclicProtoSchema` so it can be changed without touching code, and the raw bodies are
saved by the sources so the decoder can be re-fitted offline.

Known shapes (field numbers):

* request ``GetMatchesBySport``: 1 sport code (``"football"``), 2 language (``"pl"``), 4 offset;
  response: 1 -> {1 sport label, 3 repeated Match, 5 total count}; 40 matches per page.
* request ``GetMatch``: 1 match id (int64), 2 language, 3 category id (``"ca_ftb_rslt"``);
  response: 1 payload -> 1 Match.
* Match: 1 id, 2 name (``"A - B"``), 3 start (RFC 3339 UTC), 6 live flag, 8 competition
  {1 id, 2 name, 5 country}, 12 repeated contestant {3 name, 4 short name},
  markets in 9 and 25, sub-categories in 11 -> {3 (or 1) repeated Market}.
* Market: 1 id, 2/3 name, 9 status (3 = suspended); selections sit in layout containers that
  change with the UI (selection matrix, tabs, groups...), so they are found by *shape*.
* Selection: 1 id (int64), 12 odds (double, wire type 1), 15 betslip market id (int64),
  name in 10/11/2/3.  (The Market's own field 12 is an enum, so the double test is decisive.)
"""

from __future__ import annotations

import base64
import binascii
import re
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

GRPC_WEB_TRAILER_FLAG = 0x80
WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN = 2
WIRE_FIXED32 = 5

DEFAULT_GRPC_BASE_URL = "https://offering.begmedia.com/web/offering.access.api"
MATCH_SERVICE = "offering.access.api.MatchService"
METHOD_GET_MATCH = "GetMatchWithNotification"
METHOD_MATCHES_BY_SPORT = "GetMatchesBySportWithNotifications"

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_CATEGORY_RE = re.compile(r"^ca_[a-z0-9_]+$")
_CODE_LIKE_RE = re.compile(r"^(?:ca_|contestant:|mt_|mtc_)", re.IGNORECASE)


# --------------------------------------------------------------------------- frames


@dataclass(frozen=True)
class GrpcFrame:
    flags: int
    payload: bytes

    @property
    def is_trailer(self) -> bool:
        return bool(self.flags & GRPC_WEB_TRAILER_FLAG)

    @property
    def is_compressed(self) -> bool:
        return bool(self.flags & 0x01)


def decode_grpc_web_text(body: bytes | str) -> bytes:
    """Decode an ``application/grpc-web-text`` body (base64; streams concatenate padded chunks)."""
    text = body.decode("ascii", "ignore") if isinstance(body, bytes) else body
    text = re.sub(r"\s+", "", text)
    out = bytearray()
    for chunk in re.findall(r"[A-Za-z0-9+/]+={0,2}", text):
        try:
            out.extend(base64.b64decode(chunk + "=" * (-len(chunk) % 4)))
        except (binascii.Error, ValueError):
            break
    return bytes(out)


def split_frames(body: bytes, *, max_frames: int | None = None) -> list[GrpcFrame]:
    """Split a gRPC-web body into frames (1 flag byte + 4-byte big-endian length + payload).

    A truncated trailing frame (stream cut mid-way) is ignored instead of raising.
    """
    frames: list[GrpcFrame] = []
    pos = 0
    while pos + 5 <= len(body):
        flags = body[pos]
        length = int.from_bytes(body[pos + 1 : pos + 5], "big")
        start, end = pos + 5, pos + 5 + length
        if end > len(body):
            break
        frames.append(GrpcFrame(flags=flags, payload=body[start:end]))
        pos = end
        if max_frames is not None and len(frames) >= max_frames:
            break
    return frames


def body_frames(body: bytes, content_type: str | None = None) -> list[GrpcFrame]:
    """Frames of a response body, handling the base64 ``grpc-web-text`` variant."""
    if content_type and "grpc-web-text" in content_type.lower():
        body = decode_grpc_web_text(body)
    return split_frames(body)


def parse_trailers(payload: bytes) -> dict[str, str]:
    """``grpc-status:0\\r\\ngrpc-message:...`` -> dict (lower-case keys)."""
    out: dict[str, str] = {}
    for line in payload.decode("utf-8", "replace").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip().lower()] = value.strip()
    return out


def grpc_web_frame(payload: bytes) -> bytes:
    """Wrap a protobuf message in an uncompressed gRPC-web data frame."""
    return b"\x00" + struct.pack(">I", len(payload)) + payload


# --------------------------------------------------------------------------- protobuf encoding


def encode_varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1  # int64 two's complement, as protobuf does
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def field_varint(number: int, value: int) -> bytes:
    return encode_varint(number << 3 | WIRE_VARINT) + encode_varint(value)


def field_bytes(number: int, value: str | bytes) -> bytes:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return encode_varint(number << 3 | WIRE_LEN) + encode_varint(len(data)) + data


def get_match_request(match_id: int, language: str, category_id: str | None = None) -> bytes:
    """``GetMatchRequest {match_id = 1; language = 2; category_id = 3}`` (unframed)."""
    msg = field_varint(1, match_id) + field_bytes(2, language)
    if category_id:
        msg += field_bytes(3, category_id)
    return msg


def matches_by_sport_request(
    sport_code: str, language: str, offset: int = 0, limit: int | None = None
) -> bytes:
    """``GetMatchesBySport {sport_code = 1; language = 2; offset = 4; limit = 5}`` (unframed)."""
    msg = field_bytes(1, sport_code) + field_bytes(2, language)
    if offset > 0:
        msg += field_varint(4, offset)
    if limit:
        msg += field_varint(5, limit)
    return msg


# --------------------------------------------------------------------------- protobuf decoding


@dataclass(frozen=True)
class ProtoField:
    number: int
    wire: int
    value: int | bytes  # varint -> int; fixed64 / fixed32 / length-delimited -> bytes


def _read_varint(buf: bytes, pos: int) -> tuple[int, int] | None:
    result = 0
    shift = 0
    for _ in range(10):
        if pos >= len(buf):
            return None
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
    return None


def decode_fields(buf: bytes) -> list[ProtoField] | None:
    """Strictly decode one protobuf message; ``None`` if ``buf`` is not a valid message."""
    fields: list[ProtoField] = []
    pos = 0
    while pos < len(buf):
        tag_pos = _read_varint(buf, pos)
        if tag_pos is None:
            return None
        tag, pos = tag_pos
        number, wire = tag >> 3, tag & 0x07
        if number == 0 or number > 0x1FFFFFFF:
            return None
        if wire == WIRE_VARINT:
            val_pos = _read_varint(buf, pos)
            if val_pos is None:
                return None
            value, pos = val_pos
            fields.append(ProtoField(number, wire, value))
        elif wire == WIRE_FIXED64:
            if pos + 8 > len(buf):
                return None
            fields.append(ProtoField(number, wire, buf[pos : pos + 8]))
            pos += 8
        elif wire == WIRE_LEN:
            len_pos = _read_varint(buf, pos)
            if len_pos is None:
                return None
            length, pos = len_pos
            if pos + length > len(buf):
                return None
            fields.append(ProtoField(number, wire, buf[pos : pos + length]))
            pos += length
        elif wire == WIRE_FIXED32:
            if pos + 4 > len(buf):
                return None
            fields.append(ProtoField(number, wire, buf[pos : pos + 4]))
            pos += 4
        else:  # groups (3/4) do not exist in proto3; 6/7 are invalid
            return None
    return fields


def as_text(raw: bytes) -> str | None:
    """Printable UTF-8 text, else ``None`` (protobuf messages almost always contain control bytes)."""
    if not raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(not (ch.isprintable() or ch in "\t\n\r") for ch in text):
        return None
    return text


class ProtoMessage:
    """Thin accessor over decoded fields (no schema needed)."""

    __slots__ = ("fields",)

    def __init__(self, fields: list[ProtoField]) -> None:
        self.fields = fields

    @classmethod
    def parse(cls, buf: bytes) -> ProtoMessage | None:
        decoded = decode_fields(buf)
        return cls(decoded) if decoded is not None else None

    def varint(self, number: int) -> int | None:
        for f in self.fields:
            if f.number == number and f.wire == WIRE_VARINT and isinstance(f.value, int):
                return f.value
        return None

    def raw(self, number: int) -> list[bytes]:
        return [
            f.value
            for f in self.fields
            if f.number == number and f.wire == WIRE_LEN and isinstance(f.value, bytes)
        ]

    def string(self, *numbers: int) -> str | None:
        """First printable string among ``numbers`` (in the given priority order)."""
        for number in numbers:
            for raw in self.raw(number):
                text = as_text(raw)
                if text is not None and text.strip():
                    return text.strip()
        return None

    def strings(self) -> list[tuple[int, str]]:
        out: list[tuple[int, str]] = []
        for f in self.fields:
            if f.wire == WIRE_LEN and isinstance(f.value, bytes):
                text = as_text(f.value)
                if text is not None and text.strip():
                    out.append((f.number, text.strip()))
        return out

    def double(self, number: int) -> float | None:
        for f in self.fields:
            if f.number == number and f.wire == WIRE_FIXED64 and isinstance(f.value, bytes):
                value: float = struct.unpack("<d", f.value)[0]
                return value
        return None

    def messages(self, number: int) -> list[ProtoMessage]:
        out: list[ProtoMessage] = []
        for raw in self.raw(number):
            if as_text(raw) is not None:
                continue
            msg = ProtoMessage.parse(raw)
            if msg is not None and msg.fields:
                out.append(msg)
        return out

    def children(self) -> Iterator[tuple[int, ProtoMessage]]:
        """Every length-delimited field that decodes as a non-empty message (text excluded)."""
        for f in self.fields:
            if f.wire != WIRE_LEN or not isinstance(f.value, bytes) or as_text(f.value) is not None:
                continue
            msg = ProtoMessage.parse(f.value)
            if msg is not None and msg.fields:
                yield f.number, msg


def proto_debug_tree(buf: bytes, *, max_depth: int = 12) -> dict[str, list[Any]] | None:
    """Human-readable nested view ``{"<field>": [values]}`` of an unknown message, for fitting.

    Strings stay strings, varints ints, fixed64 values are shown as ``{"double": .., "hex": ..}``.
    """

    def walk(raw: bytes, depth: int) -> dict[str, list[Any]] | None:
        msg = ProtoMessage.parse(raw)
        if msg is None:
            return None
        out: dict[str, list[Any]] = {}
        for f in msg.fields:
            value: Any
            if f.wire == WIRE_VARINT:
                value = f.value
            elif f.wire == WIRE_FIXED64 and isinstance(f.value, bytes):
                value = {"double": struct.unpack("<d", f.value)[0], "hex": f.value.hex()}
            elif f.wire == WIRE_FIXED32 and isinstance(f.value, bytes):
                value = {"float": struct.unpack("<f", f.value)[0], "hex": f.value.hex()}
            elif isinstance(f.value, bytes):
                text = as_text(f.value)
                nested = walk(f.value, depth + 1) if text is None and depth < max_depth else None
                value = text if text is not None else nested if nested is not None else f.value.hex()
            else:
                value = f.value
            out.setdefault(str(f.number), []).append(value)
        return out

    return walk(buf, 0)


# --------------------------------------------------------------------------- Betclic schema


@dataclass(frozen=True)
class BetclicProtoSchema:
    """Field numbers of Betclic's offering messages (see module docstring; all assumptions)."""

    match_id: int = 1
    match_name: int = 2
    match_date: int = 3
    match_live: tuple[int, ...] = (6,)
    match_competition: int = 8
    competition_name: int = 2
    match_contestants: int = 12
    contestant_names: tuple[int, ...] = (3, 2, 4)
    match_markets: tuple[int, ...] = (9, 25)
    match_market_groups: tuple[int, ...] = (11,)
    group_markets: tuple[int, ...] = (3, 1)
    market_id: int = 1
    market_names: tuple[int, ...] = (2, 3)
    market_status: int = 9
    market_suspended_values: tuple[int, ...] = (3,)
    selection_id: int = 1
    selection_odds: int = 12
    selection_market_id: int = 15
    selection_names: tuple[int, ...] = (10, 11, 2, 3)
    max_odds: float = 10_000.0
    max_depth: int = 16


DEFAULT_SCHEMA = BetclicProtoSchema()


@dataclass
class _MatchSearch:
    schema: BetclicProtoSchema
    matches: list[dict[str, Any]] = field(default_factory=list)


def _selection_json(msg: ProtoMessage, schema: BetclicProtoSchema) -> dict[str, Any] | None:
    sel_id = msg.varint(schema.selection_id)
    odds = msg.double(schema.selection_odds)
    if not sel_id or odds is None or not (1.0 < odds <= schema.max_odds):
        return None
    name = msg.string(*schema.selection_names)
    if name is None:  # unknown layout: fall back to the first printable string field
        strings = [s for _, s in msg.strings() if not _CODE_LIKE_RE.match(s)]
        name = strings[0] if strings else None
    if name is None:
        return None
    out: dict[str, Any] = {"id": str(sel_id), "name": name, "odds": repr(round(odds, 6))}
    market_id = msg.varint(schema.selection_market_id)
    if market_id:
        out["betslipMarketId"] = str(market_id)
    return out


def _find_selections(
    msg: ProtoMessage, schema: BetclicProtoSchema, depth: int, seen: set[str]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if depth > schema.max_depth:
        return out
    for _, child in msg.children():
        sel = _selection_json(child, schema)
        if sel is not None:
            if sel["id"] not in seen:
                seen.add(sel["id"])
                out.append(sel)
            continue
        out.extend(_find_selections(child, schema, depth + 1, seen))
    return out


def _market_json(msg: ProtoMessage, schema: BetclicProtoSchema, seen: set[str]) -> dict[str, Any] | None:
    name = None
    for number in schema.market_names:
        candidate = msg.string(number)
        if candidate and len(candidate) >= 2 and not _CODE_LIKE_RE.match(candidate):
            name = candidate
            break
    if name is None:
        return None
    selections = _find_selections(msg, schema, 0, seen)
    if not selections:
        return None
    market: dict[str, Any] = {"name": name, "selections": selections}
    market_id = msg.varint(schema.market_id)
    if market_id:
        market["id"] = str(market_id)
    if msg.varint(schema.market_status) in schema.market_suspended_values:
        market["isSuspended"] = True
    return market


def _category_ids(msg: ProtoMessage, depth: int, out: list[str]) -> None:
    if depth > 8:
        return
    for _, text in msg.strings():
        if _CATEGORY_RE.match(text) and text not in out:
            out.append(text)
    for _, child in msg.children():
        _category_ids(child, depth + 1, out)


def _match_json(msg: ProtoMessage, schema: BetclicProtoSchema) -> dict[str, Any] | None:
    match_id = msg.varint(schema.match_id)
    date = msg.string(schema.match_date)
    if not match_id or date is None or not _ISO_RE.match(date):
        return None
    contestants: list[str] = []
    for c in msg.messages(schema.match_contestants):
        name = c.string(*schema.contestant_names)
        if name:
            contestants.append(name)
    name = msg.string(schema.match_name)
    if len(contestants) < 2 and not (name and re.search(r"\s[-–]\s|\svs\.?\s", name)):
        return None
    out: dict[str, Any] = {"id": str(match_id), "date": date}
    if name:
        out["name"] = name
    if len(contestants) >= 2:
        out["contestants"] = [{"name": c} for c in contestants]
    out["isLive"] = any(msg.varint(n) == 1 for n in schema.match_live)
    competition = msg.messages(schema.match_competition)
    if competition:
        comp_name = competition[0].string(schema.competition_name)
        if comp_name:
            out["competition"] = {"name": comp_name}
    seen: set[str] = set()
    market_msgs: list[ProtoMessage] = []
    for number in schema.match_markets:
        market_msgs.extend(msg.messages(number))
    for number in schema.match_market_groups:
        for group in msg.messages(number):
            for inner in schema.group_markets:
                market_msgs.extend(group.messages(inner))
    markets = [m for m in (_market_json(mm, schema, seen) for mm in market_msgs) if m is not None]
    out["markets"] = markets
    categories: list[str] = []
    _category_ids(msg, 0, categories)
    if categories:
        out["categoryIds"] = categories
    return out


def _search_matches(msg: ProtoMessage, search: _MatchSearch, depth: int) -> None:
    if depth > search.schema.max_depth:
        return
    as_match = _match_json(msg, search.schema)
    if as_match is not None:
        search.matches.append(as_match)
        return
    for _, child in msg.children():
        _search_matches(child, search, depth + 1)


def decode_betclic_message(payload: bytes, schema: BetclicProtoSchema | None = None) -> list[dict[str, Any]]:
    """All Match messages found anywhere in one protobuf message, converted to JSON-like dicts.

    The dicts use key names that :func:`cs2bot.odds.payload_parser.extract_events` understands
    (``id``, ``name``, ``date``, ``isLive``, ``competition``, ``contestants``, ``markets`` ->
    ``selections`` with ``odds`` and ``betslipMarketId``) plus ``categoryIds`` (``ca_*`` strings).
    """
    msg = ProtoMessage.parse(payload)
    if msg is None:
        return []
    search = _MatchSearch(schema=schema or DEFAULT_SCHEMA)
    _search_matches(msg, search, 0)
    return search.matches


def decode_betclic_grpc_body(
    body: bytes,
    *,
    content_type: str | None = None,
    schema: BetclicProtoSchema | None = None,
    max_frames: int | None = 1,
) -> dict[str, Any]:
    """Decode a captured gRPC-web response body into ``{"matches": [...], "grpcStatus": ...}``.

    Only the first ``max_frames`` data frames are used by default: later frames of these
    server streams are incremental price notifications, not full snapshots.
    """
    frames = body_frames(body, content_type)
    matches: list[dict[str, Any]] = []
    status: str | None = None
    used = 0
    for frame in frames:
        if frame.is_trailer:
            status = parse_trailers(frame.payload).get("grpc-status", status)
            continue
        if frame.is_compressed:
            continue
        if max_frames is not None and used >= max_frames:
            continue
        used += 1
        matches.extend(decode_betclic_message(frame.payload, schema))
    out: dict[str, Any] = {"matches": matches}
    if status is not None:
        out["grpcStatus"] = status
    return out


def grpc_listing_total(body: bytes, content_type: str | None = None) -> int | None:
    """``GetMatchesBySport`` responses carry the total match count in ``response(1).field 5``."""
    for frame in body_frames(body, content_type):
        if frame.is_trailer or frame.is_compressed:
            continue
        root = ProtoMessage.parse(frame.payload)
        if root is None:
            continue
        for response in root.messages(1):
            total = response.varint(5)
            if total is not None:
                return total
    return None


def betclic_grpc_headers(
    base_url: str, country_code: str, language: str, *, browser: bool = False
) -> dict[str, str]:
    """Request headers Betclic's own front-end sends to the offering gRPC-web API.

    ``browser=True`` leaves out what a page's ``fetch`` may not set itself (Origin, Referer).
    """
    zone = country_code.upper()
    headers = {
        "Content-Type": "application/grpc-web+proto",
        "Accept": "*/*",
        "X-Grpc-Web": "1",
        "X-Bg-Ref-Brand": "BETCLIC",
        "X-Bg-Ref-Platform": "DESKTOP",
        "X-Bg-Regulation": zone,
        "X-Bg-Ref-Regulator-Zone": zone,
    }
    if not browser:
        origin = base_url.rstrip("/")
        headers |= {
            "Origin": origin,
            "Referer": origin + "/",
            "Accept-Language": f"{language}-{zone},{language};q=0.9",
        }
    return headers


def looks_like_grpc_web(body: bytes, content_type: str | None = None) -> bool:
    """Cheap sniff: declared grpc content type, or a body made of well-formed frames."""
    if content_type and "grpc" in content_type.lower():
        return True
    frames = split_frames(body)
    if not frames:
        return False
    consumed = sum(5 + len(f.payload) for f in frames)
    return consumed == len(body) and all(f.flags in (0x00, 0x01, 0x80, 0x81) for f in frames)
