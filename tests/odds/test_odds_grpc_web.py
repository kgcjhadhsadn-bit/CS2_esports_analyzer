from __future__ import annotations

import base64
import struct

import pytest

from cs2bot.odds.grpc_web import (
    BetclicProtoSchema,
    ProtoMessage,
    decode_betclic_grpc_body,
    decode_betclic_message,
    decode_fields,
    decode_grpc_web_text,
    encode_varint,
    field_bytes,
    field_varint,
    get_match_request,
    grpc_web_frame,
    looks_like_grpc_web,
    matches_by_sport_request,
    parse_trailers,
    proto_debug_tree,
    split_frames,
)
from tests.odds.odds_testkit import (
    fixed64,
    grpc_response,
    proto_market,
    proto_match,
    proto_selection,
    sample_grpc_match,
)


def test_get_match_request_matches_bytes_confirmed_against_live_server() -> None:
    # tomasmourato/bettrackr test: confirmed against offering.begmedia.com on 2026-09-19
    body = grpc_web_frame(get_match_request(1218287014801408, "pt", "ca_ftb_rslt"))
    assert body.hex() == "000000001a0880e081e6e8809502120270741a0b63615f6674625f72736c74"


def test_matches_by_sport_request_layout() -> None:
    msg = matches_by_sport_request("counter_strike", "pl", offset=40)
    fields = decode_fields(msg)
    assert fields is not None
    assert [(f.number, f.value) for f in fields] == [(1, b"counter_strike"), (2, b"pl"), (4, 40)]
    assert decode_fields(matches_by_sport_request("football", "pl")) is not None


def test_varint_roundtrip_and_negative_int64() -> None:
    for value in (0, 1, 127, 128, 300, 2**35, 1218287014801408):
        fields = decode_fields(field_varint(1, value))
        assert fields is not None and fields[0].value == value
    assert len(encode_varint(-1)) == 10  # two's complement int64


def test_split_frames_ignores_truncated_tail_and_reads_trailers() -> None:
    body = grpc_response(b"\x08\x01")
    frames = split_frames(body)
    assert [f.is_trailer for f in frames] == [False, True]
    assert parse_trailers(frames[1].payload) == {"grpc-status": "0", "grpc-message": ""}
    assert split_frames(body[:-3])[-1].is_trailer is False  # cut trailer frame dropped
    assert split_frames(b"\x00\x00\x00") == []


def test_grpc_web_text_variant() -> None:
    raw = grpc_web_frame(b"\x08\x05")
    trailer = b"\x80" + struct.pack(">I", 14) + b"grpc-status:0\r\n"
    text = base64.b64encode(raw) + base64.b64encode(trailer)  # two padded chunks, concatenated
    assert decode_grpc_web_text(text) == raw + trailer


def test_decode_fields_is_strict() -> None:
    assert decode_fields(b"\x08") is None  # truncated varint
    assert decode_fields(b"\x12\x05ab") is None  # length beyond buffer
    assert decode_fields(b"\x0b") is None  # wire type 3 (group)
    assert decode_fields(b"\x00\x01") is None  # field number 0
    assert decode_fields(b"") == []


def test_proto_message_accessors() -> None:
    msg = ProtoMessage.parse(field_varint(1, 7) + field_bytes(2, "Mapa 1 - liczba rund") + fixed64(12, 1.85))
    assert msg is not None
    assert msg.varint(1) == 7
    assert msg.string(3, 2) == "Mapa 1 - liczba rund"
    assert msg.double(12) == pytest.approx(1.85)
    assert msg.double(1) is None  # varint, not fixed64


