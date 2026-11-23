"""VetoModel: templates, preference learning, exact sequence enumeration and conditioning."""

from __future__ import annotations

import itertools
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from cs2bot.domain.enums import MatchFormat, TeamSlot, VetoAction
from cs2bot.domain.predictions import MapSequenceProbability
from cs2bot.domain.records import VetoRecord, VetoStep
from cs2bot.models.ratings import MapRatingModel
from cs2bot.models.veto import (
    BO1_TEMPLATE_2_3_1,
    VETO_TEMPLATES,
    VetoCoefficients,
    VetoConfig,
    VetoModel,
    VetoTemplateStep,
    map_slot_distribution,
    p_map_played,
    veto_template,
)
from cs2bot.testing.synthetic import SynthUniverse

from .rv_helpers import POOL7, T0, universe, veto_record, veto_steps

BAN, PICK, LEFTOVER = VetoAction.BAN, VetoAction.PICK, VetoAction.LEFTOVER
T1, T2 = TeamSlot.TEAM1, TeamSlot.TEAM2
SeqKey = tuple[tuple[str, ...], tuple[TeamSlot | None, ...]]


def _as_dict(seqs: list[MapSequenceProbability]) -> dict[SeqKey, float]:
    out: dict[SeqKey, float] = {}
    for s in seqs:
        key = (tuple(s.maps), tuple(s.pickers))
        assert key not in out, "sequences must be aggregated"
        out[key] = s.probability
    return out


def _split(uni: SynthUniverse, frac: float = 0.7) -> tuple[list[VetoRecord], list[VetoRecord], datetime]:
    recs = sorted(uni.veto_records(), key=lambda r: r.played_at)
    cut = recs[int(len(recs) * frac)].played_at
    return [r for r in recs if r.played_at < cut], [r for r in recs if r.played_at >= cut], cut


@pytest.fixture(scope="module")
def fitted() -> tuple[VetoModel, SynthUniverse, list[VetoRecord], datetime]:
    """Veto model fitted on the first 70% of a synthetic universe (no advantage term)."""
    uni = universe(seed=3)
    train, test, cut = _split(uni)
    return VetoModel().fit(train), uni, test, cut


def _played_sequence(rec: VetoRecord) -> tuple[list[str], list[TeamSlot | None]]:
    """Maps a veto leads to, in playing order (the synthetic BO1/BO3/BO5 templates)."""
    maps: list[str] = []
    pickers: list[TeamSlot | None] = []
    for st in rec.steps:
        if st.action is PICK:
            maps.append(st.map_name)
            pickers.append(st.team)
        elif st.action is LEFTOVER:
            maps.append(st.map_name)
            pickers.append(None)
    return maps, pickers


# -------------------------------------------------------------------------------- templates


def test_templates_for_the_seven_map_pool() -> None:
    def spec(fmt: MatchFormat) -> list[tuple[VetoAction, int | None]]:
        return [(s.action, s.actor) for s in VETO_TEMPLATES[fmt]]

    assert spec(MatchFormat.BO1) == [
        (BAN, 0),
        (BAN, 1),
        (BAN, 0),
        (BAN, 1),
        (BAN, 0),
        (BAN, 1),
        (LEFTOVER, None),
    ]
    assert spec(MatchFormat.BO3) == [
        (BAN, 0),
        (BAN, 1),
        (PICK, 0),
        (PICK, 1),
        (BAN, 0),
        (BAN, 1),
        (LEFTOVER, None),
    ]
    assert spec(MatchFormat.BO5) == [
        (BAN, 0),
        (BAN, 1),
        (PICK, 0),
        (PICK, 1),
        (PICK, 0),
        (PICK, 1),
        (LEFTOVER, None),
    ]
    assert spec(MatchFormat.BO2) == [(BAN, 0), (BAN, 1), (PICK, 0), (PICK, 1)]
    assert set(VETO_TEMPLATES) == set(MatchFormat)
    for fmt in MatchFormat:
        assert VETO_TEMPLATES[fmt] == veto_template(fmt, 7)
        assert all(isinstance(s, VetoTemplateStep) for s in VETO_TEMPLATES[fmt])


@pytest.mark.parametrize("n_maps", [3, 4, 5, 6, 7, 8, 9, 10])
@pytest.mark.parametrize("fmt", list(MatchFormat))
def test_templates_generalise_to_other_pool_sizes(fmt: MatchFormat, n_maps: int) -> None:
    if n_maps < fmt.n_maps:
        with pytest.raises(ValueError):
            veto_template(fmt, n_maps)
        return
    tpl = veto_template(fmt, n_maps)
    picks = sum(1 for s in tpl if s.action is PICK)
    bans = sum(1 for s in tpl if s.action is BAN)
    leftovers = sum(1 for s in tpl if s.action is LEFTOVER)
    played = picks + leftovers
    assert played == fmt.n_maps
    if fmt is MatchFormat.BO2:
        assert leftovers == 0 and picks == 2
    else:
        assert leftovers == 1 and tpl[-1] == VetoTemplateStep(LEFTOVER, None)
        assert bans + picks + 1 == n_maps  # every map is banned, picked or left over
    actors = [s.actor for s in tpl if s.action is not LEFTOVER]
    assert actors == [i % 2 for i in range(len(actors))]  # teams alternate, first-veto team starts


# ----------------------------------------------------------------- sequences without history


@pytest.mark.parametrize(
    ("fmt", "n_maps", "pickers"),
    [
        (MatchFormat.BO1, 1, [None]),
        (MatchFormat.BO2, 2, [T1, T2]),
        (MatchFormat.BO3, 3, [T1, T2, None]),
        (MatchFormat.BO5, 5, [T1, T2, T1, T2, None]),
    ],
)
def test_sequence_shape_per_format(fmt: MatchFormat, n_maps: int, pickers: list[TeamSlot | None]) -> None:
    model = VetoModel()
    seqs = model.predict_sequences(1, 2, fmt, POOL7, top_k=100_000)
    assert sum(s.probability for s in seqs) == pytest.approx(1.0)
    assert [s.probability for s in seqs] == sorted((s.probability for s in seqs), reverse=True)
    for s in seqs:
        assert len(s.maps) == n_maps == len(s.pickers)
        assert len(set(s.maps)) == n_maps
        assert set(s.maps) <= set(POOL7)
        assert s.pickers == pickers
    # an untrained model is uniform over all ordered outcomes
    n_outcomes = {
        MatchFormat.BO1: 7,
        MatchFormat.BO2: 7 * 6,
        MatchFormat.BO3: 7 * 6 * 5,
        MatchFormat.BO5: 2520,
    }
    assert len(seqs) == n_outcomes[fmt]
    assert all(s.probability == pytest.approx(1.0 / n_outcomes[fmt]) for s in seqs)
    # the second team starting swaps the pickers
    swapped = model.predict_sequences(1, 2, fmt, POOL7, first_veto=T2, top_k=5)
    assert swapped[0].pickers == [None if p is None else p.other for p in pickers]


