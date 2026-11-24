"""Model lifecycle: train every model, save the artifacts, load the latest bundle, hot-swap it.

Training (:func:`train_models`) uses only data strictly before ``as_of`` (the loaders' ``until``),
so a retrain can never see the future. It fits:

* :class:`~cs2bot.models.ratings.MapRatingModel` (per-team, per-map Glicko-2) with recorded history,
  so the veto model and the ensemble can ask it about past dates without leakage;
* :class:`~cs2bot.models.veto.VetoModel` (with the ratings' historical map advantage);
* :class:`~cs2bot.models.round_model.RoundModel` (priors only when no round data exists);
* :class:`~cs2bot.models.player_props.PlayerKillModel` (when player stat lines exist);
* :class:`~cs2bot.models.ensemble.MapWinEnsemble` when at least ``EnsembleConfig.min_rows`` labelled
  maps exist (it falls back to the ratings by itself when training fails).

Artifacts go to ``settings.models.artifacts_dir/<version>/`` together with ``manifest.json``; a
``model_runs`` row (kind ``train``) records the parameters and metrics. :func:`load_latest` reads the
newest complete manifest. :class:`ModelRegistry` holds the active :class:`ModelBundle` and swaps it
atomically after the nightly retrain (readers keep the bundle object they already hold).

Between retrains :meth:`ModelBundle.refresh` keeps the rating model current (new results are applied
incrementally in time order) and rebuilds the ensemble's feature store when it is older than
``store_max_age``.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from cs2bot import datasets
from cs2bot.config import Settings
from cs2bot.db.models import ModelRun
from cs2bot.domain.enums import JobStatus
from cs2bot.models.ensemble import EnsembleConfig, MapWinEnsemble
from cs2bot.models.player_props import PlayerKillModel
from cs2bot.models.ratings import MapRatingModel, RatingConfig
from cs2bot.models.round_model import RoundModel
from cs2bot.models.veto import VetoModel
from cs2bot.utils.paths import resolve_path
from cs2bot.utils.time import ensure_utc, utcnow

log = logging.getLogger(__name__)

MANIFEST = "manifest.json"
RATINGS_FILE = "ratings.json"
VETO_FILE = "veto.json"
ROUND_FILE = "round_model.json"
KILL_FILE = "kill_model.npz"
ENSEMBLE_FILE = "ensemble.json"
#: Optional tuned rating configuration written by ``cs2bot models tune`` and used by the next training.
RATING_CONFIG_FILE = "rating_config.json"
#: Bumped when the artifact layout changes; older bundles are ignored by :func:`load_latest`.
BUNDLE_FORMAT = 1


def artifacts_root(settings: Settings) -> Path:
    return resolve_path(settings.models.artifacts_dir)


@dataclass
class ModelBundle:
    """Everything the prediction service needs, fitted on data before ``data_end``."""

    version: str
    trained_at: datetime
    as_of: datetime
    ratings: MapRatingModel
    veto: VetoModel
    round_model: RoundModel
    kill_model: PlayerKillModel | None = None
    ensemble: MapWinEnsemble | None = None
    path: Path | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    model_run_id: int | None = None
    store_max_age: timedelta = timedelta(minutes=30)
    #: runtime only (not saved): the settings the live feature store is built with (sharp feed);
    #: set by :func:`train_models` and :class:`ModelRegistry`
    settings: Settings | None = field(default=None, repr=False, compare=False)
    # runtime state (not saved)
    _store: Any = field(default=None, repr=False)
    _store_built_at: datetime | None = field(default=None, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @property
    def uses_ensemble(self) -> bool:
        return self.ensemble is not None and not self.ensemble.is_fallback

    @property
    def lock(self) -> threading.RLock:
        """Held while the bundle's mutable state (ratings, feature store) is updated or used."""
        return self._lock

    # ------------------------------------------------------------------ runtime refresh
    def refresh(self, session: Session, *, now: datetime | None = None) -> int:
        """Apply results that arrived since training to the rating model; returns the maps applied.

        Only maps played strictly before ``now`` are used. A result older than the latest map the
        model processed cannot be applied incrementally (the model must see games in time order); it
        is skipped and waits for the next retrain.
        """
        now = ensure_utc(now or utcnow())
        with self._lock:
            last = self.ratings.last_processed
            since = last if last is not None else self.as_of
            try:
                records = datasets.load_map_results(session, since=since, until=now)
            except Exception:  # defensive: a broken row must not stop predictions
                log.exception("could not load new map results for the rating refresh")
                return 0
            applied = 0
            for rec in sorted(records, key=lambda r: (ensure_utc(r.played_at), r.match_id, r.map_number)):
                try:
                    if self.ratings.update(rec):
                        applied += 1
                except ValueError as exc:  # out of order: wait for the retrain
                    log.debug("rating refresh skipped map %s/%s: %s", rec.match_id, rec.map_number, exc)
            if applied:
                log.info("rating model %s: applied %d new map results", self.version, applied)
            return applied

    def feature_store(self, session: Session, *, now: datetime | None = None) -> Any:
        """The ensemble's :class:`~cs2bot.features.FeatureStore`, rebuilt when older than ``store_max_age``.

        Returns None when the bundle has no usable ensemble.
        """
        if not self.uses_ensemble:
            return None
        now = ensure_utc(now or utcnow())
        with self._lock:
            fresh = self._store_built_at is not None and now - self._store_built_at < self.store_max_age
            if self._store is None or not fresh:
                # the same inputs as training (build_feature_store): team demo stats + deep data
                self._store = build_feature_store(session, self.settings, until=now)
                self._store_built_at = now
            return self._store

    def set_feature_store(self, store: Any, built_at: datetime) -> None:
        with self._lock:
            self._store = store
            self._store_built_at = ensure_utc(built_at)

    def summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "trained_at": self.trained_at.isoformat(),
            "as_of": self.as_of.isoformat(),
            "ensemble": self.uses_ensemble,
            "kill_model": self.kill_model is not None,
            "path": str(self.path) if self.path else None,
        }