def test_decode_betclic_body_finds_match_markets_and_categories() -> None:
    decoded = decode_betclic_grpc_body(
        grpc_response(sample_grpc_match()), content_type="application/grpc-web+proto"
    )
    assert decoded["grpcStatus"] == "0"
    (match,) = decoded["matches"]
    assert match["id"] == "1001234567"
    assert match["date"] == "2026-11-23T17:00:00Z"
    assert [c["name"] for c in match["contestants"]] == ["Natus Vincere", "FaZe Clan"]
    assert match["competition"] == {"name": "Counter-Strike 2 - IEM Chengdu"}
    assert match["isLive"] is False
    assert match["categoryIds"] == ["ca_esp_top", "ca_esp_maps"]
    names = {m["name"]: m for m in match["markets"]}
    assert set(names) == {"Zwycięzca meczu", "Mapa 1 - liczba rund", "Dogrywka - mapa 1"}
    assert names["Dogrywka - mapa 1"].get("isSuspended") is True
    rounds = names["Mapa 1 - liczba rund"]["selections"]
    assert {(s["name"], s["betslipMarketId"]) for s in rounds} == {
        ("Powyżej 21,5", "80021"),
        ("Poniżej 21,5", "80021"),
        ("Powyżej 23,5", "80022"),
        ("Poniżej 23,5", "80022"),
    }


def test_only_first_data_frame_is_used_by_default() -> None:
    later = field_bytes(1, field_bytes(1, sample_grpc_match(match_id=555)))
    body = grpc_response(sample_grpc_match(), extra_frames=[later])
    assert [m["id"] for m in decode_betclic_grpc_body(body)["matches"]] == ["1001234567"]
    both = decode_betclic_grpc_body(body, max_frames=None)["matches"]
    assert sorted(m["id"] for m in both) == ["1001234567", "555"]


def test_live_flag_and_unnamed_selection_fallback() -> None:
    odd_layout = field_varint(1, 77) + field_bytes(4, "Natus Vincere") + fixed64(12, 1.5)  # name in field 4
    market = proto_market(1, "Zwycięzca meczu", [odd_layout])
    match = proto_match(9, "A - B", "2026-11-23T17:00:00Z", ("A", "B"), "CS2", [market], live=True)
    (decoded,) = decode_betclic_message(field_bytes(1, match))
    assert decoded["isLive"] is True
    assert decoded["markets"][0]["selections"][0]["name"] == "Natus Vincere"


def test_schema_field_numbers_can_be_overridden() -> None:
    sel = field_varint(1, 1) + field_bytes(10, "A") + fixed64(13, 2.0)  # odds moved to field 13
    market = proto_market(1, "Wynik", [sel])
    match = proto_match(9, "A - B", "2026-11-23T17:00:00Z", ("A", "B"), "CS2", [market])
    assert decode_betclic_message(match)[0]["markets"] == []
    schema = BetclicProtoSchema(selection_odds=13)
    assert decode_betclic_message(match, schema)[0]["markets"][0]["selections"][0]["odds"] == "2.0"


@pytest.mark.parametrize(
    "body",
    [b"", b"<html>403</html>", b"\x00\x00\x00\x00\x05\x08", grpc_response(b"\xff\xff\xff"), b"\x01" * 50],
)
def test_garbage_bodies_decode_to_nothing(body: bytes) -> None:
    assert decode_betclic_grpc_body(body)["matches"] == []


def test_compressed_frames_are_skipped() -> None:
    match = field_bytes(1, field_bytes(1, sample_grpc_match()))
    body = b"\x01" + struct.pack(">I", len(match)) + match
    assert decode_betclic_grpc_body(body)["matches"] == []


def test_looks_like_grpc_web() -> None:
    assert looks_like_grpc_web(grpc_response(b"\x08\x01"))
    assert looks_like_grpc_web(b"", "application/grpc-web+proto")
    assert not looks_like_grpc_web(b'{"a": 1}')


def test_proto_debug_tree_for_offline_fitting() -> None:
    tree = proto_debug_tree(proto_selection(5, "Tak", 1.9, 77))
    assert tree is not None
    assert tree["1"] == [5] and tree["10"] == ["Tak"] and tree["15"] == [77]
    assert tree["12"][0]["double"] == pytest.approx(1.9)
    assert proto_debug_tree(b"\x0b") is None
