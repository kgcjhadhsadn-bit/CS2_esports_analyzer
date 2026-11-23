"""BetclicBrowserSource against local pages served with ``context.route`` (no network)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from cs2bot.config import Settings
from cs2bot.db.models import BookmakerMarket
from cs2bot.odds.browser import BetclicBrowserSource, BetclicSourceError
from cs2bot.odds.collector import collect_once
from cs2bot.odds.grpc_web import decode_fields, split_frames
from cs2bot.odds.market_mapping import MarketMapper
from cs2bot.odds.source_config import BetclicSourceConfig
from cs2bot.utils.paths import project_root
from cs2bot.utils.time import utcnow
from tests.odds.odds_testkit import (
    grpc_response,
    make_match,
    make_team,
    proto_market,
    proto_match,
    proto_selection,
)

pytestmark = pytest.mark.browser

CHROMIUM = "/opt/pw-browsers/chromium"
SITE = "https://betclic.test"
GRPC = "https://offering.test/web/offering.access.api"
EVENT_ID = 1001234567
EVENT_PATH = f"/e-sport-s102/counter-strike-2-iem-chengdu-c9001/natus-vincere-faze-clan-m{EVENT_ID}"
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
}


class FakeBetclic:
    """Serves the listing page, the XHR JSON, an event page with ng-state and the gRPC API."""

    def __init__(self, *, failing_categories: tuple[str, ...] = ()) -> None:
        self.failing_categories = failing_categories
        self.start = (utcnow() + timedelta(hours=3)).replace(microsecond=0)
        self.grpc_requests: list[dict[int, Any]] = []
        self.paths: list[str] = []

    @property
    def start_iso(self) -> str:
        return self.start.strftime("%Y-%m-%dT%H:%M:%SZ")

    def listing_html(self) -> str:
        return f"""<!DOCTYPE html><html lang="pl"><body>
<a href="{EVENT_PATH}">Natus Vincere - FaZe Clan</a>
<a href="/e-sport-s102/league-of-legends-lec-c77/g2-fnatic-m555">G2 - Fnatic</a>
<a href="/pilka-nozna-s1/ekstraklasa-c3/lech-poznan-legia-warszawa-m2002002">Lech - Legia</a>
<script>
  const xhr = new XMLHttpRequest();
  xhr.open('GET', '/api/listing.json');
  xhr.send();
</script>
</body></html>"""

    def listing_json(self) -> dict[str, Any]:
        return {
            "unifiedEvents": [
                {
                    "id": EVENT_ID,
                    "name": "Natus Vincere - FaZe Clan",
                    "date": self.start_iso,
                    "competition": {"name": "Counter-Strike 2 - IEM Chengdu"},
                    "contestants": [{"name": "Natus Vincere"}, {"name": "FaZe Clan"}],
                    "markets": [
                        {
                            "id": 1,
                            "name": "Zwycięzca meczu",
                            "selections": [
                                {"id": 11, "name": "Natus Vincere", "odds": 1.6},
                                {"id": 12, "name": "FaZe Clan", "odds": 2.3},
                            ],
                        }
                    ],
                },
                {
                    "id": 555,
                    "name": "G2 - Fnatic",
                    "date": self.start_iso,
                    "competition": {"name": "League of Legends - LEC"},
                    "contestants": [{"name": "G2"}, {"name": "Fnatic"}],
                    "markets": [
                        {
                            "id": 2,
                            "name": "Zwycięzca meczu",
                            "selections": [
                                {"id": 21, "name": "G2", "odds": 1.5},
                                {"id": 22, "name": "Fnatic", "odds": 2.6},
                            ],
                        }
                    ],
                },
            ]
        }

    def event_html(self) -> str:
        state = {
            "grpc:1": {"requestHeaders": {"Appversion": "10.9.0-3"}, "request": {"language": "pl"}},
            "grpc:2": {
                "response": {
                    "payload": {
                        "match": {
                            "matchId": str(EVENT_ID),
                            "name": "Natus Vincere - FaZe Clan",
                            "matchDateUtc": self.start_iso,
                            "competition": {"name": "Counter-Strike 2 - IEM Chengdu"},
                            "contestants": [{"name": "Natus Vincere"}, {"name": "FaZe Clan"}],
                            "categories": [
                                {"id": "ca_esp_top", "name": "Top"},
                                {"id": "ca_esp_maps", "name": "Mapy"},
                            ],
                            "markets": [
                                {
                                    "id": "1",
                                    "name": "Zwycięzca meczu",
                                    "mainSelections": [
                                        {
                                            "id": "11",
                                            "name": "Natus Vincere",
                                            "odds": 1.62,
                                            "betslipMarketId": "1",
                                        },
                                        {
                                            "id": "12",
                                            "name": "FaZe Clan",
                                            "odds": 2.28,
                                            "betslipMarketId": "1",
                                        },
                                    ],
                                }
                            ],
                        }
                    }
                }
            },
        }
        # the page itself asks the gRPC-web API for its default bundle (captured by the fetch hook)
        page_call = f"""