@dataclass
class TrainReport:
    version: str
    as_of: datetime
    trained_at: datetime
    path: Path | None
    model_run_id: int | None
    n_map_results: int
    n_vetoes: int
    n_rounds: int
    n_player_maps: int
    ensemble_fitted: bool
    ensemble_fallback: bool | None
    metrics: dict[str, Any]
    warnings: list[str]
    bundle: ModelBundle | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "as_of": self.as_of.isoformat(),
            "trained_at": self.trained_at.isoformat(),
            "path": str(self.path) if self.path else None,
            "model_run_id": self.model_run_id,
            "n_map_results": self.n_map_results,
            "n_vetoes": self.n_vetoes,
            "n_rounds": self.n_rounds,
            "n_player_maps": self.n_player_maps,
            "ensemble_fitted": self.ensemble_fitted,
            "ensemble_fallback": self.ensemble_fallback,
            "metrics": self.metrics,
            "warnings": self.warnings,
        }


class NotEnoughDataError(RuntimeError):
    """There is not a single usable map result before ``as_of``: nothing to train on."""


def build_feature_store(
    session: Session,
    settings: Settings | None,
    *,
    until: datetime | None,
    map_results: Any = None,
    player_maps: Any = None,
    rounds: Any = None,
    vetoes: Any = None,
) -> Any:
    """The ensemble's :class:`~cs2bot.features.FeatureStore` with EVERY input, for training and live alike.

    Final review (Lens C): training used to build its store from map results, player lines, rounds and
    vetoes only, while the live store (``FeatureStore.from_session``) also had the demo team stats and
    deep_data's :class:`~cs2bot.datasets.DeepData` (match metadata - stage, venue, schedule - demo duels
    and clutches, coach changes, Betclic market history, the optional sharp feed). The ensemble only
    knows the columns it was trained with, so the deep factors never reached a prediction (all-NaN in
    training) and the demo-stat columns were a train / live skew. One builder for both paths keeps them
    identical. Records already loaded by the caller are reused; everything is cut at ``until``.
    """
    from cs2bot.features import FeatureStore

    def get(pre: Any, loader: Any) -> list[Any]:
        return list(pre) if pre is not None else loader(session, until=until)

    sharp = None
    if settings is not None:
        try:
            from cs2bot.odds.sharp import sharp_source_from_settings

            sharp = sharp_source_from_settings(settings)
        except Exception:  # an optional benchmark feed must never stop training or predictions
            log.warning("sharp odds source unavailable; its features stay empty", exc_info=True)
    return FeatureStore.build(
        get(map_results, datasets.load_map_results),
        player_maps=get(player_maps, datasets.load_player_map_records),
        rounds=get(rounds, datasets.load_round_records),
        vetoes=get(vetoes, datasets.load_veto_records),
        team_demo_stats=datasets.load_team_demo_stats(session, until=until),
        deep=datasets.DeepData.from_session(session, until=until, sharp=sharp),
    )


