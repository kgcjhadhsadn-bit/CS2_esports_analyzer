from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from cs2bot.config import Settings
from cs2bot.odds.api import BetclicApiError, BetclicApiSource
from cs2bot.odds.browser import playwright_proxy, run_coroutine_blocking
from cs2bot.odds.grpc_web import decode_fields, field_bytes, field_varint, grpc_web_frame, split_frames
from cs2bot.odds.source_config import BetclicSourceConfig
from cs2bot.odds.sources import (
    CapturedPayload,
    OddsSource,
    RawPayloadReplaySource,
    RawPayloadStore,
    StaticOddsSource,
    SupportsFetchInfo,
    classify_body,
    ids_from_links,
)
from cs2bot.utils.http import RateLimiter
from cs2bot.utils.paths import project_root
from tests.odds.odds_testkit import FIXTURES, event, grpc_response, proto_market, proto_match, proto_selection

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- raw archive


def test_raw_store_saves_payloads_with_index(tmp_path: Path) -> None:
    store = RawPayloadStore(tmp_path / "raw")
    path = store.save(
        CapturedPayload(
            kind="grpc",
            data=b"\x00\x00\x00\x00\x00",
            captured_at=NOW,
            url="https://offering.begmedia.com/x/GetMatchWithNotification?a=1",
            content_type="application/grpc-web+proto",
            request_body=b"\x08\x01",
        )
    )
    assert path is not None and path.parent.name == "2026-11-22" and path.suffix == ".bin"
    assert path.read_bytes() == b"\x00" * 5
    (row,) = [
        json.loads(line) for line in (path.parent / "index.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert row["kind"] == "grpc" and row["bytes"] == 5 and row["request_file"].endswith(".request.bin")
    assert (path.parent / row["request_file"]).read_bytes() == b"\x08\x01"
    assert RawPayloadStore(None).save(CapturedPayload(kind="json", data=b"{}", captured_at=NOW)) is None
    assert (
        RawPayloadStore(tmp_path, enabled=False).save(
            CapturedPayload(kind="json", data=b"{}", captured_at=NOW)
        )
        is None
    )


def test_raw_store_cleanup_only_removes_old_day_directories(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    for name in ("2026-10-01", "2026-11-20", "notes"):
        (root / name).mkdir(parents=True)
        (root / name / "f.json").write_text("{}")
    store = RawPayloadStore(root, retention_days=14)
    assert store.cleanup(NOW) == 1
    assert sorted(p.name for p in root.iterdir()) == ["2026-11-20", "notes"]


def test_raw_store_from_settings_uses_data_dir(settings: Settings) -> None:
    store = RawPayloadStore.from_settings(settings)
    assert Path(settings.data_dir).is_absolute()  # conftest points it at a temp dir
    assert store.root == Path(settings.data_dir) / "raw" / "betclic"


# --------------------------------------------------------------------------- simple sources


def test_static_source_returns_copies_and_supports_fetch_info() -> None:
    source = StaticOddsSource([event("1")], partial_event_ids=["1"])
    assert isinstance(source, OddsSource) and isinstance(source, SupportsFetchInfo)
    first = source.fetch_cs2_events()
    first[0].name = "changed"
    assert source.fetch_cs2_events()[0].name != "changed"
    assert source.last_fetch_info().partial_event_ids == {"1"}


def test_replay_source_reparses_archived_payloads(tmp_path: Path) -> None:
    day = tmp_path / "raw" / "2026-11-22"
    day.mkdir(parents=True)
    shutil.copy(FIXTURES / "begmedia_v4_events.json", day / "120000_0001_json_listing.json")
    shutil.copy(FIXTURES / "betclic_event_page.html", day / "120001_0002_html_event.html")
    (day / "120002_0003_grpc_match.bin").write_bytes(
        grpc_response(
            proto_match(
                777,
                "G2 Esports - Team Vitality",
                "2026-11-24T19:00:00Z",
                ("G2 Esports", "Team Vitality"),
                "Counter-Strike 2 - IEM Chengdu",
                [
                    proto_market(
                        1,
                        "Zwycięzca meczu",
                        [
                            proto_selection(1, "G2 Esports", 2.8, 1),
                            proto_selection(2, "Team Vitality", 1.42, 1),
                        ],
                    )
                ],
            )
        )
    )
    (day / "120002_0003_grpc_match.request.bin").write_bytes(b"\x08\x01")
    (day / "index.jsonl").write_text("{}\n")
    source = RawPayloadReplaySource([tmp_path / "raw"], cs2_pattern=BetclicSourceConfig().cs2_pattern)
    events = {e.external_id: e for e in source.fetch_cs2_events()}
    assert {"1001234567", "1001234999", "777"} <= set(events)
    assert "2002002" not in events  # football recommendation filtered
    navi = events["1001234567"]
    assert {m.raw_name for m in navi.markets} >= {"Zwycięzca meczu", "Handicap map", "Mapa 1 - liczba rund"}
    info = source.last_fetch_info()
    assert info.payloads_captured == 3 and info.partial_event_ids == set(events)
    assert info.raw_paths_by_event["777"][0].endswith("120002_0003_grpc_match.bin")


@pytest.mark.parametrize(
    ("body", "ctype", "expected"),
    [
        (b'{"a": 1}', None, "json"),
        (b"<html></html>", "text/html; charset=utf-8", "html"),
        (b"\x00\x00\x00\x00\x00", None, "grpc"),
        (b"anything", "application/grpc-web+proto", "grpc"),
        (b"\x89PNG", "image/png", None),
    ],
)
def test_classify_body(body: bytes, ctype: str | None, expected: str | None) -> None:
    assert classify_body(body, ctype) == expected


def test_ids_from_links() -> None:
    links = ["https://x/a-m1", "https://x/b-m2?tab=1", "https://x/a-m1#again", "https://x/c-s102"]
    assert ids_from_links(links, BetclicSourceConfig().event_link_pattern) == {
        "1": "https://x/a-m1",
        "2": "https://x/b-m2?tab=1",
    }


# --------------------------------------------------------------------------- config


def test_source_config_from_repo_yaml_and_overrides(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cfg = BetclicSourceConfig.from_yaml(project_root() / "config" / "betclic_markets.yaml")
    assert cfg.max_events_per_run == 40 and isinstance(cfg.consent_selectors, tuple)
    custom = tmp_path / "rules.yaml"
    custom.write_text(
        "source:\n  max_events_per_run: 5\n  grpc_url_markers: [a, b]\n  bogus: 1\n", encoding="utf-8"
    )
    loaded = BetclicSourceConfig.from_yaml(custom)
    assert loaded.max_events_per_run == 5 and loaded.grpc_url_markers == ("a", "b")
    assert "bogus" in caplog.text
    assert BetclicSourceConfig.from_yaml(tmp_path / "missing.yaml") == BetclicSourceConfig()


def test_source_config_load_uses_settings_rules_file(settings: Settings) -> None:
    assert BetclicSourceConfig.load(settings).cs2_pattern  # resolved via the project root from a temp CWD


# --------------------------------------------------------------------------- helpers


def test_run_coroutine_blocking_works_inside_a_running_loop() -> None:
    async def inner() -> int:
        await asyncio.sleep(0)
        return 42

    async def outer() -> int:
        return run_coroutine_blocking(inner)

    assert asyncio.run(outer()) == 42
    assert run_coroutine_blocking(inner) == 42

    async def boom() -> None:
        raise ValueError("bad")

    with pytest.raises(ValueError, match="bad"):
        run_coroutine_blocking(boom)


def test_playwright_proxy_keeps_credentials_separate() -> None:
    assert playwright_proxy("http://user:p%40ss@proxy.example:8080") == {
        "server": "http://proxy.example:8080",
        "username": "user",
        "password": "p@ss",
    }
    assert playwright_proxy("socks5://proxy.example:1080") == {"server": "socks5://proxy.example:1080"}


# --------------------------------------------------------------------------- API source


def _api_settings(monkeypatch: pytest.MonkeyPatch, base: str) -> Settings:
    monkeypatch.setenv("CS2BOT_BETCLIC__OFFERING_API_BASE", base)
    return Settings()


def _api_source(settings: Settings, client: httpx.Client, **cfg: object) -> BetclicApiSource:
    config = BetclicSourceConfig(**cfg)  # type: ignore[arg-type]
    return BetclicApiSource(settings, client, config=config, limiter=RateLimiter(0), retry_attempts=1)


def test_api_source_requires_a_base_url(settings: Settings) -> None:
    with pytest.raises(BetclicApiError, match="offering_api_base"):
        _api_source(settings, httpx.Client()).fetch_cs2_events()


def test_api_source_rest_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _api_settings(monkeypatch, "https://offer.test")
    listing = json.loads((FIXTURES / "begmedia_v4_events.json").read_text(encoding="utf-8"))
    listing["unifiedEvents"].append(
        {
            "id": 5,
            "name": "G2 - Fnatic",
            "date": "2026-11-24T19:00:00Z",
            "competition": {"name": "League of Legends - LEC"},
            "contestants": [{"name": "G2"}, {"name": "Fnatic"}],
            "markets": [{"name": "Zwycięzca meczu", "selections": [{"name": "G2", "odds": 1.5}]}],
        }
    )
    detail = {
        "id": 1001234567,
        "name": "Natus Vincere - FaZe Clan",
        "date": "2026-11-23T17:00:00Z",
        "contestants": [{"name": "Natus Vincere"}, {"name": "FaZe Clan"}],
        "markets": [
            {
                "id": 9,
                "name": "Liczba rund - mapa 1",
                "selections": [
                    {"id": 91, "name": "Powyżej 21,5", "odds": "1,85"},
                    {"id": 92, "name": "Poniżej 21,5", "odds": "1,95"},
                ],
            }
        ],
    }
    with respx.mock(assert_all_called=False) as mock, httpx.Client() as client:
        list_route = mock.get(url__regex=r"https://offer\.test/api/pub/v4/events\?.*").mock(
            return_value=httpx.Response(200, json=listing)
        )
        mock.get(url__regex=r"https://offer\.test/api/pub/v5/events/1001234567\?.*").mock(
            return_value=httpx.Response(200, json=detail)
        )
        mock.get(url__regex=r"https://offer\.test/api/pub/v5/events/1001234999\?.*").mock(
            return_value=httpx.Response(503)
        )
        source = _api_source(settings, client, api_page_size=40)
        events = {e.external_id: e for e in source.fetch_cs2_events()}
    assert source.protocol == "rest"
    query = list_route.calls[0].request.url.params
    assert (query["countrycode"], query["language"], query["sitecode"], query["sportIds"]) == (
        "pl",
        "pl",
        "plpl",
        "102",
    )
    assert set(events) == {"1001234567", "1001234999"}  # LoL dropped by the CS2 filter
    assert {m.raw_name for m in events["1001234567"].markets} >= {"Zwycięzca meczu", "Liczba rund - mapa 1"}
    info = source.last_fetch_info()
    assert info.partial_event_ids == {"1001234999"} and any("503" in e for e in info.errors)
    assert info.notes["protocol"] == "rest" and info.raw_paths_by_event["1001234567"]


def test_api_source_grpc_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _api_settings(monkeypatch, "https://offering.begmedia.com/web/offering.access.api")
    match = proto_match(
        1001234567,
        "Natus Vincere - FaZe Clan",
        "2026-11-23T17:00:00Z",
        ("Natus Vincere", "FaZe Clan"),
        "Counter-Strike 2 - IEM Chengdu",
        [
            proto_market(
                1,
                "Zwycięzca meczu",
                [proto_selection(11, "Natus Vincere", 1.62, 1), proto_selection(12, "FaZe Clan", 2.25, 1)],
            )
        ],
        categories=("ca_esp_top", "ca_esp_maps"),
    )
    listing_body = grpc_web_frame(
        field_bytes(1, field_bytes(1, "Counter-Strike") + field_bytes(3, match) + field_varint(5, 1))
    )
    maps_match = proto_match(
        1001234567,
        "Natus Vincere - FaZe Clan",
        "2026-11-23T17:00:00Z",
        ("Natus Vincere", "FaZe Clan"),
        "Counter-Strike 2 - IEM Chengdu",
        [
            proto_market(
                2,
                "Mapa 1 - liczba rund",
                [proto_selection(21, "Powyżej 21,5", 1.85, 2), proto_selection(22, "Poniżej 21,5", 1.95, 2)],
            )
        ],
        categories=(),
    )
    seen: list[dict[int, object]] = []

    def get_match(request: httpx.Request) -> httpx.Response:
        frame = split_frames(request.content)[0]
        fields = {f.number: f.value for f in decode_fields(frame.payload) or []}
        seen.append(fields)
        body = grpc_response(maps_match if fields.get(3) == b"ca_esp_maps" else match)
        return httpx.Response(200, content=body, headers={"content-type": "application/grpc-web+proto"})

    with respx.mock(assert_all_called=True) as mock, httpx.Client() as client:
        listing_route = mock.post(url__regex=r".*MatchService/GetMatchesBySportWithNotifications$").mock(
            return_value=httpx.Response(
                200, content=listing_body, headers={"content-type": "application/grpc-web+proto"}
            )
        )
        mock.post(url__regex=r".*MatchService/GetMatchWithNotification$").mock(side_effect=get_match)
        source = _api_source(settings, client)
        events = source.fetch_cs2_events()

    assert source.protocol == "grpc"
    sent = listing_route.calls[0].request
    assert sent.headers["x-bg-regulation"] == "PL" and sent.headers["origin"] == "https://www.betclic.pl"
    assert sent.headers["content-type"] == "application/grpc-web+proto"
    listing_fields = {f.number: f.value for f in decode_fields(split_frames(sent.content)[0].payload) or []}
    assert listing_fields == {1: b"counter_strike", 2: b"pl"}
    assert [f.get(3) for f in seen] == [None, b"ca_esp_top", b"ca_esp_maps"]
    assert all(f[1] == 1001234567 and f[2] == b"pl" for f in seen)
    (navi,) = events
    assert {m.raw_name for m in navi.markets} == {"Zwycięzca meczu", "Mapa 1 - liczba rund"}
    assert source.last_fetch_info().partial_event_ids == set()


def test_api_source_grpc_error_status_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _api_settings(monkeypatch, "https://offering.begmedia.com/web/offering.access.api")
    with respx.mock() as mock, httpx.Client() as client:
        mock.post(url__regex=r".*/GetMatchesBySportWithNotifications").mock(
            return_value=httpx.Response(
                200, content=b"", headers={"grpc-status": "12", "grpc-message": "unimplemented"}
            )
        )
        source = _api_source(settings, client)
        assert source.fetch_cs2_events() == []
    assert any("gRPC status 12" in e for e in source.last_fetch_info().errors)


def test_api_rest_listing_pagination_stops_on_short_page(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _api_settings(monkeypatch, "https://offer.test")
    page = {
        "unifiedEvents": [
            {
                "id": i,
                "name": f"A{i} - B{i}",
                "date": (NOW + timedelta(days=1)).isoformat(),
                "competition": {"name": "Counter-Strike 2 - CCT"},
                "contestants": [{"name": f"A{i}"}, {"name": f"B{i}"}],
                "markets": [{"name": "Zwycięzca meczu", "selections": [{"name": f"A{i}", "odds": 1.5}]}],
            }
            for i in range(1, 3)
        ]
    }
    with respx.mock(assert_all_called=False) as mock, httpx.Client() as client:
        route = mock.get(url__regex=r".*/v4/events\?.*").mock(return_value=httpx.Response(200, json=page))
        mock.get(url__regex=r".*/v5/events/.*").mock(return_value=httpx.Response(404))
        source = _api_source(settings, client, api_page_size=2, api_max_pages=3)
        source.fetch_cs2_events()
    offsets = [call.request.url.params["offset"] for call in route.calls]
    assert offsets == ["0", "2", "4"]  # full pages keep paginating up to api_max_pages


# --------------------------------------------------------------------------- review round 1


def test_select_cs2_drops_other_esports_even_when_their_id_is_allowed() -> None:
    """With no CS2-looking links the whole e-sport section is crawled: LoL must not come out as CS2."""
    from cs2bot.odds.payload_parser import ParsedEvent
    from cs2bot.odds.source_config import DEFAULT_CS2_PATTERN, is_other_game, url_section
    from cs2bot.odds.sources import select_cs2

    def rec(
        eid: str, competition: str | None, url: str | None = None, sport: str | None = None
    ) -> ParsedEvent:
        return ParsedEvent(
            external_id=eid,
            name="G2 - Fnatic",
            team1_raw="G2",
            team2_raw="Fnatic",
            competition_raw=competition,
            url=url,
            sport_raw=sport,
        )

    records = [
        rec("1", "IEM Chengdu"),
        rec("2", "LEC Winter"),
        rec("3", None, "https://www.betclic.pl/e-sport-s102/dota-2-the-international-c5/g2-fnatic-m3"),
        rec("4", "Playoffs", None, "Valorant"),
        rec("5", "Counter-Strike 2 - ESL Pro League"),
    ]
    allowed = {"1", "2", "3", "4", "5"}
    kept = select_cs2(records, pattern=DEFAULT_CS2_PATTERN, allowed_ids=allowed)
    assert [r.external_id for r in kept] == ["1", "5"]
    assert url_section("https://x.test/a/b/c-m1?x=1") == "https://x.test/a/b"
    assert url_section(None) is None
    assert is_other_game([None, "", "ESL Pro League"]) is False
    assert is_other_game(["Rainbow-Six Siege"]) is True


def _winner_payload(odds: float) -> dict[str, object]:
    return {
        "id": 42,
        "name": "Natus Vincere - FaZe Clan",
        "date": "2026-11-23T17:00:00Z",
        "sport": {"name": "Counter-Strike 2"},
        "contestants": [{"name": "Natus Vincere"}, {"name": "FaZe Clan"}],
        "markets": [
            {
                "id": 1,
                "name": "Zwycięzca meczu",
                "selections": [
                    {"id": 11, "name": "Natus Vincere", "odds": odds},
                    {"id": 12, "name": "FaZe Clan", "odds": 2.3},
                ],
            }
        ],
    }


def test_capture_times_are_tracked_per_market_and_page_names_survive() -> None:
    """A later payload that only repeats the match winner must not make the other markets look fresh."""
    from cs2bot.odds.sources import FetchInfo, parse_captured_full

    full = _winner_payload(1.60)
    full["markets"].append(  # type: ignore[attr-defined]
        {
            "name": "Mapa 1",  # a group: its markets get the map as prefix
            "items": [
                {
                    "id": 2,
                    "name": "Liczba rund",
                    "selections": [
                        {"id": 21, "name": "Powyżej 21,5", "odds": 1.85},
                        {"id": 22, "name": "Poniżej 21,5", "odds": 1.95},
                    ],
                }
            ],
        }
    )
    early, late = NOW, NOW + timedelta(minutes=4)
    payloads = [
        CapturedPayload(kind="json", data=json.dumps(full).encode(), captured_at=early, url="https://x/a"),
        CapturedPayload(
            kind="json", data=json.dumps(_winner_payload(1.70)).encode(), captured_at=late, url="https://x/b"
        ),
        CapturedPayload(kind="json", data=b"{not json", captured_at=late, url="https://x/c"),  # ignored
    ]
    parsed = parse_captured_full(payloads)
    (collected,) = parsed.collected()
    assert collected.captured_at == late and collected.sport_raw == "Counter-Strike 2"
    winner = next(m for m in collected.markets if m.raw_name == "Zwycięzca meczu")
    assert {s.raw_name: str(s.odds) for s in winner.selections}["Natus Vincere"] == "1.700"  # later wins
    info = FetchInfo()
    parsed.fill_info(info, parsed.records)
    assert info.captured_at_by_event == {"42": late}
    assert info.captured_at_by_market["42"] == {
        ("Zwycięzca meczu", "1"): late,
        ("Mapa 1 - Liczba rund", "2"): early,
    }
    assert info.market_meta_by_event == {
        "42": {("Mapa 1 - Liczba rund", "2"): {"page_name": "Liczba rund", "group": "Mapa 1"}}
    }


def test_replay_keeps_the_original_capture_times(tmp_path: Path) -> None:
    """Replaying yesterday's archive must never stamp its prices with the current time."""
    day = tmp_path / "2026-11-21"
    day.mkdir()
    name = "101500_0001_json_listing.json"
    (day / name).write_text(json.dumps(_winner_payload(1.60)), encoding="utf-8")
    captured = datetime(2026, 11, 21, 10, 15, tzinfo=UTC)
    index = [
        "this line is damaged",
        json.dumps({"file": name, "captured_at": captured.isoformat(), "page_url": "https://x/listing"}),
    ]
    (day / "index.jsonl").write_text("\n".join(index) + "\n", encoding="utf-8")
    source = RawPayloadReplaySource([tmp_path])
    (replayed,) = source.fetch_cs2_events()
    assert replayed.captured_at == captured
    info = source.last_fetch_info()
    assert info.captured_at_by_event == {"42": captured} and info.partial_event_ids == {"42"}
    # without an index entry the file's modification time is used - still never "now + something"
    (day / "index.jsonl").unlink()
    (again,) = RawPayloadReplaySource([tmp_path]).fetch_cs2_events()
    assert again.captured_at is not None and again.captured_at <= datetime.now(UTC)


def test_source_config_ignores_values_of_the_wrong_type(caplog: pytest.LogCaptureFixture) -> None:
    cfg = BetclicSourceConfig.from_mapping(
        {
            "max_events_per_run": "many",
            "min_page_delay_s": "1.5",  # numeric text is fine
            "max_page_delay_s": None,
            "fetch_categories": "yes",
            "consent_selectors": "#one",
            "user_agent": "Mozilla/5.0 test",
            "raw_retention_days": 7.0,
            "api_page_size": 2.5,
            "cs2_pattern": ["x"],
        }
    )
    default = BetclicSourceConfig()
    assert cfg.max_events_per_run == default.max_events_per_run
    assert cfg.min_page_delay_s == 1.5 and cfg.max_page_delay_s == default.max_page_delay_s
    assert cfg.fetch_categories is default.fetch_categories
    assert cfg.consent_selectors == default.consent_selectors
    assert cfg.user_agent == "Mozilla/5.0 test" and cfg.raw_retention_days == 7
    assert cfg.api_page_size == default.api_page_size and cfg.cs2_pattern == default.cs2_pattern
    for key in ("max_events_per_run", "max_page_delay_s", "fetch_categories", "consent_selectors"):
        assert key in caplog.text