def test_top_k_truncation_renormalises(
    fitted: tuple[VetoModel, SynthUniverse, list[VetoRecord], datetime],
) -> None:
    model, uni, _, _ = fitted
    full = model.predict_sequences(1, 2, MatchFormat.BO3, uni.map_pool, top_k=100_000)
    assert _as_dict(model.predict_sequences(1, 2, MatchFormat.BO3, uni.map_pool, top_k=None)) == _as_dict(
        full
    )
    default = model.predict_sequences(1, 2, MatchFormat.BO3, uni.map_pool)
    assert len(full) == 210 and 40 <= len(default) <= 50
    assert sum(s.probability for s in default) == pytest.approx(1.0)
    assert [s.probability for s in default] == sorted((s.probability for s in default), reverse=True)
    # plain truncation: a short list (below the 19 sequences needed to keep a BO3's slot marginals)
    # or preserve_marginals=False keeps the most likely sequences and rescales them
    for top in (
        model.predict_sequences(1, 2, MatchFormat.BO3, uni.map_pool, top_k=10),
        model.predict_sequences(1, 2, MatchFormat.BO3, uni.map_pool, top_k=30, preserve_marginals=False),
    ):
        k = len(top)
        assert k in (10, 30)
        assert sum(s.probability for s in top) == pytest.approx(1.0)
        kept = sum(s.probability for s in full[:k])
        for a, b in zip(top, full[:k], strict=True):
            assert a.maps == b.maps and a.pickers == b.pickers
            assert a.probability == pytest.approx(b.probability / kept)
    with pytest.raises(ValueError):
        model.predict_sequences(1, 2, MatchFormat.BO3, uni.map_pool, top_k=0)


@pytest.mark.parametrize("n_maps", [5, 6, 8, 9])
@pytest.mark.parametrize("fmt", list(MatchFormat))
def test_other_pool_sizes_at_prediction_time(fmt: MatchFormat, n_maps: int) -> None:
    pool = [*POOL7, "overpass", "vertigo"][:n_maps]
    seqs = VetoModel().predict_sequences(10, 20, fmt, pool, top_k=100_000)
    assert sum(s.probability for s in seqs) == pytest.approx(1.0)
    assert all(len(s.maps) == fmt.n_maps and len(set(s.maps)) == fmt.n_maps for s in seqs)
    slots = map_slot_distribution(seqs)
    assert sorted(slots) == list(range(1, fmt.n_maps + 1))
    for dist in slots.values():
        assert sum(dist.values()) == pytest.approx(1.0)
        assert all(p == pytest.approx(1.0 / n_maps) for p in dist.values())  # symmetric when untrained


def test_invalid_requests_raise() -> None:
    model = VetoModel()
    with pytest.raises(ValueError, match="must differ"):
        model.predict_sequences(1, 1, MatchFormat.BO3, POOL7)
    with pytest.raises(ValueError, match="at least 3 maps"):
        model.predict_sequences(1, 2, MatchFormat.BO3, ["mirage", "nuke"])
    dup_step = [VetoStep(step=1, action=BAN, team=T1, map_name="nuke")] * 2
    with pytest.raises(ValueError, match="given twice"):
        model.predict_sequences(1, 2, MatchFormat.BO3, POOL7, known_steps=dup_step)
    dup_map = veto_steps([("ban", 1, "nuke"), ("ban", 2, "de_nuke")])
    with pytest.raises(ValueError, match="appears twice"):
        model.predict_sequences(1, 2, MatchFormat.BO3, POOL7, known_steps=dup_map)
    with pytest.raises(ValueError):
        model.preference(1, "nuke", LEFTOVER)


# ------------------------------------------------------------------- learning from history


def test_step_predictions_are_well_above_chance(
    fitted: tuple[VetoModel, SynthUniverse, list[VetoRecord], datetime],
) -> None:
    model, uni, test, _ = fitted
    hits = {BAN: 0, PICK: 0}
    counts = {BAN: 0, PICK: 0}
    chance = {BAN: 0.0, PICK: 0.0}
    nll = 0.0
    nll_uniform = 0.0
    for rec in test:
        known: list[VetoStep] = []
        for st in rec.steps:
            if st.action is LEFTOVER:
                break
            forecast = model.next_step(
                rec.team1_id, rec.team2_id, rec.format, uni.map_pool, known_steps=known
            )
            assert forecast.step == st.step
            assert forecast.action is st.action
            assert forecast.team is st.team
            assert sum(forecast.probabilities.values()) == pytest.approx(1.0)
            assert set(forecast.probabilities) == set(uni.map_pool) - {k.map_name for k in known}
            top = max(forecast.probabilities, key=lambda m: forecast.probabilities[m])
            hits[st.action] += top == st.map_name
            counts[st.action] += 1
            chance[st.action] += 1.0 / len(forecast.probabilities)
            nll -= math.log(forecast.probabilities[st.map_name])
            nll_uniform += math.log(len(forecast.probabilities))
            known.append(st)
    assert counts[PICK] > 250 and counts[BAN] > 500
    pick_acc = hits[PICK] / counts[PICK]
    ban_acc = hits[BAN] / counts[BAN]
    assert pick_acc > 1.6 * chance[PICK] / counts[PICK]
    assert pick_acc > 0.38
    assert ban_acc > 1.6 * chance[BAN] / counts[BAN]
    n = counts[PICK] + counts[BAN]
    assert nll / n < nll_uniform / n - 0.2


def test_played_sequences_get_far_more_than_uniform_probability(
    fitted: tuple[VetoModel, SynthUniverse, list[VetoRecord], datetime],
) -> None:
    model, uni, test, _ = fitted
    lift = 0.0
    top1 = 0
    for rec in test:
        seqs = model.predict_sequences(rec.team1_id, rec.team2_id, rec.format, uni.map_pool, top_k=100_000)
        assert sum(s.probability for s in seqs) == pytest.approx(1.0)
        maps, pickers = _played_sequence(rec)
        p = _as_dict(seqs).get((tuple(maps), tuple(pickers)), 0.0)
        lift += p * len(seqs)  # 1.0 for a uniform model
        top1 += seqs[0].maps == maps
    assert lift / len(test) > 1.8
    assert top1 / len(test) > 3.0 / 210