def _new_version(root: Path, as_of: datetime) -> str:
    base = f"m{ensure_utc(as_of):%Y%m%dT%H%M%S}"
    version, n = base, 1
    while (root / version).exists():
        n += 1
        version = f"{base}-{n}"
    return version


def load_rating_config(settings: Settings) -> RatingConfig | None:
    """The tuned rating configuration saved by ``cs2bot models tune``, if any (bad files are ignored)."""
    path = artifacts_root(settings) / RATING_CONFIG_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return RatingConfig.model_validate(data.get("config", data))
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable tuned rating config %s: %s", path, exc)
        return None


def save_rating_config(settings: Settings, config: RatingConfig, report: dict[str, Any]) -> Path:
    root = artifacts_root(settings)
    root.mkdir(parents=True, exist_ok=True)
    path = root / RATING_CONFIG_FILE
    payload = {"config": config.model_dump(mode="json"), "report": report, "saved_at": utcnow().isoformat()}
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def train_models(
    session: Session,
    settings: Settings,
    *,
    as_of: datetime | None = None,
    use_ensemble: bool | None = None,
    ensemble_config: EnsembleConfig | None = None,
    save: bool = True,
    record_run: bool = True,
) -> TrainReport:
    """Fit every model on in-scope data strictly before ``as_of`` (default: now).

    ``use_ensemble=None`` fits the ensemble only when there are at least ``EnsembleConfig.min_rows``
    labelled maps. Artifacts are written under ``settings.models.artifacts_dir/<version>/`` (``save``)
    and a ``model_runs`` row is written and flushed (``record_run``; the caller commits). Raises
    :class:`NotEnoughDataError` when there is no map result at all.
    """
    as_of = ensure_utc(as_of or utcnow())
    started = utcnow()
    root = artifacts_root(settings)
    version = _new_version(root, as_of)
    warnings: list[str] = []
    run: ModelRun | None = None
    if record_run:
        run = ModelRun(
            kind="train",
            model_version=version,
            status=JobStatus.RUNNING,
            started_at=started,
            params={"as_of": as_of.isoformat()},
        )
        session.add(run)
        session.flush()
    try:
        map_results = datasets.load_map_results(session, until=as_of)
        if not map_results:
            raise NotEnoughDataError("no in-scope map results before as_of - ingest results first")
        vetoes = datasets.load_veto_records(session, until=as_of)
        rounds = datasets.load_round_records(session, until=as_of)
        player_maps = datasets.load_player_map_records(session, until=as_of)

        ms = settings.models
        rating_config = load_rating_config(settings)
        ratings = MapRatingModel(rating_config).fit(map_results, record_history=True)
        veto = VetoModel()
        if vetoes:
            veto.fit(vetoes, map_advantage=ratings.historical_map_advantage)
        else:
            warnings.append("no veto history: the veto model uses its league prior only")
        if rounds:
            round_model = RoundModel().fit(rounds, as_of=as_of, half_life_days=ms.round_model_half_life_days)
        else:
            round_model = RoundModel.default()
            warnings.append("no round data: the round model uses priors only")
        kill_model: PlayerKillModel | None = None
        if player_maps:
            kill_model = PlayerKillModel().fit(player_maps, half_life_days=ms.kill_model_half_life_days)
        else:
            warnings.append("no player stat lines: player prop markets are not priced")

        cfg = ensemble_config or EnsembleConfig()
        want_ensemble = use_ensemble if use_ensemble is not None else len(map_results) >= cfg.min_rows
        ensemble: MapWinEnsemble | None = None
        store = None
        ensemble_metrics: dict[str, Any] | None = None
        if want_ensemble:
            store = build_feature_store(
                session,
                settings,
                until=as_of,
                map_results=map_results,
                player_maps=player_maps,
                rounds=rounds,
                vetoes=vetoes,
            )
            ensemble = MapWinEnsemble(cfg, settings=settings)
            fit_report = ensemble.fit(map_results, store=store, rating_provider=ratings, as_of=as_of)
            ensemble_metrics = fit_report.model_dump(mode="json")
            if ensemble.is_fallback:
                warnings.append(f"ensemble fell back to the ratings: {fit_report.fallback_reason}")

        metrics: dict[str, Any] = {
            "n_map_results": len(map_results),
            "n_vetoes": len(vetoes),
            "n_rounds": len(rounds),
            "n_player_maps": len(player_maps),
            "ratings": {"n_processed": ratings.n_processed, "teams": len(ratings.team_ids)},
            "veto": {"n_records": veto.n_records},
            "round_model": dict(round_model.fit_stats),
            "kill_model": dict(kill_model.fit_stats) if kill_model is not None else None,
            "ensemble": ensemble_metrics,
            "rating_config": "tuned" if rating_config is not None else "default",
        }
        trained_at = utcnow()
        bundle = ModelBundle(
            version=version,
            trained_at=trained_at,
            as_of=as_of,
            ratings=ratings,
            veto=veto,
            round_model=round_model,
            kill_model=kill_model,
            ensemble=ensemble,
            metrics=metrics,
            model_run_id=run.id if run is not None else None,
            settings=settings,
        )
        if store is not None:
            bundle.set_feature_store(store, as_of)
        path: Path | None = None
        if save:
            path = save_bundle(bundle, root)
            bundle.path = path
        if run is not None:
            run.status = JobStatus.SUCCESS
            run.finished_at = utcnow()
            run.metrics = json.loads(json.dumps(metrics, default=str))
            run.artifact_path = str(path) if path else None
            run.notes = "; ".join(warnings) or None
            session.flush()
        return TrainReport(
            version=version,
            as_of=as_of,
            trained_at=trained_at,
            path=path,
            model_run_id=bundle.model_run_id,
            n_map_results=len(map_results),
            n_vetoes=len(vetoes),
            n_rounds=len(rounds),
            n_player_maps=len(player_maps),
            ensemble_fitted=ensemble is not None,
            ensemble_fallback=ensemble.is_fallback if ensemble is not None else None,
            metrics=metrics,
            warnings=warnings,
            bundle=bundle,
        )
    except Exception as exc:
        if run is not None:
            run.status = JobStatus.FAILED
            run.finished_at = utcnow()
            run.notes = f"{type(exc).__name__}: {exc}"[:2000]
            session.flush()
        raise


