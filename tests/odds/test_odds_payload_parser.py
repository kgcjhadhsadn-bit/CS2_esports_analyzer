"""Generic payload walker against several plausible Betclic payload shapes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from cs2bot.domain.odds import CollectedEvent
from cs2bot.odds.grpc_web import decode_betclic_grpc_body
from cs2bot.odds.payload_parser import (
    PayloadKeys,
    event_id_from_url,
    extract_event_records,
    extract_events,
    extract_json_documents,
    find_match_categories,
    find_request_headers,
    merge_parsed_events,
)
from tests.odds.odds_testkit import FIXTURES, grpc_response, sample_grpc_match

CS2 = r"counter[\s\-_]*strike|\bcs\s*-?\s*2\b|\bcs2\b"


def _by_id(events: list[CollectedEvent]) -> dict[str, CollectedEvent]:
    return {e.external_id: e for e in events}


def _markets(event: CollectedEvent) -> dict[tuple[str, str], dict[str, tuple[Decimal, bool]]]:
    return {
        (m.raw_name, m.external_id): {s.raw_name: (s.odds, s.is_available) for s in m.selections}
        for m in event.markets
    }


# --------------------------------------------------------------------------- shape 1: begmedia REST


def test_begmedia_offering_api_shape() -> None:
    payload = json.loads((FIXTURES / "begmedia_v4_events.json").read_text(encoding="utf-8"))
    events = _by_id(extract_events([payload], source_url="https://www.betclic.pl/e-sport-s102"))
    assert set(events) == {"1001234567", "1001234999"}  # the 3-contestant outright is skipped

    navi = events["1001234567"]
    assert (navi.team1_raw, navi.team2_raw) == ("Natus Vincere", "FaZe Clan")
    assert navi.starts_at == datetime(2026, 11, 23, 17, tzinfo=UTC)
    assert navi.competition_raw == "Counter-Strike 2 - IEM Chengdu"
    assert navi.is_live is False
    assert navi.url == (
        "https://www.betclic.pl/e-sport-s102/counter-strike-2-iem-chengdu-c9001/natus-vincere-faze-clan-m1001234567"
    )
    markets = _markets(navi)
    assert markets[("Zwycięzca meczu", "50001")] == {
        "Natus Vincere": (Decimal("1.62"), True),
        "FaZe Clan": (Decimal("2.25"), True),
    }
    # Polish decimal strings
    assert markets[("Liczba map", "50003")]["Powyżej 2,5"] == (Decimal("2.10"), True)
    # suspended market -> unavailable selections; raw names keep their Polish letters
    assert markets[("Mapa 1 - zwycięzca", "50004")]["FaZe Clan"] == (Decimal("2.40"), False)
    score = markets[("Dokładny wynik", "50005")]
    assert score["1:2"][1] is False  # status "suspended" on the selection
    assert "broken" not in score  # unparseable odds dropped, the rest of the market kept

    g2 = events["1001234999"]
    assert g2.is_live is True
    assert g2.starts_at == datetime(2026, 11, 24, 19, tzinfo=UTC)  # epoch milliseconds


# --------------------------------------------------------------------------- shape 2: Angular ng-state


def test_angular_transfer_state_shape() -> None:
    html = (FIXTURES / "betclic_event_page.html").read_text(encoding="utf-8")
    docs = extract_json_documents(html)
    assert len(docs) >= 2  # JSON-LD + ng-state (+ the window.__APP_CONFIG__ assignment)
    page_url = "https://www.betclic.pl/e-sport-s102/cs2-iem-chengdu-c9001/natus-vincere-faze-clan-m1001234567"
    events = _by_id(extract_events(docs, source_url=page_url))
    assert set(events) == {"1001234567", "2002002"}  # the football recommendation is an event too ...
    only_cs2 = extract_events(docs, source_url=page_url, sport_pattern=CS2)
    assert [e.external_id for e in only_cs2] == ["1001234567"]  # ... until filtered by sport

    navi = events["1001234567"]
    assert navi.url == page_url
    assert navi.starts_at == datetime(2026, 11, 23, 17, tzinfo=UTC)
    markets = _markets(navi)
    # "topMarkets" duplicate merged into one market (same betslip id), the later prices win
    winners = [k for k in markets if k[0] == "Zwycięzca meczu"]
    assert winners == [("Zwycięzca meczu", "1218288612810001")]
    assert markets[winners[0]]["Natus Vincere"] == (Decimal("1.62"), True)
    # one selectionMatrix card with two lines -> two markets keyed by betslipMarketId
    assert markets[("Mapa 1 - liczba rund", "1218288612810011")] == {
        "Powyżej 21,5": (Decimal("1.85"), True),
        "Poniżej 21,5": (Decimal("1.95"), True),
    }
    assert ("Mapa 1 - liczba rund", "1218288612810012") in markets
    # market inside a "Mapa 2" group gets the group name as prefix
    assert ("Mapa 2 - Zwycięzca", "1218288612810021") in markets
    # SUSPENDED status propagates to the selections
    assert all(not avail for _, avail in markets[("Handicap rund - mapa 1", "1218288612810031")].values())
    # named matrix rows ("Powyżej"/"Poniżej") do not become market names
    assert set(markets[("Zabójstwa gracza - mapa 1", "1218288612810042")]) == {
        "ropz Powyżej 16,5",
        "ropz Poniżej 16,5",
    }
    # market published outside the match node but carrying its matchId is attached
    assert markets[("Dogrywka - mapa 1", "1218288612810051")]["Tak"] == (Decimal("4.75"), True)

    assert find_match_categories(docs, "1001234567") == ["ca_esp_top", "ca_esp_maps", "ca_esp_players"]
    headers = find_request_headers(docs)
    assert headers["Appversion"] == "10.9.0-3" and "Retry" not in headers  # only string values


def test_legacy_angular_escaped_state() -> None:
    state = {
        "event": {"id": 5, "contestants": [{"name": "A"}, {"name": "B"}], "date": "2026-11-23T17:00:00Z"}
    }
    escaped = json.dumps(state).replace('"', "&q;")
    html = f'<html><script id="serverApp-state" type="application/json">{escaped}</script></html>'
    docs = extract_json_documents(html)
    assert docs == [state]
    # an event with explicit contestants and a start time is kept even without markets yet
    events = extract_events(docs)
    assert [(e.external_id, e.markets) for e in events] == [("5", [])]
    assert [r.external_id for r in extract_event_records(docs)] == ["5"]


# --------------------------------------------------------------------------- shape 3: flat rows


def test_flat_rows_shape() -> None:
    events = _by_id(extract_events([(FIXTURES / "flat_odds_rows.json").read_text(encoding="utf-8")]))
    assert set(events) == {"E-77"}  # incomplete / invalid-price rows are skipped
    spirit = events["E-77"]
    assert (spirit.team1_raw, spirit.team2_raw) == ("Team Spirit", "MOUZ")
    assert spirit.starts_at == datetime(2026, 11, 24, 13, 30, tzinfo=UTC)  # +01:00 converted to UTC
    markets = _markets(spirit)
    assert markets[("Wynik meczu", "W-1")] == {"1": (Decimal("1.72"), True), "2": (Decimal("2.05"), True)}
    assert markets[("Mapa 1 - handicap rund", "RH-1")]["MOUZ +3,5"] == (Decimal("1.90"), False)
    assert markets[("Mapa 1 - dogrywka", "")] == {"Tak": (Decimal("5.5"), True)}


# --------------------------------------------------------------------------- shape 4: gRPC-web -> JSON


def test_grpc_web_body_shape() -> None:
    body = grpc_response(sample_grpc_match())
    events = extract_events([decode_betclic_grpc_body(body, content_type="application/grpc-web+proto")])
    assert len(events) == 1
    navi = events[0]
    assert navi.external_id == "1001234567" and navi.competition_raw == "Counter-Strike 2 - IEM Chengdu"
    markets = _markets(navi)
    assert markets[("Zwycięzca meczu", "8001")]["Natus Vincere"] == (Decimal("1.62"), True)
    assert set(markets[("Mapa 1 - liczba rund", "80021")]) == {"Powyżej 21,5", "Poniżej 21,5"}
    assert set(markets[("Mapa 1 - liczba rund", "80022")]) == {"Powyżej 23,5", "Poniżej 23,5"}
    assert markets[("Dogrywka - mapa 1", "8003")]["Tak"] == (Decimal("4.75"), False)  # suspended


# --------------------------------------------------------------------------- merging & robustness


def test_duplicates_across_payloads_are_merged_and_later_prices_win() -> None:
    first = {
        "events": [
            {
                "id": 1,
                "name": "Natus Vincere - FaZe Clan",
                "startDate": "2026-11-23T17:00:00Z",
                "markets": [
                    {
                        "id": "m1",
                        "name": "Zwycięzca meczu",
                        "selections": [
                            {"id": "s1", "name": "Natus Vincere", "odds": "1,60"},
                            {"id": "s2", "name": "FaZe Clan", "odds": "2,30"},
                        ],
                    }
                ],
            }
        ]
    }
    second = {
        "match": {
            "matchId": "1",
            "contestants": [{"name": "Natus Vincere"}, {"name": "FaZe Clan"}],
            "competition": "Counter-Strike 2 - IEM",
            "markets": [
                {
                    "id": "m1",
                    "name": "Zwycięzca meczu",
                    "selections": [{"id": "s1", "name": "Natus Vincere", "price": 1.66}],
                },
                {
                    "id": "m2",
                    "name": "Liczba map",
                    "outcomes": [
                        {"id": "s3", "label": "Powyżej 2,5", "oddValue": "2,05"},
                        {"id": "s4", "label": "Poniżej 2,5", "oddValue": "1,72"},
                    ],
                },
            ],
        }
    }
    events = extract_events([first, second])
    assert len(events) == 1
    merged = events[0]
    assert merged.starts_at == datetime(2026, 11, 23, 17, tzinfo=UTC)
    assert merged.competition_raw == "Counter-Strike 2 - IEM"
    markets = _markets(merged)
    assert markets[("Zwycięzca meczu", "m1")] == {
        "Natus Vincere": (Decimal("1.66"), True),  # updated by the later payload
        "FaZe Clan": (Decimal("2.30"), True),  # kept from the first one
    }
    assert markets[("Liczba map", "m2")]["Poniżej 2,5"] == (Decimal("1.72"), True)


def test_merge_parsed_events_does_not_mutate_inputs() -> None:
    payload = {
        "id": 9,
        "name": "A - B",
        "date": "2026-11-23T17:00:00Z",
        "markets": [{"name": "Wynik", "selections": [{"name": "A", "odds": 1.5}]}],
    }
    recs = extract_event_records([payload]) + extract_event_records([payload])
    merged = merge_parsed_events(recs)
    assert len(merged) == 1 and len(recs) == 2
    assert merged[0] is not recs[0]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "",
        "<html>403 Forbidden</html>",
        b"\x00\x01\x02",
        {"id": 1, "name": "x"},
        [[[[[[[]]]]]]],
        {"id": 1, "contestants": [{"name": "A"}, {"name": "B"}], "date": "not a date", "markets": [{"x": 1}]},
        {"id": 1, "name": "A - B", "date": 3, "markets": None},
        {"matchId": "1", "contestants": "oops", "matchDateUtc": "2026-11-23T17:00:00Z"},
    ],
)
def test_garbage_never_raises(payload: object) -> None:
    assert extract_events([payload]) == []


def test_deeply_nested_payload_is_survivable() -> None:
    node: dict[str, object] = {"leaf": 1}
    for _ in range(5000):
        node = {"child": node}
    assert extract_events([node]) == []


def test_json_strings_inside_payloads_are_decoded() -> None:
    inner = {
        "id": 3,
        "contestants": [{"name": "A"}, {"name": "B"}],
        "date": "2026-11-23T17:00:00Z",
        "markets": [{"name": "Zwycięzca meczu", "selections": [{"name": "A", "odds": "1,9"}]}],
    }
    events = extract_events([{"payload": json.dumps(inner)}])
    assert [e.external_id for e in events] == ["3"]


def test_custom_key_candidates() -> None:
    payload = {
        "fixtures": [
            {
                "fixtureKey": "F1",
                "sides": [{"nm": "A"}, {"nm": "B"}],
                "ko": "2026-11-23T17:00:00Z",
                "books": [{"nm": "Wynik", "picks": [{"nm": "A", "kurs": "1,5"}]}],
            }
        ]
    }
    assert extract_events([payload]) == []
    keys = PayloadKeys(
        event_ids=("fixtureKey",),
        contestants=("sides",),
        contestant_names=("nm",),
        start_times=("ko",),
        market_names=("nm",),
        selection_containers=("picks",),
        selection_names=("nm",),
        odds=("kurs",),
    )
    events = extract_events([payload], keys=keys)
    assert [e.external_id for e in events] == ["F1"]
    assert events[0].markets[0].selections[0].odds == Decimal("1.5")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.betclic.pl/e-sport-s102/iem-c9001/navi-faze-m1001234567", "1001234567"),
        ("/e-sport-s102/iem-c9001/navi-faze-m42?tab=maps", "42"),
        ("https://www.betclic.pl/e-sport-s102", None),
        ("https://www.betclic.pl/e-sport-s102/m-c1/team-mo-faze", None),
        (None, None),
    ],
)
def test_event_id_from_url(url: str | None, expected: str | None) -> None:
    assert event_id_from_url(url) == expected


def test_market_named_like_a_match_with_a_date_is_not_a_nested_event() -> None:
    payload = {
        "id": 1,
        "contestants": [{"name": "A"}, {"name": "B"}],
        "date": "2026-11-23T17:00:00Z",
        "url": "https://cdn.example/logo.png",
        "markets": [
            {
                "id": "m",
                "name": "Mapa 1 - liczba rund",
                "startDate": "2026-11-23T17:00:00Z",
                "selections": [{"id": "s", "name": "Powyżej 21,5", "odds": "1,9"}],
            }
        ],
    }
    (only,) = extract_events([payload])
    assert [m.raw_name for m in only.markets] == ["Mapa 1 - liczba rund"]
    assert only.url is None  # image URLs are not event URLs