def test_fitted_coefficients_improve_the_likelihood(
    fitted: tuple[VetoModel, SynthUniverse, list[VetoRecord], datetime],
) -> None:
    model, _, _, _ = fitted
    coef = model.coefficients
    assert coef.n_steps > 2000
    assert coef.a_ban > 1.0 and coef.a_pick > 1.0  # habits are informative: sharper than the raw ratio
    assert coef.b_ban == 0.0 and coef.b_pick == 0.0  # no advantage callable was given
    assert coef.c_ban > 0.0  # ban the opponent's comfort picks
    assert coef.c_pick < 0.0  # avoid picking into them
    assert coef.log_likelihood is not None
    assert coef.log_likelihood / coef.n_steps > -math.log(4.5)
    assert model.coefficients_with_advantage is None
    assert model.n_records > 400
    assert len(model.team_ids) == 16


def test_advantage_term_is_learned_and_used() -> None:
    uni = universe(seed=3)
    train, test, cut = _split(uni)
    ratings = MapRatingModel().fit_until(uni.map_results(), cut, record_history=True)
    model = VetoModel().fit(train, map_advantage=ratings.historical_map_advantage)
    plain, with_adv = model.coefficients, model.coefficients_with_advantage
    assert with_adv is not None
    assert model.advantage_failures == 0
    # teams ban maps where they are weaker than this opponent and pick where they are stronger
    assert with_adv.b_ban > 0.1 and with_adv.b_pick > 0.1
    assert with_adv.log_likelihood is not None and plain.log_likelihood is not None
    assert with_adv.log_likelihood > plain.log_likelihood + 5.0

    def advantage(team: int, opp: int, map_name: str, as_of: datetime) -> float:
        return ratings.map_advantage(team, opp, map_name, cut)

    nll = {False: 0.0, True: 0.0}
    for rec in test:
        maps, pickers = _played_sequence(rec)
        for use_adv in (False, True):
            seqs = model.predict_sequences(
                rec.team1_id,
                rec.team2_id,
                rec.format,
                uni.map_pool,
                top_k=100_000,
                map_advantage=advantage if use_adv else None,
            )
            assert sum(s.probability for s in seqs) == pytest.approx(1.0)
            nll[use_adv] -= math.log(max(_as_dict(seqs).get((tuple(maps), tuple(pickers)), 0.0), 1e-9))
    assert nll[True] < nll[False]  # out of sample the rating-based term helps
    # without the callable the plain coefficients are used: same as a model fitted without it
    reference = VetoModel().fit(train)
    a = model.predict_sequences(1, 2, MatchFormat.BO3, uni.map_pool, top_k=5)
    b = reference.predict_sequences(1, 2, MatchFormat.BO3, uni.map_pool, top_k=5)
    assert _as_dict(a) == pytest.approx(_as_dict(b))


def test_advantage_callable_shifts_bans_and_picks() -> None:
    model = VetoModel()
    model._coef_adv = VetoCoefficients(b_ban=1.0, b_pick=1.0)  # hand-set weights, no history needed

    def advantage(team: int, opp: int, map_name: str, as_of: datetime) -> float:
        sign = 1.0 if team == 1 else -1.0
        return sign * {"nuke": -2.0, "mirage": 2.0}.get(map_name, 0.0)

    ban = model.next_step(1, 2, MatchFormat.BO3, POOL7, map_advantage=advantage)
    assert ban.action is BAN and ban.team is T1
    assert max(ban.probabilities, key=lambda m: ban.probabilities[m]) == "nuke"  # team 1 is weakest there
    assert min(ban.probabilities, key=lambda m: ban.probabilities[m]) == "mirage"
    assert ban.probabilities["nuke"] / ban.probabilities["dust2"] == pytest.approx(math.exp(2.0))
    known = veto_steps([("ban", 1, "train"), ("ban", 2, "ancient")])
    pick = model.next_step(1, 2, MatchFormat.BO3, POOL7, known_steps=known, map_advantage=advantage)
    assert pick.action is PICK and pick.step == 3
    assert max(pick.probabilities, key=lambda m: pick.probabilities[m]) == "mirage"
    # non-finite advantages are treated as "no information"
    flat = model.next_step(1, 2, MatchFormat.BO3, POOL7, map_advantage=lambda *_: float("nan"))
    assert all(p == pytest.approx(1 / 7) for p in flat.probabilities.values())
    # a model fitted without an advantage term ignores the callable instead of guessing a weight
    plain = VetoModel()
    ignored = plain.next_step(1, 2, MatchFormat.BO3, POOL7, map_advantage=advantage)
    assert all(p == pytest.approx(1 / 7) for p in ignored.probabilities.values())


def test_failing_advantage_lookups_do_not_abort_the_fit() -> None:
    uni = universe(seed=3)
    train, _, _ = _split(uni, 0.2)
    calls = {"n": 0}

    def flaky(team: int, opp: int, map_name: str, as_of: datetime) -> float:
        calls["n"] += 1
        if calls["n"] % 50 == 0:
            raise RuntimeError("rating lookup failed")
        return 0.1

    model = VetoModel().fit(train, map_advantage=flaky)
    assert model.n_records == len(train)
    assert model.advantage_failures > 0
    assert model.coefficients_with_advantage is not None


def _habit_history(start_id: int, banned: str, *, days: list[float], team: int = 1) -> list[VetoRecord]:
    """BO3 vetoes in which ``team`` (as team1, first veto) always bans ``banned`` first."""
    out: list[VetoRecord] = []
    for i, day in enumerate(days):
        rest = [m for m in POOL7 if m != banned]
        rot = rest[i % 6 :] + rest[: i % 6]  # the other choices rotate so nothing else is a habit
        spec: list[tuple[str, int | None, str]] = [
            ("ban", 1, banned),
            ("ban", 2, rot[0]),
            ("pick", 1, rot[1]),
            ("pick", 2, rot[2]),
            ("ban", 1, rot[3]),
            ("ban", 2, rot[4]),
            ("leftover", None, rot[5]),
        ]
        out.append(veto_record(start_id + i, team, 50 + i, spec, day=day))
    return out