def save_bundle(bundle: ModelBundle, root: Path) -> Path:
    """Write the artifacts and, last, the manifest (a directory without manifest is incomplete)."""
    directory = root / bundle.version
    tmp = root / f".{bundle.version}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    bundle.ratings.save(tmp / RATINGS_FILE)
    bundle.veto.save(tmp / VETO_FILE)
    bundle.round_model.save(tmp / ROUND_FILE)
    if bundle.kill_model is not None:
        bundle.kill_model.save(tmp / KILL_FILE)
    if bundle.ensemble is not None:
        bundle.ensemble.save(tmp / ENSEMBLE_FILE)
    manifest = {
        "format": BUNDLE_FORMAT,
        "version": bundle.version,
        "trained_at": bundle.trained_at.isoformat(),
        "as_of": bundle.as_of.isoformat(),
        "model_run_id": bundle.model_run_id,
        "files": {
            "ratings": RATINGS_FILE,
            "veto": VETO_FILE,
            "round_model": ROUND_FILE,
            "kill_model": KILL_FILE if bundle.kill_model is not None else None,
            "ensemble": ENSEMBLE_FILE if bundle.ensemble is not None else None,
        },
        "metrics": bundle.metrics,
    }
    (tmp / MANIFEST).write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    if directory.exists():
        shutil.rmtree(directory)
    tmp.rename(directory)
    return directory


def load_bundle(directory: Path) -> ModelBundle:
    """Load one saved bundle (raises on missing / corrupt files)."""
    manifest = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    if manifest.get("format") != BUNDLE_FORMAT:
        raise ValueError(f"unsupported bundle format {manifest.get('format')!r} in {directory}")
    files = manifest["files"]
    kill_file = files.get("kill_model")
    ens_file = files.get("ensemble")
    return ModelBundle(
        version=str(manifest["version"]),
        trained_at=ensure_utc(datetime.fromisoformat(manifest["trained_at"])),
        as_of=ensure_utc(datetime.fromisoformat(manifest["as_of"])),
        ratings=MapRatingModel.load(directory / files["ratings"]),
        veto=VetoModel.load(directory / files["veto"]),
        round_model=RoundModel.load(directory / files["round_model"]),
        kill_model=PlayerKillModel.load(directory / kill_file) if kill_file else None,
        ensemble=MapWinEnsemble.load(directory / ens_file) if ens_file else None,
        path=directory,
        metrics=dict(manifest.get("metrics") or {}),
        model_run_id=manifest.get("model_run_id"),
    )


