"""Final review, Lens C: deep_data's features reach the model - training, live and backtest stores
are built from the same inputs (demo team stats + DeepData), and the backtest cuts them at every
training cutoff."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from cs2bot.backtest.types import HistoricalData, LeakageError, TrainingSet
from cs2bot.config import Settings
from cs2bot.datasets.deep import DeepData, DuelRecord
from cs2bot.features import FeatureStore
from cs2bot.services import registry
from cs2bot.services.registry import build_feature_store, train_models

T0 = datetime(2026, 3, 1, 12, tzinfo=UTC)


class _Stop(Exception):
    pass


def _capture(monkeypatch: pytest.MonkeyPatch, *, stop: bool) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    original = FeatureStore.build

    def spy(map_results: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        if stop:
            raise _Stop
        return original(map_results, **kwargs)

    monkeypatch.setattr(FeatureStore, "build", staticmethod(spy))
    return calls


def test_training_builds_the_store_with_demo_stats_and_deep_data(
    learning_db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Before the fix training passed neither ``team_demo_stats`` nor ``deep``: every deep column was
    NaN in training, so the ensemble could never use them live."""
    settings = Settings()
    settings.models.artifacts_dir = tmp_path / "models"
    calls = _capture(monkeypatch, stop=True)
    with pytest.raises(_Stop):
        train_models(learning_db, settings, use_ensemble=True, save=False, record_run=False)
    (kwargs,) = calls
    assert isinstance(kwargs["deep"], DeepData)
    assert kwargs["deep"].match_meta, "the match metadata (stage, venue, schedule) must reach training"
    assert "team_demo_stats" in kwargs


def test_live_and_training_stores_get_the_same_inputs(
    learning_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture(monkeypatch, stop=True)
    settings = Settings()
    for _ in range(2):
        with pytest.raises(_Stop):
            build_feature_store(learning_db, settings, until=T0 + timedelta(days=400))
    live, again = calls
    assert set(live) == set(again) == {"player_maps", "rounds", "vetoes", "team_demo_stats", "deep"}


def test_bundle_feature_store_uses_the_shared_builder(
    learning_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []

    def fake(session: Session, settings: Any, *, until: Any, **_: Any) -> str:
        seen.append((settings, until))
        return "store"

    monkeypatch.setattr(registry, "build_feature_store", fake)

    class _Ens:
        is_fallback = False

    bundle = registry.ModelBundle.__new__(registry.ModelBundle)
    registry.ModelBundle.__init__(
        bundle,
        version="v",
        trained_at=T0,
        as_of=T0,
        ratings=None,  # type: ignore[arg-type]
        veto=None,  # type: ignore[arg-type]
        round_model=None,  # type: ignore[arg-type]
        ensemble=_Ens(),  # type: ignore[arg-type]
        settings=Settings(),
    )
    assert bundle.feature_store(learning_db, now=T0) == "store"
    assert seen and seen[0][1] == T0 and isinstance(seen[0][0], Settings)


def _duel(match_id: int, at: datetime) -> DuelRecord:
    return DuelRecord(
        match_id=match_id, map_number=1, map_name="mirage", played_at=at, attacker_id=1, victim_id=2
    )


def test_backtest_training_sets_cut_the_deep_data() -> None:
    deep = DeepData(duels=[_duel(1, T0 - timedelta(days=2)), _duel(2, T0 + timedelta(days=2))])
    data = HistoricalData(deep=deep)
    training = data.until(T0)
    assert [d.match_id for d in training.deep.duels] == [1]
    with pytest.raises(LeakageError):
        TrainingSet(cutoff=T0, deep=deep)