def test_preferences_are_recency_weighted() -> None:
    old = _habit_history(1, "nuke", days=[float(d) for d in range(0, 20, 2)])  # 10 vetoes, long ago
    new = _habit_history(100, "mirage", days=[300.0 + d for d in range(4)])  # 4 recent vetoes
    as_of = T0 + timedelta(days=305)
    short = VetoModel(VetoConfig(half_life_days=60.0)).fit(old + new)
    long = VetoModel(VetoConfig(half_life_days=5000.0)).fit(old + new)
    s = short.next_step(1, 2, MatchFormat.BO3, POOL7, as_of=as_of).probabilities
    lg = long.next_step(1, 2, MatchFormat.BO3, POOL7, as_of=as_of).probabilities
    assert s["mirage"] > s["nuke"]  # recent habit dominates with a short half-life
    assert lg["nuke"] > lg["mirage"]  # ten old vetoes outweigh four new ones without decay
    assert short.preference(1, "mirage", BAN, as_of) > short.preference(1, "nuke", BAN, as_of)
    # preferences fade toward the league rates as the history ages
    soon = short.preference(1, "mirage", BAN, as_of)
    later = short.preference(1, "mirage", BAN, as_of + timedelta(days=720))
    league = short.preference(999, "mirage", BAN, as_of + timedelta(days=720))
    assert soon > later > league - 1e-9
    assert later - league < 0.1 * (soon - league)
    assert VetoConfig().half_life_days == 90.0


def test_bayesian_smoothing_toward_league_rates() -> None:
    history = _habit_history(1, "nuke", days=[float(d) for d in range(30)])  # team 1: 30 vetoes
    rookie = veto_record(
        900,
        7,
        8,
        [
            ("ban", 1, "mirage"),
            ("ban", 2, "anubis"),
            ("pick", 1, "dust2"),
            ("pick", 2, "inferno"),
            ("ban", 1, "train"),
            ("ban", 2, "ancient"),
            ("leftover", None, "nuke"),
        ],
        day=31,
    )
    model = VetoModel().fit([*history, rookie])
    unknown = model.preferences(12345, BAN, POOL7)
    veteran = model.preferences(1, BAN, POOL7)
    one_veto = model.preferences(7, BAN, POOL7)
    # league-wide: nuke is the most banned map, an unknown team inherits exactly that
    assert max(unknown, key=lambda m: unknown[m]) == "nuke"
    assert unknown["nuke"] > 0.0
    # a long history moves a team far from the league; a single veto only a little
    assert veteran["nuke"] > unknown["nuke"] + 0.5
    assert one_veto["mirage"] > unknown["mirage"]
    assert abs(one_veto["nuke"] - unknown["nuke"]) < 0.5
    assert one_veto["mirage"] - unknown["mirage"] < veteran["nuke"] - unknown["nuke"]
    # stronger team prior -> closer to the league
    stiff = VetoModel(VetoConfig(team_prior_strength=50.0)).fit([*history, rookie])
    assert stiff.preference(1, "nuke", BAN) - stiff.preference(12345, "nuke", BAN) < (
        veteran["nuke"] - unknown["nuke"]
    )
    # two unknown teams: the forecast is the league distribution, identical for either slot
    first = model.next_step(801, 802, MatchFormat.BO3, POOL7).probabilities
    assert max(first, key=lambda m: first[m]) == "nuke"
    assert first == pytest.approx(model.next_step(803, 804, MatchFormat.BO3, POOL7).probabilities)


def test_malformed_veto_records_do_not_abort_the_fit() -> None:
    good = _habit_history(1, "nuke", days=[0.0, 1.0, 2.0])
    weird_steps = (
        VetoStep(step=1, action=BAN, team=T1, map_name="de_nuke"),
        VetoStep(step=2, action=BAN, team=T2, map_name="Nuke"),  # duplicate map: ignored
        VetoStep(step=3, action=PICK, team=None, map_name="mirage"),  # unknown actor: not a decision
        VetoStep(step=4, action=PICK, team=T2, map_name="Tuscan"),  # not in the default pool
        VetoStep(step=9, action=BAN, team=T1, map_name="Dust II"),  # gap in the numbering
    )
    weird = VetoRecord(
        match_id=77,
        played_at=T0 + timedelta(days=3),
        format=MatchFormat.BO3,
        team1_id=1,
        team2_id=60,
        steps=weird_steps,
        map_pool=None,
    )
    empty = VetoRecord(
        match_id=78,
        played_at=T0 + timedelta(days=4),
        format=MatchFormat.BO1,
        team1_id=1,
        team2_id=61,
        steps=(),
    )
    same_team = veto_record(79, 5, 5, [("ban", 1, "nuke")], day=5)
    model = VetoModel().fit([*good, weird, empty, same_team, good[0]])
    assert model.n_records == 5  # 3 good + weird + empty; duplicate and same-team records skipped
    assert model.preference(1, "nuke", BAN) > 0.5
    seqs = model.predict_sequences(1, 60, MatchFormat.BO3, POOL7)
    assert sum(s.probability for s in seqs) == pytest.approx(1.0)


# -------------------------------------------------------------------- exactness of the DP


def _brute_force(
    model: VetoModel, team1: int, team2: int, fmt: MatchFormat, pool: list[str], first: TeamSlot
) -> dict[SeqKey, float]:
    """Independent full enumeration of the veto tree from public preferences and coefficients."""
    coef = model.coefficients
    ids = {T1: (team1, team2), T2: (team2, team1)}
    out: dict[SeqKey, float] = {}

    def choice_probs(slot: TeamSlot, action: VetoAction, remaining: tuple[str, ...]) -> dict[str, float]:
        team, opp = ids[slot]
        a, _, c = coef.for_action(action)
        u = {m: a * model.preference(team, m, action) + c * model.preference(opp, m, PICK) for m in remaining}
        z = sum(math.exp(v) for v in u.values())
        return {m: math.exp(v) / z for m, v in u.items()}

    def walk(
        i: int, remaining: tuple[str, ...], played: tuple[tuple[str, TeamSlot | None], ...], p: float
    ) -> None:
        template = VETO_TEMPLATES[fmt]
        if i == len(template):
            key = (tuple(m for m, _ in played), tuple(t for _, t in played))
            out[key] = out.get(key, 0.0) + p
            return
        step = template[i]
        if step.action is LEFTOVER:
            assert len(remaining) == 1
            walk(i + 1, (), (*played, (remaining[0], None)), p)
            return
        assert step.actor is not None
        slot = first if step.actor == 0 else first.other
        for m, q in choice_probs(slot, step.action, remaining).items():
            rest = tuple(x for x in remaining if x != m)
            walk(i + 1, rest, (*played, (m, slot)) if step.action is PICK else played, p * q)

    walk(0, tuple(pool), (), 1.0)
    return out