def list_bundles(settings: Settings) -> list[tuple[datetime, Path]]:
    """Complete bundle directories (with a manifest), newest training first."""
    root = artifacts_root(settings)
    if not root.is_dir():
        return []
    found: list[tuple[datetime, Path]] = []
    for directory in root.iterdir():
        manifest = directory / MANIFEST
        if not directory.is_dir() or directory.name.startswith(".") or not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            found.append((ensure_utc(datetime.fromisoformat(data["trained_at"])), directory))
        except (OSError, ValueError, KeyError) as exc:
            log.warning("ignoring unreadable model manifest %s: %s", manifest, exc)
    return sorted(found, key=lambda item: item[0], reverse=True)


def load_latest(settings: Settings) -> ModelBundle | None:
    """The newest bundle that loads cleanly, or None (a corrupt newest bundle falls back to older ones)."""
    for _, directory in list_bundles(settings):
        try:
            bundle = load_bundle(directory)
            bundle.settings = settings
            return bundle
        except Exception as exc:
            log.error("could not load model bundle %s: %s", directory, exc)
    return None


def prune_bundles(settings: Settings, *, keep: int = 5) -> int:
    """Delete all but the ``keep`` newest bundles; returns how many were removed."""
    removed = 0
    for _, directory in list_bundles(settings)[keep:]:
        try:
            shutil.rmtree(directory)
            removed += 1
        except OSError as exc:
            log.warning("could not delete old model bundle %s: %s", directory, exc)
    return removed


class ModelRegistry:
    """Thread-safe holder of the active :class:`ModelBundle`.

    ``get()`` returns the current bundle object; a retrain builds a complete new bundle and swaps the
    reference under a lock, so a prediction that is running keeps using the bundle it started with.
    """

    def __init__(self, settings: Settings, bundle: ModelBundle | None = None) -> None:
        self.settings = settings
        self._bundle = bundle
        self._lock = threading.Lock()
        self._train_lock = threading.RLock()  # re-entered by retrain_unless_fresh -> retrain

    def get(self) -> ModelBundle | None:
        with self._lock:
            return self._bundle

    def swap(self, bundle: ModelBundle) -> ModelBundle | None:
        if bundle.settings is None:
            bundle.settings = self.settings
        with self._lock:
            old, self._bundle = self._bundle, bundle
        log.info("active model bundle: %s (was %s)", bundle.version, old.version if old else None)
        return old

    def load_latest(self) -> ModelBundle | None:
        bundle = load_latest(self.settings)
        if bundle is not None:
            self.swap(bundle)
        return bundle

    def retrain(
        self,
        session_factory: sessionmaker[Session],
        *,
        as_of: datetime | None = None,
        use_ensemble: bool | None = None,
        keep: int = 5,
    ) -> TrainReport:
        """Train a new bundle, commit its ``model_runs`` row and make it active (one retrain at a time)."""
        with self._train_lock:
            with session_factory() as session:
                try:
                    report = train_models(session, self.settings, as_of=as_of, use_ensemble=use_ensemble)
                    session.commit()
                except Exception:
                    session.commit()  # keep the FAILED model_runs row
                    raise
            if report.bundle is not None:
                self.swap(report.bundle)
            prune_bundles(self.settings, keep=keep)
            return report

    def retrain_unless_fresh(
        self, session_factory: sessionmaker[Session], *, max_age: timedelta, keep: int = 5
    ) -> TrainReport | None:
        """:meth:`retrain`, or None when the active bundle was trained less than ``max_age`` ago.

        Checked inside the train lock, so the scheduler's retrain job that waited for the start-up
        training does not train the same data a second time right after it (final review, Lens C).
        """
        with self._train_lock:
            current = self.get()
            if current is not None and utcnow() - ensure_utc(current.trained_at) < max_age:
                log.info("retrain skipped: bundle %s is fresh", current.version)
                return None
            return self.retrain(session_factory, keep=keep)