<script>
  const msg = new Uint8Array([0x08, ...varint({EVENT_ID}), 0x12, 0x02, 0x70, 0x6c]);
  function varint(n) {{ const out = []; let v = BigInt(n);
    while (v > 127n) {{ out.push(Number(v & 127n) | 128); v >>= 7n; }} out.push(Number(v)); return out; }}
  const frame = new Uint8Array(5 + msg.length);
  new DataView(frame.buffer).setUint32(1, msg.length);
  frame.set(msg, 5);
  fetch('{GRPC}/offering.access.api.MatchService/GetMatchWithNotification', {{
    method: 'POST', body: frame, headers: {{'Content-Type': 'application/grpc-web+proto', 'X-Grpc-Web': '1'}}
  }}).then((r) => r.arrayBuffer());
</script>"""
        return (
            '<!DOCTYPE html><html lang="pl"><body><app-root>NAVI vs FaZe</app-root>'
            f'<script id="ng-state" type="application/json">{json.dumps(state)}</script>'
            f"{page_call}</body></html>"
        )

    def grpc_body(self, category: str | None) -> bytes:
        if category == "ca_esp_maps":
            markets = [
                proto_market(
                    30,
                    "Mapa 1 - liczba rund",
                    [
                        proto_selection(301, "Powyżej 21,5", 1.85, 30),
                        proto_selection(302, "Poniżej 21,5", 1.95, 30),
                    ],
                )
            ]
        else:
            markets = [
                proto_market(
                    20,
                    "Handicap map",
                    [
                        proto_selection(201, "Natus Vincere (-1,5)", 2.95, 20),
                        proto_selection(202, "FaZe Clan (+1,5)", 1.38, 20),
                    ],
                )
            ]
        return grpc_response(
            proto_match(
                EVENT_ID,
                "Natus Vincere - FaZe Clan",
                self.start_iso,
                ("Natus Vincere", "FaZe Clan"),
                "Counter-Strike 2 - IEM Chengdu",
                markets,
            )
        )

    async def install(self, context: Any) -> None:
        async def site(route: Any) -> None:
            url = route.request.url
            path = url.replace(SITE, "").split("?", 1)[0]
            self.paths.append(path)
            if path == "/e-sport-s102":
                await route.fulfill(
                    status=200, content_type="text/html; charset=utf-8", body=self.listing_html()
                )
            elif path == "/api/listing.json":
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(self.listing_json())
                )
            elif path == EVENT_PATH:
                await route.fulfill(
                    status=200, content_type="text/html; charset=utf-8", body=self.event_html()
                )
            else:
                await route.fulfill(status=404, content_type="text/plain", body="not found")

        async def grpc(route: Any) -> None:
            request = route.request
            if request.method == "OPTIONS":
                await route.fulfill(status=204, headers=CORS, body="")
                return
            frames = split_frames(request.post_data_buffer or b"")
            fields = decode_fields(frames[0].payload) if frames else None
            parsed = {f.number: f.value for f in fields or []}
            self.grpc_requests.append(parsed)
            category = parsed.get(3)
            if isinstance(category, bytes) and category.decode() in self.failing_categories:
                await route.fulfill(status=503, headers=CORS, body="")
                return
            await route.fulfill(
                status=200,
                headers={**CORS, "Content-Type": "application/grpc-web+proto"},
                body=self.grpc_body(category.decode() if isinstance(category, bytes) else None),
            )

        await context.route(f"{SITE}/**", site)
        await context.route("https://offering.test/**", grpc)


@pytest.fixture
def browser_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    if not Path(CHROMIUM).exists():
        pytest.skip("Chromium is not installed at /opt/pw-browsers/chromium")
    monkeypatch.setenv("CS2BOT_BETCLIC__CHROMIUM_EXECUTABLE", CHROMIUM)
    monkeypatch.setenv("CS2BOT_BETCLIC__BASE_URL", SITE)
    monkeypatch.setenv("CS2BOT_DATA_DIR", str(tmp_path / "data"))
    return Settings()


def make_source(settings: Settings, fake: FakeBetclic) -> BetclicBrowserSource:
    async def no_sleep(_: float) -> None:
        return None

    config = BetclicSourceConfig(
        min_page_delay_s=0,
        max_page_delay_s=0,
        min_category_delay_s=0,
        max_category_delay_s=0,
        settle_s=0.6,
        grpc_base_url=GRPC,
        click_category_tabs=False,
    )
    return BetclicBrowserSource(settings, config=config, context_hook=fake.install, sleep=no_sleep)


def test_browser_source_collects_listing_xhr_ng_state_and_grpc(browser_settings: Settings) -> None:
    fake = FakeBetclic()
    source = make_source(browser_settings, fake)
    events = source.fetch_cs2_events()
    info = source.last_fetch_info()

    assert [e.external_id for e in events] == [str(EVENT_ID)]  # LoL + football filtered out
    navi = events[0]
    assert (navi.team1_raw, navi.team2_raw) == ("Natus Vincere", "FaZe Clan")
    assert navi.starts_at == fake.start
    markets = {m.raw_name: m for m in navi.markets}
    assert {"Zwycięzca meczu", "Handicap map", "Mapa 1 - liczba rund"} <= set(markets)
    # ng-state prices (captured later than the listing XHR) win for the main market
    winner = {s.raw_name: s.odds for s in markets["Zwycięzca meczu"].selections}
    assert str(winner["Natus Vincere"]) in ("1.620", "1.600")

    # only the listing and the CS2 event page were opened; the LoL/football pages never
    assert fake.paths.count(EVENT_PATH) == 1
    assert not any("m555" in p or "m2002002" in p for p in fake.paths)
    assert info.pages_visited == 2 and info.partial_event_ids == set()
    assert (
        info.started_at is not None
        and info.started_at <= info.captured_at_by_event[str(EVENT_ID)] <= utcnow()
    )

    # page's own gRPC call (no category) + our category fetches with the right request fields
    categories = [r.get(3) for r in fake.grpc_requests]
    assert None in categories and b"ca_esp_maps" in categories
    for request in fake.grpc_requests:
        assert request[1] == EVENT_ID and request[2] == b"pl"

    # raw payloads archived and referenced per event
    day_dirs = list((Path(browser_settings.data_dir) / "raw" / "betclic").iterdir())
    assert len(day_dirs) == 1
    index = [
        json.loads(line) for line in (day_dirs[0] / "index.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    kinds = {row["kind"] for row in index}
    assert kinds == {"html", "json", "grpc"}
    assert any(row["url"].endswith("/api/listing.json") for row in index)  # the XHR JSON
    grpc_rows = [row for row in index if row["kind"] == "grpc"]
    hooked = [row for row in grpc_rows if "#" not in row["url"]]  # our category fetches are tagged "#ca_..."
    assert hooked and all(row.get("request_file") for row in hooked)  # page's own stream captured by the hook
    assert {row["url"].rsplit("#", 1)[-1] for row in grpc_rows if "#" in row["url"]} == {
        "ca_esp_top",
        "ca_esp_maps",
    }
    assert info.raw_paths_by_event[str(EVENT_ID)]
    assert all(Path(p).is_file() for p in info.raw_paths_by_event[str(EVENT_ID)])


def test_browser_source_end_to_end_into_the_database(browser_settings: Settings, session) -> None:
    fake = FakeBetclic()
    navi, faze = make_team(session, "Natus Vincere"), make_team(session, "FaZe Clan")
    make_match(session, faze, navi, fake.start)  # swapped orientation on purpose
    mapper = MarketMapper.from_yaml(project_root() / "config" / "betclic_markets.yaml")
    report = collect_once(session, make_source(browser_settings, fake), mapper, settings=browser_settings)
    assert report.errors == [] and report.link_status_counts == {"auto": 1}
    keys = {m.spec_key for m in session.scalars(select(BookmakerMarket)) if m.is_mapped}
    assert {"match_winner", "map_handicap|line=1.5", "total_rounds|map=1|line=21.5"} <= keys


def test_blocked_listing_raises_with_diagnostics(browser_settings: Settings) -> None:
    async def blocked(context: Any) -> None:
        async def handler(route: Any) -> None:
            await route.fulfill(status=403, content_type="text/html", body="<html>Access denied</html>")

        await context.route(f"{SITE}/**", handler)

    source = make_source(browser_settings, FakeBetclic())
    source._context_hook = blocked
    with pytest.raises(BetclicSourceError, match="403"):
        source.fetch_cs2_events()
    assert source.last_fetch_info().pages_visited == 1


def test_event_with_a_failed_category_fetch_is_reported_as_partial(
    browser_settings: Settings, session
) -> None:
    """An incomplete market list must never make the collector mark the unseen markets unavailable."""
    navi, faze = make_team(session, "Natus Vincere"), make_team(session, "FaZe Clan")
    mapper = MarketMapper.from_yaml(project_root() / "config" / "betclic_markets.yaml")
    healthy = FakeBetclic()
    make_match(session, navi, faze, healthy.start)
    first = collect_once(session, make_source(browser_settings, healthy), mapper, settings=browser_settings)
    assert first.events_partial == 0 and first.errors == []
    rounds = session.scalar(select(BookmakerMarket).where(BookmakerMarket.raw_name == "Mapa 1 - liczba rund"))
    assert rounds is not None and all(s.is_available for s in rounds.selections)

    broken = FakeBetclic(failing_categories=("ca_esp_maps",))
    broken.start = healthy.start
    source = make_source(browser_settings, broken)
    second = collect_once(session, source, mapper, settings=browser_settings)
    info = source.last_fetch_info()
    assert info.partial_event_ids == {str(EVENT_ID)} and info.notes["events_incomplete"] == 1
    assert any("ca_esp_maps" in err for err in info.errors)
    assert second.events_partial == 1 and second.selections_marked_unavailable == 0
    session.refresh(rounds)
    assert all(s.is_available for s in rounds.selections)  # not seen this time, but not "gone" either
