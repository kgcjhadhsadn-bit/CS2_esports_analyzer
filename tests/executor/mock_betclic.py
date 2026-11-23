"""Local mock of a Betclic-like site for the executor's browser tests.

A ``ThreadingHTTPServer`` on 127.0.0.1 serves a small single-page app (``mock_site/``) whose DOM mimics
the structure assumed by ``config/betclic_selectors.yaml`` (Angular custom elements, ``.marketBox``,
``.oddValue``, ``.betBox`` ...). All state lives here, in Python, so tests can script the bookmaker's
answers and assert exactly what the executor clicked - in particular every click on "Postaw zakład"
reaches ``POST /api/place`` and is recorded in ``state.place_calls``.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

SITE_DIR = Path(__file__).parent / "mock_site"
WARSAW = ZoneInfo("Europe/Warsaw")

EVENT_ID = "3002201001"
OTHER_EVENT_ID = "3002201002"


def pl_money(value: Decimal) -> str:
    """Decimal('1234.56') -> '1 234,56 zł' (NBSP thousands separator, like many Polish sites)."""
    q = value.quantize(Decimal("0.01"))
    integer, _, frac = f"{abs(q):f}".partition(".")
    groups = []
    while len(integer) > 3:
        groups.insert(0, integer[-3:])
        integer = integer[:-3]
    groups.insert(0, integer)
    sign = "-" if q < 0 else ""
    return f"{sign}{chr(0xA0).join(groups)},{frac} zł"


def parse_pl(text: str) -> Decimal:
    return Decimal(re.sub(r"\s", "", text).replace(",", "."))


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


@dataclass
class MockSelection:
    id: str
    name: str
    odds: str  # Polish text, e.g. "1,85"
    suspended: bool = False


@dataclass
class MockMarket:
    id: str
    name: str
    selections: list[MockSelection]
    only_in_all: bool = False  # hidden until the "Wszystkie" tab is clicked


@dataclass
class MockEvent:
    id: str
    team1: str
    team2: str
    markets: list[MockMarket]

    @property
    def name(self) -> str:
        return f"{self.team1} - {self.team2}"

    @property
    def path(self) -> str:
        return f"/e-sport-s102/iem-katowice-c1234/{slug(self.team1)}-{slug(self.team2)}-m{self.id}"


@dataclass
class MockBet:
    bet_id: str
    placed_at: datetime
    event: str
    market: str
    selection: str
    odds: str
    stake: Decimal
    status: str = "W grze"
    payout: Decimal | None = None

    def to_json(self) -> dict[str, Any]:
        local = self.placed_at.astimezone(WARSAW)
        if self.status == "W grze":
            payout_text = f"Możliwa wygrana: {pl_money(self.stake * parse_pl(self.odds))}"
        elif self.status == "Przegrany":
            payout_text = f"Wygrana: {pl_money(Decimal('0'))}"
        elif self.status == "Zwrot":
            payout_text = f"Zwrot: {pl_money(self.payout or self.stake)}"
        elif self.status == "Cashout":
            payout_text = f"Cashout: {pl_money(self.payout or Decimal('0'))}"
        else:
            payout_text = f"Wygrana: {pl_money(self.payout or Decimal('0'))}"
        return {
            "bet_id": self.bet_id,
            "date_text": local.strftime("%d.%m.%Y %H:%M"),
            "event": self.event,
            "market": self.market,
            "selection": self.selection,
            "odds": self.odds,
            "stake_text": pl_money(self.stake),
            "status": self.status,
            "payout_text": payout_text,
        }


def default_events() -> dict[str, MockEvent]:
    main = MockEvent(
        EVENT_ID,
        "Natus Vincere",
        "Vitality",
        [
            MockMarket(
                "M100",
                "Zwycięzca meczu",
                [MockSelection("S1001", "Natus Vincere", "1,85"), MockSelection("S1002", "Vitality", "1,95")],
            ),
            MockMarket(
                "M101",
                "Zwycięzca mapy 1",
                [MockSelection("S1011", "Natus Vincere", "1,80"), MockSelection("S1012", "Vitality", "2,00")],
            ),
            MockMarket(
                "M102",
                "Handicap map",
                [
                    MockSelection("S1021", "Natus Vincere (-1,5)", "2,75"),
                    MockSelection("S1022", "Vitality (+1,5)", "1,40"),
                ],
            ),
            MockMarket(
                "M104",
                "Zwycięzca mapy 2",
                [MockSelection("S1041", "Vitality", "1,90"), MockSelection("S1042", "VITALITY ", "1,95")],
            ),
            MockMarket(
                "M105",
                "Dokładny wynik",
                [
                    MockSelection("S1051", "2:0", "3,20", suspended=True),
                    MockSelection("S1052", "2:1", "4,00"),
                ],
            ),
            MockMarket(
                "M103",
                "Liczba map",
                [
                    MockSelection("S1031", "Powyżej 2,5", "2,10"),
                    MockSelection("S1032", "Poniżej 2,5", "1,70"),
                ],
                only_in_all=True,
            ),
        ],
    )
    other = MockEvent(
        OTHER_EVENT_ID,
        "FaZe",
        "G2",
        [
            MockMarket(
                "M200",
                "Zwycięzca meczu",
                [MockSelection("S2001", "FaZe", "1,70"), MockSelection("S2002", "G2", "2,15")],
            )
        ],
    )
    return {main.id: main, other.id: other}


@dataclass
class MockState:
    logged_in: bool = True
    balance: Decimal = Decimal("1234.56")
    max_stake: Decimal = Decimal("500.00")
    accept_mode: str = "places"  # "places" | "acknowledges"
    events: dict[str, MockEvent] = field(default_factory=default_events)
    slip: list[str] = field(default_factory=list)
    place_script: list[dict[str, Any]] = field(default_factory=list)
    place_calls: list[dict[str, Any]] = field(default_factory=list)
    accept_calls: list[dict[str, Any]] = field(default_factory=list)
    decline_calls: int = 0
    login_form: dict[str, str] = field(default_factory=dict)
    login_submits: int = 0
    bets: list[MockBet] = field(default_factory=list)
    next_bet_no: int = 1001
    history_page_size: int = 5
    slip_add_delay_s: float = 0.15
    last_receipt: str | None = None  # shown in the slip until the user closes it (survives reloads)
    # --- knobs for adversarial tests ---
    show_potential_win: bool = True  # "Możliwa wygrana" line in the slip
    win_factor: Decimal = Decimal("0.88")  # potential win = stake x odds x this (12% stake tax)
    stake_multiplier: Decimal = Decimal("1")  # 100 = the site misreads "12,50" as 1250
    hidden_slip_item: bool = False  # a stale selection kept in the DOM but hidden (display: none)
    slip_market_override: str | None = None  # the slip shows this market name instead of the real one
    extra_slip_notice: str | None = None  # an informational role=alert message in the slip
    place_delay_s: float = 0.0  # the server takes this long to answer a place request
    redirects: dict[str, str] = field(default_factory=dict)  # GET path -> 302 Location

    def find_selection(self, selection_id: str) -> tuple[MockEvent, MockMarket, MockSelection] | None:
        for event in self.events.values():
            for market in event.markets:
                for selection in market.selections:
                    if selection.id == selection_id:
                        return event, market, selection
        return None


class MockBetclicServer:
    """Threaded local HTTP server; ``state`` may be modified by tests (under ``lock``)."""

    def __init__(self) -> None:
        self.state = MockState()
        self.lock = threading.RLock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> MockBetclicServer:
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="mock-betclic", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def reset(self) -> None:
        with self.lock:
            self.state = MockState()

    @property
    def base_url(self) -> str:
        assert self._httpd is not None
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    # ------------------------------------------------------------------ helpers for tests

    def event_path(self, event_id: str = EVENT_ID) -> str:
        return self.state.events[event_id].path

    def script_place(self, *responses: dict[str, Any]) -> None:
        with self.lock:
            self.state.place_script.extend(responses)

    def set_odds(self, selection_id: str, odds: str) -> None:
        with self.lock:
            found = self.state.find_selection(selection_id)
            assert found is not None
            found[2].odds = odds

    def add_bet(self, bet: MockBet) -> None:
        with self.lock:
            self.state.bets.insert(0, bet)

    # ------------------------------------------------------------------ API implementation

    def slip_items(self) -> list[dict[str, str]]:
        items = []
        for sid in self.state.slip:
            found = self.state.find_selection(sid)
            if found is None:
                continue
            event, market, selection = found
            items.append(
                {
                    "selection_id": selection.id,
                    "selection": selection.name,
                    "market": self.state.slip_market_override or market.name,
                    "event": event.name,
                    "odds": selection.odds,
                }
            )
        return items

    def page_data(self, path: str) -> dict[str, Any]:
        st = self.state
        if path in ("", "/"):
            return {"kind": "home"}
        if path.rstrip("/") == "/e-sport-s102":
            return {
                "kind": "listing",
                "events": [{"path": e.path, "team1": e.team1, "team2": e.team2} for e in st.events.values()],
            }
        if path.rstrip("/") == "/moje-zaklady":
            return {"kind": "history", "bets": [b.to_json() for b in st.bets]}
        m = re.search(r"-m(\d+)$", path)
        if m and m.group(1) in st.events:
            event = st.events[m.group(1)]
            return {
                "kind": "event",
                "event": {
                    "id": event.id,
                    "team1": event.team1,
                    "team2": event.team2,
                    "markets": [
                        {
                            "id": mk.id,
                            "name": mk.name,
                            "only_in_all": mk.only_in_all,
                            "selections": [
                                {"id": s.id, "name": s.name, "odds": s.odds, "suspended": s.suspended}
                                for s in mk.selections
                            ],
                        }
                        for mk in event.markets
                    ],
                },
            }
        return {"kind": "not_found"}

    def full_state(self, path: str) -> dict[str, Any]:
        st = self.state
        return {
            "logged_in": st.logged_in,
            "balance_text": pl_money(st.balance),
            "max_stake_text": f"{st.max_stake:.2f}".replace(".", ","),
            "accept_mode": st.accept_mode,
            "history_page_size": st.history_page_size,
            "receipt": st.last_receipt,
            "show_potential_win": st.show_potential_win,
            "win_factor": float(st.win_factor),
            "stake_multiplier": float(st.stake_multiplier),
            "hidden_slip_item": st.hidden_slip_item,
            "extra_slip_notice": st.extra_slip_notice,
            "slip": self.slip_items(),
            "page": self.page_data(path),
        }

    def handle_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        st = self.state
        if path == "/api/slip/add":
            found = st.find_selection(str(body.get("selection_id")))
            if found is None:
                return {"slip": self.slip_items(), "error": "Zakład niedostępny"}
            _, market, selection = found
            if selection.suspended:
                return {"slip": self.slip_items(), "error": "Zakład zawieszony"}
            same_market = {s.id for s in market.selections}
            st.slip = [sid for sid in st.slip if sid not in same_market] + [selection.id]
            return {"slip": self.slip_items()}
        if path == "/api/slip/remove":
            st.slip = [sid for sid in st.slip if sid != body.get("selection_id")]
            return {"slip": self.slip_items()}
        if path == "/api/slip/clear":
            st.slip = []
            return {"slip": self.slip_items()}
        if path == "/api/place":
            return self._place(body)
        if path == "/api/accept_odds":
            st.accept_calls.append({"slip": list(st.slip), "mode": "acknowledge"})
            return {"ok": True}
        if path == "/api/receipt/close":
            st.last_receipt = None
            return {"ok": True}
        if path == "/api/decline":
            st.decline_calls += 1
            return {"ok": True}
        if path == "/api/login_form":
            st.login_form = {
                "username": str(body.get("username", "")),
                "password": str(body.get("password", "")),
            }
            return {"ok": True}
        if path == "/api/login":
            st.login_submits += 1
            st.logged_in = True
            return {"ok": True}
        return {"error": "unknown endpoint"}

    def _place(self, body: dict[str, Any]) -> dict[str, Any]:
        st = self.state
        call = {
            "stake_text": body.get("stake_text"),
            "accept": bool(body.get("accept")),
            "slip": list(st.slip),
        }
        (st.accept_calls if call["accept"] else st.place_calls).append(call)
        if not st.logged_in:
            return {"result": "error", "message": "Musisz być zalogowany, aby postawić zakład."}
        response = dict(st.place_script.pop(0)) if st.place_script else {"result": "ok"}
        if response["result"] == "odds_changed":
            for sid in st.slip:
                found = st.find_selection(sid)
                if found is not None:
                    found[2].odds = response["new_odds"]
            response["slip"] = self.slip_items()
            return response
        if response["result"] != "ok":
            return response
        stake = parse_pl(str(call["stake_text"] or "0")) * st.stake_multiplier
        if stake > st.balance:
            return {
                "result": "error",
                "message": "Niewystarczające środki na koncie. Doładuj konto, aby postawić zakład.",
            }
        if len(st.slip) != 1:
            return {"result": "error", "message": "Kupon musi zawierać jedno zdarzenie"}
        found = st.find_selection(st.slip[0])
        assert found is not None
        event, market, selection = found
        bet_id = f"BC{st.next_bet_no}"
        st.next_bet_no += 1
        st.bets.insert(
            0,
            MockBet(
                bet_id=bet_id,
                placed_at=datetime.now(UTC).replace(second=0, microsecond=0),
                event=event.name,
                market=market.name,
                selection=selection.name,
                odds=selection.odds,
                stake=stake,
            ),
        )
        st.balance -= stake
        st.slip = []
        st.last_receipt = bet_id
        return {
            "result": "ok",
            "bet_id": bet_id,
            "balance_text": pl_money(st.balance),
            "slip": [],
            "note": response.get("note"),  # e.g. "oczekuje na akceptację" (pending trader approval)
        }


def _make_handler(server: MockBetclicServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: dict[str, Any]) -> None:
            self._send(200, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            path = parts.path
            if path.startswith("/static/"):
                name = path.removeprefix("/static/")
                file = SITE_DIR / name
                if not file.is_file() or "/" in name:
                    self._send(404, b"not found", "text/plain")
                    return
                ctype = "application/javascript" if name.endswith(".js") else "text/css"
                self._send(200, file.read_bytes(), f"{ctype}; charset=utf-8")
                return
            if path == "/api/state":
                page = parse_qs(parts.query).get("path", ["/"])[0]
                with server.lock:
                    self._json(server.full_state(page))
                return
            if path == "/api/session":
                with server.lock:
                    self._json(
                        {
                            "logged_in": server.state.logged_in,
                            "balance_text": pl_money(server.state.balance),
                            "slip_odds": {item["selection_id"]: item["odds"] for item in server.slip_items()},
                        }
                    )
                return
            if path == "/favicon.ico":
                self._send(404, b"", "text/plain")
                return
            target = server.state.redirects.get(path)
            if target is not None:
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send(200, (SITE_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            path = urlsplit(self.path).path
            if path == "/api/slip/add":
                time.sleep(server.state.slip_add_delay_s)  # the real site updates the slip asynchronously
            if path == "/api/place" and server.state.place_delay_s > 0:
                time.sleep(server.state.place_delay_s)
            with server.lock:
                payload = server.handle_post(path, body)
            self._json(payload)

    return Handler


def history_bet(
    bet_id: str,
    *,
    days_ago: float,
    status: str = "W grze",
    odds: str = "1,85",
    stake: str = "10.00",
    payout: str | None = None,
    selection: str = "Natus Vincere",
) -> MockBet:
    """Helper for history tests."""
    return MockBet(
        bet_id=bet_id,
        placed_at=(datetime.now(UTC) - timedelta(days=days_ago)).replace(second=0, microsecond=0),
        event="Natus Vincere - Vitality",
        market="Zwycięzca meczu",
        selection=selection,
        odds=odds,
        stake=Decimal(stake),
        status=status,
        payout=Decimal(payout) if payout is not None else None,
    )
