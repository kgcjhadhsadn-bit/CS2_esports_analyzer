"""Shared world for the integration tests: a synthetic history, upcoming matches with Betclic odds.

Building the history and training the models takes a few seconds, so it is done ONCE per test
session into a template SQLite file + artifacts directory; every test gets its own copy of the file.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import cs2bot.db.models as M
from cs2bot.config import Settings
from cs2bot.db.base import Base
from cs2bot.db.session import make_engine
from cs2bot.domain.enums import Bookmaker, DataSource, LinkStatus, MatchFormat, MatchStatus, Tier
from cs2bot.domain.markets import MarketSpec, MarketType
from cs2bot.services.registry import ModelBundle, load_bundle, train_models
from cs2bot.testing.synthetic import generate_universe, populate_db
from cs2bot.utils.time import utcnow

NOW = utcnow().replace(microsecond=0)
KICKOFF_IN = timedelta(hours=6)
# upcoming matches: (team1, team2) - synthetic teams 1..8
UPCOMING_PAIRS = [(1, 2), (3, 4), (5, 6)]


def make_settings(root: Path, *, dry_run: bool = True) -> Settings:
    s = Settings()
    s.database.url = f"sqlite:///{root / 'bot.db'}"
    s.models.artifacts_dir = root / "models"
    s.models.n_map_simulations = 400
    s.models.scenario_n_simulations = 300
    s.models.top_fragger_samples = 400
    s.executor.dry_run = dry_run
    s.executor.screenshot_dir = root / "shots"
    # decision thresholds relaxed so that the synthetic odds set by the tests decide acceptance
    s.decision.min_probability = 0.55
    s.decision.max_sigma = 0.25
    s.decision.min_ev = 0.02
    s.data_dir = root / "data"
    return s


def market_specs() -> list[tuple[MarketSpec, list[str], str]]:
    """(spec, selection keys, Betclic market name) offered for every upcoming match."""
    return [
        (MarketSpec(market_type=MarketType.MATCH_WINNER), ["team1", "team2"], "Zwycięzca meczu"),
        (
            MarketSpec(market_type=MarketType.CORRECT_MAP_SCORE),
            ["2-0", "2-1", "1-2", "0-2"],
            "Dokładny wynik",
        ),
        (MarketSpec(market_type=MarketType.TOTAL_MAPS, line=2.5), ["over", "under"], "Liczba map"),
        (MarketSpec(market_type=MarketType.MAP_WINNER, map_number=1), ["team1", "team2"], "Mapa 1 - Zwycięzca"),
    ]


def add_upcoming(session: Session, uni: object, now: datetime) -> dict[int, int]:
    """Upcoming BO3 matches with lineups and a linked Betclic event; returns match id -> event id."""
    out: dict[int, int] = {}
    teams = uni.teams  # type: ignore[attr-defined]
    for i, (a, b) in enumerate(UPCOMING_PAIRS):
        match = M.Match(
            team1_id=a,
            team2_id=b,
            scheduled_at=now + KICKOFF_IN + timedelta(minutes=10 * i),
            status=MatchStatus.SCHEDULED,
            format=MatchFormat.BO3,
            tier=Tier.S,
            in_scope=True,
        )
        session.add(match)
        session.flush()
        for tid in (a, b):
            for p in teams[tid].players:
                session.add(M.MatchLineup(match_id=match.id, team_id=tid, player_id=p.id, source=DataSource.SYNTHETIC))
        event = M.BookmakerEvent(
            bookmaker=Bookmaker.BETCLIC,
            external_id=f"ev{match.id}",
            name=f"Synth Team {a} - Synth Team {b}",
            team1_raw=f"Synth Team {a}",
            team2_raw=f"Synth Team {b}",
            starts_at=match.scheduled_at,
            url=f"https://www.betclic.pl/e-sport/cs2-ev{match.id}",
            match_id=match.id,
            link_confidence=0.99,
            link_status=LinkStatus.AUTO,
            first_seen_at=now - timedelta(hours=1),
            last_seen_at=now,
        )
        session.add(event)
        session.flush()
        for spec, selections, name in market_specs():
            market = M.BookmakerMarket(
                event_id=event.id,
                external_id=f"{event.external_id}-{spec.key()}",
                raw_name=name,
                market_type=spec.market_type.value,
                spec=spec.model_dump(mode="json"),
                spec_key=spec.key(),
                is_mapped=True,
                first_seen_at=now - timedelta(hours=1),
                last_seen_at=now,
            )
            session.add(market)
            session.flush()
            for sel in selections:
                session.add(
                    M.BookmakerSelection(
                        market_id=market.id,
                        external_id=f"{market.id}-{sel}",
                        raw_name=f"{name}: {sel}",
                        selection_key=sel,
                        is_mapped=True,
                        last_odds=Decimal("1.01"),
                        last_odds_at=now - timedelta(hours=1),
                        last_seen_at=now - timedelta(hours=1),
                        is_available=True,
                    )
                )
        out[match.id] = event.id
    session.flush()
    return out


@dataclass
class Template:
    root: Path
    bundle_path: Path
    events: dict[int, int]


_TEMPLATE: dict[str, Template] = {}


def _build_template(root: Path) -> Template:
    settings = make_settings(root)
    engine = make_engine(settings.database.url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    uni = generate_universe(n_teams=8, n_matches=220, seed=3, start=NOW - timedelta(days=200), days=195)
    with factory() as session:
        populate_db(session, uni)
        events = add_upcoming(session, uni, NOW)
        session.commit()
        report = train_models(session, settings, as_of=NOW, use_ensemble=False)
        session.commit()
    engine.dispose()
    assert report.path is not None
    return Template(root=root, bundle_path=report.path, events=events)


@pytest.fixture(scope="session")
def world_template(tmp_path_factory: pytest.TempPathFactory) -> Template:
    if "t" not in _TEMPLATE:
        _TEMPLATE["t"] = _build_template(tmp_path_factory.mktemp("world_template"))
    return _TEMPLATE["t"]


@dataclass
class World:
    root: Path
    settings: Settings
    factory: sessionmaker[Session]
    bundle: ModelBundle
    events: dict[int, int]
    now: datetime

    @property
    def match_ids(self) -> list[int]:
        return sorted(self.events)

    def selection(self, session: Session, match_id: int, spec_key: str, selection_key: str) -> M.BookmakerSelection:
        sel = session.scalar(
            select(M.BookmakerSelection)
            .join(M.BookmakerMarket, M.BookmakerMarket.id == M.BookmakerSelection.market_id)
            .where(
                M.BookmakerMarket.event_id == self.events[match_id],
                M.BookmakerMarket.spec_key == spec_key,
                M.BookmakerSelection.selection_key == selection_key,
            )
        )
        assert sel is not None
        return sel

    def set_odds(self, match_id: int, odds: dict[tuple[str, str], Decimal | float | str], *, at: datetime) -> None:
        with self.factory() as session:
            for (spec_key, sel_key), value in odds.items():
                sel = self.selection(session, match_id, spec_key, sel_key)
                price = Decimal(str(value))
                sel.last_odds = price
                sel.last_odds_at = at
                sel.last_seen_at = at
                session.add(M.OddsSnapshot(selection_id=sel.id, odds=price, is_available=True, captured_at=at))
            session.commit()


@pytest.fixture
def world(world_template: Template, tmp_path: Path) -> Iterator[World]:
    root = tmp_path
    shutil.copy(world_template.root / "bot.db", root / "bot.db")
    shutil.copytree(world_template.root / "models", root / "models")
    settings = make_settings(root)
    engine = make_engine(settings.database.url)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    bundle = load_bundle(root / "models" / world_template.bundle_path.name)
    try:
        yield World(root=root, settings=settings, factory=factory, bundle=bundle, events=dict(world_template.events), now=NOW)
    finally:
        engine.dispose()


def fair_odds(p: float, ev: float, tax: float = 0.12) -> Decimal:
    """Odds giving expected value ``ev`` per unit after the stake tax for win probability ``p``."""
    return Decimal(str(round((1.0 + ev) / (p * (1.0 - tax)), 2)))
