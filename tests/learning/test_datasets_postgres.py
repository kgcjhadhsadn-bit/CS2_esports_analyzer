"""cs2bot.datasets / cs2bot.features on PostgreSQL (the production database).

Every loader must return exactly what it returns on SQLite. Skipped unless CS2BOT_TEST_DATABASE_URL
is set (``scripts/test.sh --pg``). Review round 1 ran these against a local PostgreSQL 16.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest
from sqlalchemy.orm import Session

from cs2bot import datasets
from cs2bot.config import Settings
from cs2bot.domain.enums import DataSource
from cs2bot.features import FeatureStore, build_training_rows, infer_roles, update_player_roles
from cs2bot.testing.synthetic import SynthUniverse, generate_universe, populate_db
from tests.learning.test_features_roles import T0 as ROLES_T0
from tests.learning.test_features_roles import _persist as persist_role_fixture

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def pg_universe() -> SynthUniverse:
    return generate_universe(n_teams=8, n_matches=60, seed=3, days=150)


def _populate(session: Session, uni: SynthUniverse) -> None:
    populate_db(session, uni, with_rounds=True, with_kills=True)
    session.commit()


def test_loaders_context_actuals_and_rows_agree_with_sqlite(
    pg_session: Session, session: Session, pg_universe: SynthUniverse, settings: Settings
) -> None:
    pg, lite = pg_session, session
    _populate(pg, pg_universe)
    _populate(lite, pg_universe)

    for loader in (
        datasets.load_map_results,
        datasets.load_veto_records,
        datasets.load_round_records,
        datasets.load_player_map_records,
    ):
        on_pg = loader(pg)
        assert on_pg, loader.__name__
        assert on_pg == loader(lite), loader.__name__
    # the GROUP BY of the demo loader (synthetic rounds: accept every source)
    demo_pg = datasets.load_team_demo_stats(pg, sources=None)
    assert demo_pg and demo_pg == datasets.load_team_demo_stats(lite, sources=None)
    assert datasets.load_round_records(pg, sources=[DataSource.DEMO]) == []

    last = max(r.played_at for r in pg_universe.map_results())
    now = last + timedelta(days=1)
    for match in pg_universe.matches[-4:]:
        ctx = datasets.load_match_context(pg, match.id, settings=settings, now=now)
        assert ctx == datasets.load_match_context(lite, match.id, settings=settings, now=now)
        assert datasets.completeness_breakdown(pg, ctx, now=now) == datasets.completeness_breakdown(
            lite, ctx, now=now
        )
        assert datasets.load_match_actuals(pg, match.id) == datasets.load_match_actuals(lite, match.id)

    frames_pg = datasets.load_role_frames(pg, as_of=now)
    frames_lite = datasets.load_role_frames(lite, as_of=now)
    assert len(frames_pg.kills) > 1000 and frames_pg.kills.equals(frames_lite.kills)

    store_pg, store_lite = FeatureStore.from_session(pg), FeatureStore.from_session(lite)
    x_pg, y_pg, meta_pg = build_training_rows(store_pg.map_results, store=store_pg)
    x_lite, y_lite, meta_lite = build_training_rows(store_lite.map_results, store=store_lite)
    assert x_pg.equals(x_lite) and np.array_equal(y_pg, y_lite) and meta_pg.equals(meta_lite)


def test_role_inference_and_update_on_postgres(pg_session: Session, session: Session) -> None:
    pg, lite = pg_session, session
    persist_role_fixture(pg)
    pg.commit()
    persist_role_fixture(lite)
    now = ROLES_T0 + timedelta(days=10)
    assert (
        datasets.available_position_seconds(pg) == datasets.available_position_seconds(lite) == [10, 20, 30]
    )
    roles = infer_roles(pg, as_of=now)
    assert roles and roles == infer_roles(lite, as_of=now)
    assert update_player_roles(pg, as_of=now) == len(roles)
    pg.commit()
    assert update_player_roles(pg) == 0  # idempotent, clock running