@pytest.mark.parametrize("fmt", list(MatchFormat))
@pytest.mark.parametrize("first", [T1, T2])
def test_enumeration_matches_brute_force(
    fitted: tuple[VetoModel, SynthUniverse, list[VetoRecord], datetime], fmt: MatchFormat, first: TeamSlot
) -> None:
    model, uni, _, _ = fitted
    no_pruning = VetoConfig(prune_probability=0.0).model_dump(mode="json")
    exact = VetoModel.from_dict({**model.to_dict(), "config": no_pruning})
    want = _brute_force(exact, 3, 11, fmt, uni.map_pool, first)
    got = _as_dict(exact.predict_sequences(3, 11, fmt, uni.map_pool, first_veto=first, top_k=1_000_000))
    assert sum(want.values()) == pytest.approx(1.0)
    assert set(got) == set(want)
    for key, p in want.items():
        assert got[key] == pytest.approx(p, rel=1e-9, abs=1e-15)
    # default pruning changes nothing visible
    pruned = _as_dict(
        model.predict_sequences(
            3, 11, fmt, uni.map_pool, first_veto=first, top_k=20, preserve_marginals=False
        )
    )
    top = sorted(want.items(), key=lambda kv: -kv[1])[:20]
    kept = sum(p for _, p in top)
    for key, p in top:
        assert pruned[key] == pytest.approx(p / kept, rel=1e-6)


def test_first_veto_none_is_an_even_mixture_of_both_orders(
    fitted: tuple[VetoModel, SynthUniverse, list[VetoRecord], datetime],
) -> None:
    model, uni, _, _ = fitted
    for fmt in (MatchFormat.BO1, MatchFormat.BO3, MatchFormat.BO5):
        one = _as_dict(model.predict_sequences(2, 9, fmt, uni.map_pool, first_veto=T1, top_k=1_000_000))
        two = _as_dict(model.predict_sequences(2, 9, fmt, uni.map_pool, first_veto=T2, top_k=1_000_000))
        mix = _as_dict(model.predict_sequences(2, 9, fmt, uni.map_pool, first_veto=None, top_k=1_000_000))
        assert sum(mix.values()) == pytest.approx(1.0)
        assert set(mix) == set(one) | set(two)
        for key, p in mix.items():
            assert p == pytest.approx(0.5 * one.get(key, 0.0) + 0.5 * two.get(key, 0.0), rel=1e-9, abs=1e-15)
    # with unknown order the next step's actor is unknown, the action is not
    step = model.next_step(2, 9, MatchFormat.BO3, uni.map_pool, first_veto=None)
    assert step.team is None and step.action is BAN and step.step == 1
    assert sum(step.probabilities.values()) == pytest.approx(1.0)


def test_known_steps_naming_teams_resolve_the_veto_order(
    fitted: tuple[VetoModel, SynthUniverse, list[VetoRecord], datetime],
) -> None:
    model, uni, _, _ = fitted
    known = [VetoStep(step=1, action=BAN, team=T2, map_name="nuke")]
    inferred = _as_dict(
        model.predict_sequences(2, 9, MatchFormat.BO3, uni.map_pool, first_veto=None, known_steps=known)
    )
    explicit = _as_dict(
        model.predict_sequences(2, 9, MatchFormat.BO3, uni.map_pool, first_veto=T2, known_steps=known)
    )
    assert inferred == pytest.approx(explicit)
    assert all(pickers == (T2, T1, None) for _, pickers in inferred)
    # a wrong first_veto hint is overridden by what was actually published
    contradicted = _as_dict(
        model.predict_sequences(2, 9, MatchFormat.BO3, uni.map_pool, first_veto=T1, known_steps=known)
    )
    assert contradicted == pytest.approx(explicit)
    # steps without team information: the order stays a mixture, weighted by how likely each
    # team was to make the published choice
    anonymous = [VetoStep(step=1, action=BAN, team=None, map_name="nuke")]
    mix = model.predict_sequences(2, 9, MatchFormat.BO3, uni.map_pool, first_veto=None, known_steps=anonymous)
    assert sum(s.probability for s in mix) == pytest.approx(1.0)
    assert {tuple(s.pickers) for s in mix} == {(T1, T2, None), (T2, T1, None)}
    p_first_is_t1 = sum(s.probability for s in mix if s.pickers[0] is T1)
    like = {
        slot: model.next_step(2, 9, MatchFormat.BO3, uni.map_pool, first_veto=slot).probabilities["nuke"]
        for slot in (T1, T2)
    }
    full = model.predict_sequences(
        2, 9, MatchFormat.BO3, uni.map_pool, first_veto=None, known_steps=anonymous, top_k=1_000_000
    )
    p_exact = sum(s.probability for s in full if s.pickers[0] is T1)
    assert p_exact == pytest.approx(like[T1] / (like[T1] + like[T2]))
    assert 0.0 < p_first_is_t1 < 1.0


# ------------------------------------------------------------------------- known vetoes


def test_complete_known_veto_gives_one_certain_sequence(
    fitted: tuple[VetoModel, SynthUniverse, list[VetoRecord], datetime],
) -> None:
    model, uni, test, _ = fitted
    seen_formats: set[MatchFormat] = set()
    for rec in test[:80]:
        maps, pickers = _played_sequence(rec)
        for known in (rec.steps, rec.steps[:-1]):  # with and without the explicit leftover step
            seqs = model.predict_sequences(
                rec.team1_id, rec.team2_id, rec.format, uni.map_pool, known_steps=known, first_veto=None
            )
            assert len(seqs) == 1
            assert seqs[0].probability == 1.0
            assert seqs[0].maps == maps
            assert seqs[0].pickers == pickers
        seen_formats.add(rec.format)
    assert {MatchFormat.BO1, MatchFormat.BO3} <= seen_formats
    # also without any model knowledge, pool or correct hint
    rec = next(r for r in test if r.format is MatchFormat.BO3)
    maps, pickers = _played_sequence(rec)
    blank = VetoModel().predict_sequences(
        rec.team1_id, rec.team2_id, rec.format, [], known_steps=rec.steps, first_veto=T2
    )
    assert [(s.maps, s.pickers, s.probability) for s in blank] == [(maps, pickers, 1.0)]


