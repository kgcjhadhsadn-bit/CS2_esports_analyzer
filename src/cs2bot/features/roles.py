"""Player roles inferred from demo data (kills, per-round rows, position samples).

HLTV and PandaScore do not publish roles, so they are estimated from what players actually do:

=========  ======================================================================================
AWPER      share of the player's kills made with the AWP
ENTRY      T side: how often the player takes part in the opening duel of the round
LURKER     T side: distance from the rest of the team mid-round, plus kills late in the round
ANCHOR     CT side: sets up at the same bomb site (almost) every round
ROTATOR    CT side: no fixed site - plays the middle of the map / moves between the sites
SUPPORT    none of the above and rarely in the opening duel (flashes more than the typical player)
RIFLER     none of the above
=========  ======================================================================================

``IGL`` cannot be seen in this data and is never returned; :func:`update_player_roles` keeps a
stored IGL role untouched.

A player has one ``primary_role`` in the schema although he really has a T-side and a CT-side job.
The rule: AWPER wins whenever it applies (it shapes both sides); otherwise the candidate with the
strongest evidence among ENTRY / LURKER / ANCHOR / ROTATOR; otherwise SUPPORT or RIFLER with a low
confidence. The per-role evidence behind the decision is available from :func:`role_metrics`.

Assumptions (all thresholds live in :class:`RoleConfig`; they follow public HLTV-style statistics of
professional players and have not been fitted to labelled data - there is no labelled data):

* an average player is in 20 % of his team's opening duels (5 players, one duel per round);
  dedicated entries are around 25-32 %, supports and anchors around 10-15 %;
* dedicated AWPers make roughly half or more of their kills with the AWP, riflers under 10 %;
* place names follow Valve's navigation areas as reported by ``demoparser2`` (``last_place_name``):
  every defuse map has ``BombsiteA`` and ``BombsiteB``. Other places are attributed to the nearer
  site by their average coordinates when one site is clearly closer, else they count as "middle".
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from cs2bot.datasets.demo import RoleFrames, available_position_seconds, load_role_frames
from cs2bot.db.models import Player
from cs2bot.domain.enums import PlayerRole
from cs2bot.utils.time import ensure_utc, utcnow

#: ``kills.weapon`` values that mean the AWP.
AWP_WEAPONS: Final[frozenset[str]] = frozenset({"awp", "weapon_awp"})
_ROUND_KEYS: Final[list[str]] = ["map_played_id", "round_number"]
_ROUND_COLS: Final[list[str]] = ["map_played_id", "round_number", "player_id", "side"]
_IN_CHUNK: Final[int] = 500
ROLE_INFERRED_AT_KEY: Final[str] = "role_inferred_at"
#: A label re-confirmed with an unchanged confidence gets a fresh timestamp at most this often.
_REFRESH_AFTER: Final[timedelta] = timedelta(days=1)

METRIC_COLUMNS: Final[tuple[str, ...]] = (
    "rounds",
    "t_rounds",
    "ct_rounds",
    "kills",
    "weapon_kills",
    "awp_share",
    "t_opening_attempts",
    "t_opening_rate",
    "t_timed_kills",
    "late_kill_share",
    "ct_setup_samples",
    "ct_site_share",
    "t_isolation",
    "t_isolation_rounds",
    "flashed_per_round",
)


@dataclass(frozen=True)
class RoleConfig:
    """Thresholds of the role rules. ``x_min`` = the rule applies from here, ``x_full`` = full score."""

    #: Players with fewer rounds in the window get no role at all (about three maps).
    min_rounds: int = 60
    #: Rounds at which the sample-size factor of the confidence reaches 1 (about eleven maps).
    full_confidence_rounds: int = 250
    #: Minimum rounds on one side / samples for a side-specific rule to apply.
    min_side_rounds: int = 30
    #: Minimum kills with a known weapon / with a known time for the kill-based rules.
    min_kills: int = 25
    awp_share_min: float = 0.35
    awp_share_full: float = 0.55
    entry_rate_min: float = 0.25
    entry_rate_full: float = 0.32
    #: Second after freeze time at which CT setups are read (nearest available sample is used).
    setup_second: int = 20
    anchor_share_min: float = 0.70
    anchor_share_full: float = 0.90
    rotator_share_max: float = 0.50
    rotator_share_full: float = 0.25
    #: A place belongs to a site when it is at most this fraction as far from it as from the other.
    site_distance_ratio: float = 0.75
    #: Second after freeze time at which T-side spacing is read.
    lurk_second: int = 30
    #: Distance to the rest of the team relative to the team average in that round (1 = typical).
    isolation_min: float = 1.35
    isolation_full: float = 1.90
    #: Kills at or after this many seconds into the round count as "late".
    late_kill_seconds: float = 60.0
    #: Late-kill share that makes a lurker when no position samples exist (weaker evidence).
    late_share_min: float = 0.50
    late_share_full: float = 0.70
    support_rate_max: float = 0.15
    support_rate_full: float = 0.08
    fallback_confidence: float = 0.30


def _ramp(x: float, lo: float, hi: float) -> float:
    """0 at ``lo``, 1 at ``hi``, linear in between (``lo`` may be above ``hi`` for falling scales)."""
    if not math.isfinite(x) or lo == hi:
        return 0.0
    return float(min(1.0, max(0.0, (x - lo) / (hi - lo))))


def _norm_place(place: object) -> str:
    return "".join(ch for ch in str(place).lower() if ch.isalnum())


def _nearest(available: list[int], target: int) -> int | None:
    return min(available, key=lambda s: (abs(s - target), s)) if available else None


def _segment(round_number: np.ndarray) -> np.ndarray:
    """Index of the side-constant block a round belongs to (MR12 halves, then MR3 overtime halves)."""
    r = np.asarray(round_number, dtype=np.int64)
    return np.where(r <= 12, 0, np.where(r <= 24, 1, 2 + (r - 25) // 3))


def _rounds_from_kills(kills: pd.DataFrame) -> pd.DataFrame:
    """Rounds played per player and side, reconstructed from kill events alone.

    A player's side is constant within a half (rounds 1-12, 13-24, then blocks of three overtime
    rounds), so one kill or death in a half reveals his side for all of its rounds. Every round of
    the map that has at least one kill counts as played. Only used for maps that have neither
    per-round player rows nor position samples (synthetic data).
    """
    att = kills[["map_played_id", "round_number", "attacker_player_id", "attacker_side"]]
    att = att.set_axis(_ROUND_COLS, axis=1)
    vic = kills[["map_played_id", "round_number", "victim_player_id", "victim_side"]]
    vic = vic.set_axis(_ROUND_COLS, axis=1)
    events = pd.concat([att, vic], ignore_index=True).dropna(subset=["player_id", "side"])
    if events.empty:
        return pd.DataFrame(columns=_ROUND_COLS)
    events = events.assign(segment=_segment(events["round_number"].to_numpy()))
    sides = events.drop_duplicates(["map_played_id", "player_id", "segment"])[
        ["map_played_id", "player_id", "segment", "side"]
    ]
    rounds = kills[_ROUND_KEYS].drop_duplicates()
    rounds = rounds.assign(segment=_segment(rounds["round_number"].to_numpy()))
    out = rounds.merge(sides, on=["map_played_id", "segment"], how="inner")
    return out[_ROUND_COLS]


def _rounds_by_side(
    kills: pd.DataFrame, positions: pd.DataFrame, player_rounds: pd.DataFrame
) -> pd.DataFrame:
    """One row per (map, round, player) with his side; the best source available for each map."""
    parts: list[pd.DataFrame] = []
    covered: set[int] = set()
    for frame in (player_rounds, positions):
        if frame.empty:
            continue
        rest = frame[~frame["map_played_id"].isin(covered)]
        if rest.empty:
            continue
        parts.append(rest[_ROUND_COLS].drop_duplicates(["map_played_id", "round_number", "player_id"]))
        covered |= set(rest["map_played_id"].unique().tolist())
    if not kills.empty:
        rest = kills[~kills["map_played_id"].isin(covered)]
        if not rest.empty:
            parts.append(_rounds_from_kills(rest))
    if not parts:
        return pd.DataFrame(columns=_ROUND_COLS)
    return pd.concat(parts, ignore_index=True).dropna(subset=["player_id", "side"])


def _zone_table(positions: pd.DataFrame, ratio: float) -> pd.DataFrame:
    """(map_name, place) -> zone ``"A"`` / ``"B"`` / ``"mid"``.

    ``BombsiteA`` / ``BombsiteB`` are the sites themselves. Any other place is attributed to a site
    when its average position is clearly closer to that site's average position (distance ratio at
    most ``ratio``); everything else, and every place on a map without usable coordinates, is
    ``"mid"``. Coordinates include ``z`` when present, which separates the stacked sites of Nuke.
    """
    places = positions[["map_name", "place_norm"]].drop_duplicates().reset_index(drop=True)
    direct = np.where(
        places["place_norm"].str.contains("bombsitea"),
        "A",
        np.where(places["place_norm"].str.contains("bombsiteb"), "B", ""),
    )
    places = places.assign(zone=direct)
    coords = positions.dropna(subset=["x", "y"])
    if coords.empty:
        return places.assign(zone=places["zone"].replace("", "mid"))
    centroids = coords.groupby(["map_name", "place_norm"], as_index=False)[["x", "y", "z"]].mean()
    centroids = centroids.merge(places, on=["map_name", "place_norm"], how="left")
    merged = centroids
    for site in ("A", "B"):
        site_xyz = (
            coords.merge(places[places["zone"] == site], on=["map_name", "place_norm"], how="inner")
            .groupby("map_name", as_index=False)[["x", "y", "z"]]
            .mean()
            .rename(columns={"x": f"x{site}", "y": f"y{site}", "z": f"z{site}"})
        )
        merged = merged.merge(site_xyz, on="map_name", how="left")
    dist = {}
    for site in ("A", "B"):
        dist[site] = np.sqrt(
            (merged["x"] - merged[f"x{site}"]) ** 2
            + (merged["y"] - merged[f"y{site}"]) ** 2
            + (merged["z"] - merged[f"z{site}"]) ** 2
        ).to_numpy(dtype=float)
    with np.errstate(invalid="ignore"):
        near_a = dist["A"] <= ratio * dist["B"]
        near_b = dist["B"] <= ratio * dist["A"]
    learned = np.where(near_a, "A", np.where(near_b, "B", "mid"))
    merged = merged.assign(learned=learned)[["map_name", "place_norm", "learned"]]
    out = places.merge(merged, on=["map_name", "place_norm"], how="left")
    zone = out["zone"].where(out["zone"] != "", out["learned"]).fillna("mid")
    return out.assign(zone=zone)[["map_name", "place_norm", "zone"]]


def _prepare(frame: pd.DataFrame, columns: tuple[str, ...], optional: tuple[str, ...] = ()) -> pd.DataFrame:
    """A copy with the optional columns present (filled with NaN when the caller left them out)."""
    out = frame.copy()
    for col in optional:
        if col not in out.columns:
            out[col] = np.nan
    missing = [c for c in columns if c not in out.columns]
    if missing:
        raise ValueError(f"role frame is missing columns: {missing}")
    return out


def _side(value: object) -> str | None:
    """``"ct"`` / ``"t"`` from strings or ``Side`` members; anything else is unknown."""
    return str(value).lower() if isinstance(value, str) else None


def _flag(value: object, default: bool) -> bool:
    """Tolerant bool for nullable flag columns (None / NaN -> ``default``)."""
    if value is None or value != value:
        return default
    return bool(value)


def role_metrics(frames: RoleFrames, config: RoleConfig | None = None) -> pd.DataFrame:
    """Per-player evidence behind :func:`infer_roles` (index = player id, :data:`METRIC_COLUMNS`).

    Values are NaN where the data does not allow a metric (no weapons on the kills, no position
    samples, ...). The optional frame columns (``z``, ``flashes_thrown``, ``enemies_flashed``,
    ``weapon``, ``seconds_into_round``, ``place``, coordinates) may be missing altogether.
    """
    cfg = config or RoleConfig()
    kills = _prepare(
        frames.kills,
        ("map_played_id", "round_number", "attacker_player_id", "victim_player_id"),
        ("attacker_side", "victim_side", "weapon", "is_opening", "seconds_into_round"),
    )
    positions = _prepare(
        frames.positions,
        ("map_played_id", "round_number", "player_id", "side", "seconds_into_round"),
        ("map_name", "place", "x", "y", "z", "is_alive"),
    )
    player_rounds = _prepare(
        frames.player_rounds,
        ("map_played_id", "round_number", "player_id", "side"),
        ("flashes_thrown", "enemies_flashed"),
    )
    for frame, cols in (
        (kills, ("attacker_side", "victim_side")),
        (positions, ("side",)),
        (player_rounds, ("side",)),
    ):
        for col in cols:
            frame[col] = frame[col].astype("object").map(_side)
    kills["seconds_into_round"] = pd.to_numeric(kills["seconds_into_round"], errors="coerce")
    for col in ("x", "y", "z"):
        positions[col] = pd.to_numeric(positions[col], errors="coerce")
    if positions["z"].isna().all():
        positions["z"] = 0.0  # no height data: distances are measured in the plane

    metrics: dict[str, pd.Series] = {}

    # ---- rounds played per side
    rounds = _rounds_by_side(kills, positions, player_rounds)
    per_side = rounds.groupby(["player_id", "side"]).size().unstack(fill_value=0) if len(rounds) else None
    if per_side is not None:
        metrics["t_rounds"] = per_side.get("t", pd.Series(0, index=per_side.index)).astype(float)
        metrics["ct_rounds"] = per_side.get("ct", pd.Series(0, index=per_side.index)).astype(float)
        metrics["rounds"] = metrics["t_rounds"] + metrics["ct_rounds"]

    # ---- kills: AWP share, T opening duels, late T kills
    sided = kills.dropna(subset=["attacker_side", "victim_side"])
    sided = sided[sided["attacker_side"] != sided["victim_side"]]  # no team kills
    credited = sided.dropna(subset=["attacker_player_id"])
    metrics["kills"] = credited.groupby("attacker_player_id").size().astype(float)
    with_weapon = credited.dropna(subset=["weapon"])
    if len(with_weapon):
        is_awp = with_weapon["weapon"].astype(str).str.lower().isin(AWP_WEAPONS)
        grouped = is_awp.groupby(with_weapon["attacker_player_id"])
        metrics["weapon_kills"] = grouped.size().astype(float)
        metrics["awp_share"] = grouped.mean().astype(float)
    openings = sided[sided["is_opening"].astype("object").map(lambda v: _flag(v, False))]
    if len(openings):
        t_player = openings["attacker_player_id"].where(
            openings["attacker_side"] == "t", openings["victim_player_id"]
        )
        metrics["t_opening_attempts"] = t_player.dropna().groupby(t_player.dropna()).size().astype(float)
    t_timed = credited[(credited["attacker_side"] == "t") & credited["seconds_into_round"].notna()]
    if len(t_timed):
        late = t_timed["seconds_into_round"] >= cfg.late_kill_seconds
        grouped = late.groupby(t_timed["attacker_player_id"])
        metrics["t_timed_kills"] = grouped.size().astype(float)
        metrics["late_kill_share"] = grouped.mean().astype(float)

    # ---- positions: CT site concentration, T-side isolation
    if len(positions):
        seconds = sorted({int(s) for s in positions["seconds_into_round"].dropna().unique()})
        alive = positions["is_alive"].astype("object").map(lambda v: _flag(v, True))
        setup_second = _nearest(seconds, cfg.setup_second)
        placed = positions.dropna(subset=["place"])
        if setup_second is not None and len(placed):
            placed = placed.assign(place_norm=placed["place"].map(_norm_place))
            placed["map_name"] = placed["map_name"].fillna("").astype(str)
            zones = _zone_table(placed, cfg.site_distance_ratio)
            at_setup = (placed["side"] == "ct") & (placed["seconds_into_round"] == setup_second)
            ct = placed[at_setup & alive[placed.index]].merge(
                zones, on=["map_name", "place_norm"], how="left"
            )
            if len(ct):
                counts = ct.groupby(["player_id", "map_name", "zone"]).size().unstack(fill_value=0)
                for site in ("A", "B"):
                    if site not in counts.columns:
                        counts[site] = 0
                dominant = counts[["A", "B"]].max(axis=1).groupby("player_id").sum()
                total = counts.sum(axis=1).groupby("player_id").sum()
                metrics["ct_setup_samples"] = total.astype(float)
                metrics["ct_site_share"] = (dominant / total).astype(float)
        lurk_second = _nearest(seconds, cfg.lurk_second)
        if lurk_second is not None:
            t_side = positions[
                (positions["side"] == "t") & (positions["seconds_into_round"] == lurk_second) & alive
            ].dropna(subset=["x", "y"])
            if len(t_side):
                group = t_side.groupby(_ROUND_KEYS)
                n = group["x"].transform("size")
                t_side = t_side.assign(
                    n=n, sum_x=group["x"].transform("sum"), sum_y=group["y"].transform("sum")
                )
                t_side = t_side[t_side["n"] >= 3]
                others_x = (t_side["sum_x"] - t_side["x"]) / (t_side["n"] - 1)
                others_y = (t_side["sum_y"] - t_side["y"]) / (t_side["n"] - 1)
                t_side = t_side.assign(d=np.hypot(t_side["x"] - others_x, t_side["y"] - others_y))
                mean_d = t_side.groupby(_ROUND_KEYS)["d"].transform("mean")
                t_side = t_side[mean_d > 0].assign(ratio=t_side["d"] / mean_d)
                grouped_iso = t_side.groupby("player_id")["ratio"]
                metrics["t_isolation"] = grouped_iso.mean().astype(float)
                metrics["t_isolation_rounds"] = grouped_iso.size().astype(float)

    # ---- utility: enemies flashed per round
    flashed = pd.to_numeric(player_rounds["enemies_flashed"], errors="coerce")
    if flashed.notna().any():
        metrics["flashed_per_round"] = flashed.groupby(player_rounds["player_id"]).mean().astype(float)

    out = pd.DataFrame(metrics)
    for col in METRIC_COLUMNS:
        if col not in out.columns:
            out[col] = np.nan
    out = out[list(METRIC_COLUMNS)]
    for col in ("rounds", "t_rounds", "ct_rounds", "kills", "t_opening_attempts"):
        out[col] = out[col].fillna(0.0)
    t_rounds = out["t_rounds"].where(out["t_rounds"] > 0)
    out["t_opening_rate"] = out["t_opening_attempts"] / t_rounds
    out.index = out.index.astype("int64")
    out.index.name = "player_id"
    return out.sort_index()


def _num(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return math.nan
    return number


def decide_role(
    metrics: Mapping[str, object], config: RoleConfig | None = None, *, flash_median: float = math.nan
) -> tuple[PlayerRole, float] | None:
    """Role and confidence in (0, 1] for one row of :func:`role_metrics` (``None`` = too few rounds).

    Confidence = evidence score (0.5 at a rule's threshold, 1.0 at its ``_full`` value; AWPER starts
    at 0.6) times a sample-size factor ``min(1, sqrt(rounds / full_confidence_rounds))``.
    ``flash_median`` is the median ``flashed_per_round`` of all players (SUPPORT needs at least that
    much flashing when flash data exists).
    """
    cfg = config or RoleConfig()
    rounds = _num(metrics.get("rounds"))
    if not rounds >= cfg.min_rounds:
        return None
    sample = min(1.0, math.sqrt(rounds / cfg.full_confidence_rounds))
    t_rounds = _num(metrics.get("t_rounds"))
    awp_share = _num(metrics.get("awp_share"))
    if _num(metrics.get("weapon_kills")) >= cfg.min_kills and awp_share >= cfg.awp_share_min:
        score = 0.6 + 0.4 * _ramp(awp_share, cfg.awp_share_min, cfg.awp_share_full)
        return PlayerRole.AWPER, sample * score

    candidates: list[tuple[float, PlayerRole]] = []
    entry_rate = _num(metrics.get("t_opening_rate"))
    if t_rounds >= cfg.min_side_rounds and entry_rate >= cfg.entry_rate_min:
        score = 0.5 + 0.5 * _ramp(entry_rate, cfg.entry_rate_min, cfg.entry_rate_full)
        candidates.append((score, PlayerRole.ENTRY))

    isolation = _num(metrics.get("t_isolation"))
    late_share = _num(metrics.get("late_kill_share"))
    late_known = _num(metrics.get("t_timed_kills")) >= cfg.min_kills
    if _num(metrics.get("t_isolation_rounds")) >= cfg.min_side_rounds:
        if isolation >= cfg.isolation_min:
            score = 0.5 + 0.4 * _ramp(isolation, cfg.isolation_min, cfg.isolation_full)
            if late_known:
                score += 0.1 * _ramp(late_share, cfg.late_share_min, cfg.late_share_full)
            candidates.append((score, PlayerRole.LURKER))
    elif late_known and late_share >= cfg.late_share_min:
        # no position samples: late kills alone are weaker evidence, capped at 0.75
        score = 0.5 + 0.25 * _ramp(late_share, cfg.late_share_min, cfg.late_share_full)
        candidates.append((score, PlayerRole.LURKER))

    site_share = _num(metrics.get("ct_site_share"))
    if _num(metrics.get("ct_setup_samples")) >= cfg.min_side_rounds:
        if site_share >= cfg.anchor_share_min:
            score = 0.5 + 0.5 * _ramp(site_share, cfg.anchor_share_min, cfg.anchor_share_full)
            candidates.append((score, PlayerRole.ANCHOR))
        elif site_share <= cfg.rotator_share_max:
            score = 0.5 + 0.5 * _ramp(site_share, cfg.rotator_share_max, cfg.rotator_share_full)
            candidates.append((score, PlayerRole.ROTATOR))

    if candidates:
        score, role = max(candidates, key=lambda c: c[0])  # ties: first listed wins
        return role, sample * min(1.0, score)

    flashed = _num(metrics.get("flashed_per_round"))
    flashes_enough = not (math.isfinite(flashed) and math.isfinite(flash_median)) or flashed >= flash_median
    if t_rounds >= cfg.min_side_rounds and entry_rate <= cfg.support_rate_max and flashes_enough:
        score = cfg.fallback_confidence + 0.2 * _ramp(entry_rate, cfg.support_rate_max, cfg.support_rate_full)
        return PlayerRole.SUPPORT, sample * score
    return PlayerRole.RIFLER, sample * cfg.fallback_confidence


def _frames_from_session(
    session: Session, window_days: int, as_of: datetime | None, cfg: RoleConfig
) -> RoleFrames:
    """Load the window, restricted to the two position sample offsets the rules read."""
    seconds = available_position_seconds(session)
    wanted = {_nearest(seconds, cfg.setup_second), _nearest(seconds, cfg.lurk_second)} - {None}
    return load_role_frames(
        session,
        window_days=window_days,
        as_of=as_of,
        position_seconds=sorted(s for s in wanted if s is not None) or None,
    )


def infer_roles(
    source: Session | RoleFrames,
    *,
    window_days: int = 120,
    as_of: datetime | None = None,
    config: RoleConfig | None = None,
) -> dict[int, tuple[PlayerRole, float]]:
    """player id -> (role, confidence) from demo data of the ``window_days`` before ``as_of``.

    ``source`` is a SQLAlchemy ``Session`` (the frames are loaded with
    :func:`cs2bot.datasets.load_role_frames`; ``as_of`` defaults to now) or ready-made frames
    (:class:`cs2bot.datasets.RoleFrames`; ``window_days`` / ``as_of`` are then ignored - the caller
    already chose the window). Players with fewer than ``config.min_rounds`` rounds are left out.
    Never returns ``IGL`` or ``UNKNOWN``.
    """
    cfg = config or RoleConfig()
    frames = _frames_from_session(source, window_days, as_of, cfg) if isinstance(source, Session) else source
    metrics = role_metrics(frames, cfg)
    if metrics.empty:
        return {}
    flash = metrics.loc[metrics["rounds"] >= cfg.min_rounds, "flashed_per_round"].dropna()
    flash_median = float(flash.median()) if len(flash) else math.nan
    out: dict[int, tuple[PlayerRole, float]] = {}
    player_ids = [int(i) for i in metrics.index.tolist()]
    for player_id, row in zip(player_ids, metrics.to_dict("records"), strict=True):
        decided = decide_role(row, cfg, flash_median=flash_median)
        if decided is not None:
            out[player_id] = (decided[0], round(float(decided[1]), 4))
    return out


def update_player_roles(
    session: Session,
    *,
    window_days: int = 120,
    as_of: datetime | None = None,
    config: RoleConfig | None = None,
    roles: dict[int, tuple[PlayerRole, float]] | None = None,
    unknown_stored_confidence: float = 0.5,
    confidence_half_life_days: float | None = 180.0,
) -> int:
    """Write inferred roles to ``players.primary_role`` / ``role_confidence``; returns rows changed.

    A player is only updated when the new confidence beats the stored one:

    * a stored ``IGL`` is never replaced (it cannot be inferred from demos, so it was set by hand
      or by another source);
    * no stored role (NULL / UNKNOWN) -> always written;
    * a stored role without a confidence came from elsewhere: it counts as
      ``unknown_stored_confidence``;
    * a stored confidence written by this function ages: it is discounted by
      ``0.5 ** (age / confidence_half_life_days)`` (age from ``players.extra["role_inferred_at"]``),
      so a player who changes his role is not locked into an old, once-confident label. Pass
      ``None`` to disable the ageing.

    ``roles`` lets the caller pass the result of :func:`infer_roles`; otherwise it is computed from
    the session. The session is flushed, not committed. Re-running with the same data changes
    nothing and returns 0 (idempotent, also with ``as_of=None``); a label re-confirmed with the same
    confidence only gets its ``role_inferred_at`` refreshed, at most once a day, and is not counted.
    """
    now = ensure_utc(as_of) if as_of is not None else utcnow()
    if roles is None:
        roles = infer_roles(session, window_days=window_days, as_of=now, config=config)
    ids = sorted(roles)
    changed = 0
    for i in range(0, len(ids), _IN_CHUNK):
        chunk = ids[i : i + _IN_CHUNK]
        for player in session.scalars(select(Player).where(Player.id.in_(chunk))):
            role, confidence = roles[player.id]
            if role in (PlayerRole.IGL, PlayerRole.UNKNOWN) or player.primary_role is PlayerRole.IGL:
                continue
            extra = player.extra if isinstance(player.extra, dict) else {}
            has_role = player.primary_role is not None and player.primary_role is not PlayerRole.UNKNOWN
            inferred_at = _parse_iso(extra.get(ROLE_INFERRED_AT_KEY))
            if (
                has_role
                and role is player.primary_role
                and player.role_confidence is not None
                and abs(confidence - float(player.role_confidence)) <= 1e-9
            ):
                # The same label re-confirmed by the same evidence: nothing changes (idempotent, also
                # when ``as_of`` is "now" and the stored confidence has aged by a few seconds). The
                # timestamp is refreshed at most once a day so a re-confirmed label does not age.
                if inferred_at is None or now - inferred_at >= _REFRESH_AFTER:
                    player.extra = {**extra, ROLE_INFERRED_AT_KEY: now.isoformat()}
                continue
            if not has_role:
                stored = -1.0
            elif player.role_confidence is None:
                stored = unknown_stored_confidence
            else:
                stored = float(player.role_confidence)
                if confidence_half_life_days and inferred_at is not None and now > inferred_at:
                    age_days = (now - inferred_at).total_seconds() / 86_400.0
                    stored *= 0.5 ** (age_days / confidence_half_life_days)
            if confidence <= stored:
                continue
            player.primary_role = role
            player.role_confidence = float(confidence)
            # a new dict: SQLAlchemy does not track in-place changes of JSON columns
            player.extra = {**extra, ROLE_INFERRED_AT_KEY: now.isoformat()}
            changed += 1
    session.flush()
    return changed


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value))
    except ValueError:
        return None