def test_complete_known_veto_bo2_and_bo5() -> None:
    model = VetoModel()
    bo2 = veto_steps(
        [("ban", 2, "nuke"), ("ban", 1, "train"), ("pick", 2, "de_mirage"), ("pick", 1, "Dust II")]
    )
    seqs = model.predict_sequences(1, 2, MatchFormat.BO2, POOL7, known_steps=bo2)
    assert [(s.maps, s.pickers, s.probability) for s in seqs] == [(["mirage", "dust2"], [T2, T1], 1.0)]
    bo5 = veto_steps(
        [
            ("ban", 1, "nuke"),
            ("ban", 2, "train"),
            ("pick", 1, "mirage"),
            ("pick", 2, "dust2"),
            ("pick", 1, "inferno"),
            ("pick", 2, "ancient"),
        ]
    )
    seqs = model.predict_sequences(1, 2, MatchFormat.BO5, POOL7, known_steps=bo5)
    assert len(seqs) == 1 and seqs[0].probability == 1.0
    assert seqs[0].maps == ["mirage", "dust2", "inferno", "ancient", "anubis"]
    assert seqs[0].pickers == [T1, T2, T1, T2, None]
    # a BO1 decided by a pick (non-standard organiser rule) is still a complete veto
    bo1 = veto_steps([("ban", 1, "nuke"), ("ban", 2, "train"), ("pick", 1, "mirage")])
    seqs = model.predict_sequences(1, 2, MatchFormat.BO1, POOL7, known_steps=bo1)
    assert [(s.maps, s.pickers, s.probability) for s in seqs] == [(["mirage"], [T1], 1.0)]
    # next_step: after the last pick only the leftover remains, then the veto is complete
    last = model.next_step(1, 2, MatchFormat.BO5, POOL7, known_steps=bo5)
    assert (last.step, last.action, last.team) == (7, LEFTOVER, None)
    assert last.probabilities == {"anubis": 1.0}
    done = [*bo5, VetoStep(step=7, action=LEFTOVER, team=None, map_name="anubis")]
    with pytest.raises(ValueError, match="already complete"):
        model.next_step(1, 2, MatchFormat.BO5, POOL7, known_steps=done)


def test_partial_known_veto_conditions_the_remaining_path(
    fitted: tuple[VetoModel, SynthUniverse, list[VetoRecord], datetime],
) -> None:
    model, uni, _, _ = fitted
    pool = uni.map_pool
    known = veto_steps([("ban", 1, "nuke"), ("ban", 2, "train"), ("pick", 1, "mirage")])
    seqs = model.predict_sequences(4, 13, MatchFormat.BO3, pool, known_steps=known, top_k=1_000_000)
    assert sum(s.probability for s in seqs) == pytest.approx(1.0)
    assert len(seqs) == 4 * 3  # second pick among 4 maps, decider among the other 3
    for s in seqs:
        assert s.maps[0] == "mirage" and s.pickers == [T1, T2, None]
        assert not {"nuke", "train"} & set(s.maps)
    # chain rule: P(sequence | known) = sum_m P(next = m | known) * P(sequence | known + m)
    nxt = model.next_step(4, 13, MatchFormat.BO3, pool, known_steps=known)
    assert nxt.step == 4 and nxt.action is PICK and nxt.team is T2
    assert set(nxt.probabilities) == set(pool) - {"nuke", "train", "mirage"}
    combined: dict[SeqKey, float] = {}
    for m, q in nxt.probabilities.items():
        longer = [*known, VetoStep(step=4, action=PICK, team=T2, map_name=m)]
        for key, p in _as_dict(
            model.predict_sequences(4, 13, MatchFormat.BO3, pool, known_steps=longer, top_k=1_000_000)
        ).items():
            assert key[0][:2] == ("mirage", m)
            combined[key] = combined.get(key, 0.0) + q * p
    assert combined == pytest.approx(_as_dict(seqs))
    # conditioning equals restricting the unconditional distribution when only the opening bans
    # are known (the bans do not change later choice probabilities among the remaining maps)
    bans = known[:2]
    cond = _as_dict(model.predict_sequences(4, 13, MatchFormat.BO3, pool, known_steps=bans, top_k=1_000_000))
    assert sum(cond.values()) == pytest.approx(1.0)
    assert all(not {"nuke", "train"} & set(maps) for maps, _ in cond)


def test_known_later_steps_condition_earlier_unknown_ones(
    fitted: tuple[VetoModel, SynthUniverse, list[VetoRecord], datetime],
) -> None:
    """Only the picks were published (steps 3 and 4): the bans and the decider stay uncertain."""
    model, uni, _, _ = fitted
    pool = uni.map_pool
    picks = [
        VetoStep(step=3, action=PICK, team=T1, map_name="inferno"),
        VetoStep(step=4, action=PICK, team=T2, map_name="anubis"),
    ]
    seqs = model.predict_sequences(4, 13, MatchFormat.BO3, pool, known_steps=picks, top_k=1_000_000)
    assert sum(s.probability for s in seqs) == pytest.approx(1.0)
    assert len(seqs) == 5  # any of the other five maps can be the decider
    assert all(s.maps[:2] == ["inferno", "anubis"] and s.pickers == [T1, T2, None] for s in seqs)
    # Bayes by hand: P(decider | picks) from the unconditional distribution
    full = _as_dict(model.predict_sequences(4, 13, MatchFormat.BO3, pool, top_k=1_000_000))
    mass = {maps[2]: p for (maps, _), p in full.items() if maps[:2] == ("inferno", "anubis")}
    total = sum(mass.values())
    for s in seqs:
        assert s.probability == pytest.approx(mass[s.maps[2]] / total, rel=1e-6)
    # the first unknown step cannot remove a map that is known to be picked later
    first = model.next_step(4, 13, MatchFormat.BO3, pool, known_steps=picks)
    assert first.step == 1 and first.action is BAN


def test_next_step_excludes_maps_published_for_later_steps(
    fitted: tuple[VetoModel, SynthUniverse, list[VetoRecord], datetime],
) -> None:
    model, uni, _, _ = fitted
    # steps 1 and 4 are known (a gap at steps 2-3): step 2 cannot remove the map picked at step 4
    known = [
        VetoStep(step=1, action=BAN, team=T1, map_name="nuke"),
        VetoStep(step=4, action=PICK, team=T2, map_name="mirage"),
    ]
    forecast = model.next_step(3, 11, MatchFormat.BO3, uni.map_pool, known_steps=known)
    assert forecast.step == 2 and forecast.action is BAN and forecast.team is T2
    assert set(forecast.probabilities) == set(uni.map_pool) - {"nuke", "mirage"}
    assert sum(forecast.probabilities.values()) == pytest.approx(1.0)
    full = veto_steps(
        [
            ("ban", 1, "nuke"),
            ("ban", 2, "train"),
            ("pick", 1, "mirage"),
            ("pick", 2, "dust2"),
            ("ban", 1, "anubis"),
            ("ban", 2, "ancient"),
            ("leftover", None, "inferno"),
        ]
    )
    with pytest.raises(ValueError, match="already complete"):
        model.next_step(3, 11, MatchFormat.BO3, uni.map_pool, known_steps=full)


def test_known_steps_that_deviate_from_the_template() -> None:
    """Organiser-specific order (BO1: A bans 2, B bans 3, A bans 1): published steps win."""
    model = VetoModel()
    known = veto_steps([("ban", 1, "nuke"), ("ban", 1, "train"), ("ban", 2, "mirage"), ("ban", 2, "dust2")])
    seqs = model.predict_sequences(1, 2, MatchFormat.BO1, POOL7, known_steps=known)
    assert sum(s.probability for s in seqs) == pytest.approx(1.0)
    assert {s.maps[0] for s in seqs} == {"ancient", "anubis", "inferno"}
    assert all(s.pickers == [None] for s in seqs)
    # a pick where the template expects a ban: the remaining steps are re-derived
    odd = veto_steps([("pick", 1, "nuke"), ("pick", 2, "train")])
    seqs = model.predict_sequences(1, 2, MatchFormat.BO3, POOL7, known_steps=odd, top_k=100)
    assert sum(s.probability for s in seqs) == pytest.approx(1.0)
    assert all(
        s.maps[:2] == ["nuke", "train"] and len(s.maps) == 3 and s.pickers == [T1, T2, None] for s in seqs
    )
    assert {s.maps[2] for s in seqs} == set(POOL7) - {"nuke", "train"}
    # ... and a deviating step without team information keeps the template's actor
    anonymous = [VetoStep(step=1, action=PICK, team=None, map_name="nuke")]
    seqs = model.predict_sequences(1, 2, MatchFormat.BO3, POOL7, known_steps=anonymous, top_k=100)
    assert all(s.maps[0] == "nuke" and s.pickers == [T1, T2, None] for s in seqs)
    seqs = model.predict_sequences(
        1, 2, MatchFormat.BO3, POOL7, known_steps=anonymous, first_veto=None, top_k=100
    )
    assert sum(s.probability for s in seqs) == pytest.approx(1.0)
    assert {tuple(s.pickers) for s in seqs} == {(T1, T2, None), (T2, T1, None)}
    # an explicit custom template (BO3 on a 7-map pool: pick, pick, then four bans)
    custom = [VetoTemplateStep(PICK, 0), VetoTemplateStep(PICK, 1)] + [
        VetoTemplateStep(BAN, i % 2) for i in range(4)
    ]
    seqs = model.predict_sequences(1, 2, MatchFormat.BO3, POOL7, template=custom, top_k=100_000)
    assert sum(s.probability for s in seqs) == pytest.approx(1.0)
    assert len(seqs) == 7 * 6 * 5
    assert all(s.pickers == [T1, T2, None] for s in seqs)
    # the ready-made 2-3-1 BO1 template: team 1 bans twice, team 2 three times, team 1 once
    assert [st.actor for st in BO1_TEMPLATE_2_3_1] == [0, 0, 1, 1, 1, 0, None]
    step2 = model.next_step(1, 2, MatchFormat.BO1, POOL7, known_steps=known[:1], template=BO1_TEMPLATE_2_3_1)
    assert (step2.step, step2.action, step2.team) == (2, BAN, T1)
    step3 = model.next_step(1, 2, MatchFormat.BO1, POOL7, known_steps=known[:2], template=BO1_TEMPLATE_2_3_1)
    assert (step3.step, step3.action, step3.team) == (3, BAN, T2)
    seqs = model.predict_sequences(1, 2, MatchFormat.BO1, POOL7, template=BO1_TEMPLATE_2_3_1)
    assert len(seqs) == 7 and sum(s.probability for s in seqs) == pytest.approx(1.0)
    # impossible combinations are reported instead of returning garbage
    leftover_first = veto_steps([("ban", 1, "nuke"), ("leftover", None, "train"), ("ban", 1, "mirage")])
    with pytest.raises(ValueError, match="leftover map must be the last"):
        model.predict_sequences(1, 2, MatchFormat.BO3, POOL7, known_steps=leftover_first)
    banned_decider = [
        VetoStep(step=1, action=BAN, team=T1, map_name="nuke"),
        VetoStep(step=3, action=PICK, team=T1, map_name="mirage"),
        VetoStep(step=4, action=PICK, team=T2, map_name="dust2"),
        VetoStep(step=7, action=LEFTOVER, team=None, map_name="inferno"),
    ]
    seqs = model.predict_sequences(1, 2, MatchFormat.BO3, POOL7, known_steps=banned_decider)
    assert [(s.maps, s.probability) for s in seqs] == [(["mirage", "dust2", "inferno"], 1.0)]


# ----------------------------------------------------------------------- derived views


def test_map_slot_distribution_and_p_map_played() -> None:
    seqs = [
        MapSequenceProbability(maps=["mirage", "nuke", "inferno"], pickers=[T1, T2, None], probability=0.5),
        MapSequenceProbability(maps=["mirage", "inferno", "dust2"], pickers=[T1, T2, None], probability=0.3),
        MapSequenceProbability(maps=["nuke", "mirage", "dust2"], pickers=[T1, T2, None], probability=0.2),
    ]
    slots = map_slot_distribution(seqs)
    assert slots == {
        1: pytest.approx({"mirage": 0.8, "nuke": 0.2}),
        2: pytest.approx({"nuke": 0.5, "inferno": 0.3, "mirage": 0.2}),
        3: pytest.approx({"inferno": 0.5, "dust2": 0.5}),
    }
    assert list(slots[2]) == ["nuke", "inferno", "mirage"]  # sorted by probability
    assert p_map_played(seqs, "mirage") == pytest.approx(1.0)
    assert p_map_played(seqs, "de_nuke") == pytest.approx(0.7)
    assert p_map_played(seqs, "Dust II") == pytest.approx(0.5)
    assert p_map_played(seqs, "train") == 0.0
    # the decider is only reached 40% of the time
    assert p_map_played(seqs, "dust2", slot_reach={3: 0.4}) == pytest.approx(0.2)
    assert p_map_played(seqs, "inferno", slot_reach={3: 0.4}) == pytest.approx(0.3 + 0.5 * 0.4)
    # unnormalised input is normalised; empty input is harmless
    doubled = [s.model_copy(update={"probability": 2 * s.probability}) for s in seqs]
    assert map_slot_distribution(doubled)[1] == pytest.approx({"mirage": 0.8, "nuke": 0.2})
    assert p_map_played(doubled, "nuke") == pytest.approx(0.7)
    assert map_slot_distribution([]) == {}
    assert p_map_played([], "nuke") == 0.0
    assert VetoModel.map_slot_distribution(seqs) == slots
    assert VetoModel.p_map_played(seqs, "nuke") == pytest.approx(0.7)


def test_slot_distributions_of_model_output(
    fitted: tuple[VetoModel, SynthUniverse, list[VetoRecord], datetime],
) -> None:
    model, uni, _, _ = fitted
    for fmt in MatchFormat:
        seqs = model.predict_sequences(5, 6, fmt, uni.map_pool, top_k=1_000_000)
        slots = map_slot_distribution(seqs)
        assert sorted(slots) == list(range(1, fmt.n_maps + 1))
        for dist in slots.values():
            assert sum(dist.values()) == pytest.approx(1.0)
        total = sum(p_map_played(seqs, m) for m in uni.map_pool)
        assert total == pytest.approx(fmt.n_maps)  # every sequence holds exactly n_maps different maps
        for m in uni.map_pool:
            assert p_map_played(seqs, m) == pytest.approx(sum(d.get(m, 0.0) for d in slots.values()))


# --------------------------------------------------------------- time handling, persistence


def test_as_of_and_update_are_chronological(
    fitted: tuple[VetoModel, SynthUniverse, list[VetoRecord], datetime],
) -> None:
    model, uni, test, cut = fitted
    last = model.last_veto_time
    assert last is not None and last < cut
    with pytest.raises(ValueError, match="not after the last fitted veto"):
        model.predict_sequences(1, 2, MatchFormat.BO3, uni.map_pool, as_of=last)
    with pytest.raises(ValueError):
        model.preference(1, "nuke", BAN, last - timedelta(days=1))
    assert model.predict_sequences(1, 2, MatchFormat.BO3, uni.map_pool, as_of=cut)
    # fit_until == fit on the earlier records
    recs = sorted(uni.veto_records(), key=lambda r: r.played_at)
    until = VetoModel().fit_until(recs, cut)
    assert until.to_dict() == model.to_dict()
    # incremental updates: counts move, coefficients stay, duplicates and old records are refused
    live = model.copy()
    assert live.update(test[0]) is True
    assert live.update(test[0]) is False
    assert live.n_records == model.n_records + 1
    assert live.coefficients == model.coefficients
    assert live.last_veto_time == test[0].played_at
    with pytest.raises(ValueError, match="chronologically"):
        live.update(recs[0].model_copy(update={"match_id": 10_000_000}))
    assert model.n_records == until.n_records  # the copy is independent


def test_save_load_round_trip(tmp_path: Path) -> None:
    uni = universe(seed=3)
    train, _, cut = _split(uni, 0.4)
    ratings = MapRatingModel().fit_until(uni.map_results(), cut, record_history=True)
    model = VetoModel(VetoConfig(half_life_days=75.0, team_prior_strength=4.0)).fit(
        train, map_advantage=ratings.historical_map_advantage
    )
    path = model.save(tmp_path / "nested" / "veto.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["kind"] == "cs2bot.VetoModel"
    loaded = VetoModel.load(path)
    assert loaded.config == model.config
    assert loaded.coefficients == model.coefficients
    assert loaded.coefficients_with_advantage == model.coefficients_with_advantage
    assert loaded.last_veto_time == model.last_veto_time
    assert loaded.to_dict() == model.to_dict()

    def advantage(team: int, opp: int, map_name: str, as_of: datetime) -> float:
        return ratings.map_advantage(team, opp, map_name, cut)

    for fmt, adv in itertools.product((MatchFormat.BO1, MatchFormat.BO3), (None, advantage)):
        want = model.predict_sequences(3, 8, fmt, uni.map_pool, as_of=cut, map_advantage=adv)
        got = loaded.predict_sequences(3, 8, fmt, uni.map_pool, as_of=cut, map_advantage=adv)
        assert [s.model_dump() for s in got] == [s.model_dump() for s in want]
    with pytest.raises(ValueError):
        VetoModel.from_dict({"kind": "cs2bot.MapRatingModel"})
    bad = model.to_dict()
    bad["version"] = 42
    with pytest.raises(ValueError):
        VetoModel.from_dict(bad)
    empty = VetoModel.load(VetoModel().save(tmp_path / "empty.json"))
    assert empty.n_records == 0 and empty.last_veto_time is None


def test_default_pool_comes_from_the_active_duty_list() -> None:
    assert list(VetoConfig().default_map_pool) == POOL7
    # records without a pool and without a leftover step fall back to the default pool
    rec = VetoRecord(
        match_id=1,
        played_at=T0,
        format=MatchFormat.BO3,
        team1_id=1,
        team2_id=2,
        steps=veto_steps([("ban", 1, "nuke"), ("ban", 2, "train")]),
        map_pool=None,
    )
    model = VetoModel().fit([rec])
    assert model.preference(1, "nuke", BAN) > 0 > model.preference(1, "mirage", BAN)
    # an empty pool at prediction time (a MatchContext that has none yet) means the default pool too
    seqs = model.predict_sequences(1, 2, MatchFormat.BO1, [])
    assert {s.maps[0] for s in seqs} == set(POOL7)
    assert sum(s.probability for s in seqs) == pytest.approx(1.0)
    custom = VetoModel(VetoConfig(default_map_pool=("mirage", "nuke", "dust2")))
    assert {s.maps[0] for s in custom.predict_sequences(1, 2, MatchFormat.BO1, [])} == {
        "mirage",
        "nuke",
        "dust2",
    }
