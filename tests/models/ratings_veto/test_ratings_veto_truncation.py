"""Marginal-preserving truncation of the veto's map-sequence distribution (``top_k``)."""

from __future__ import annotations

import itertools
from datetime import timedelta

import pytest

from cs2bot.domain.enums import MatchFormat, TeamSlot
from cs2bot.domain.predictions import MapSequenceProbability
from cs2bot.models.ratings import MapRatingModel, RatingConfig
from cs2bot.models.veto import VetoModel, map_slot_distribution, p_map_played
from cs2bot.models.veto_truncation import marginal_rank, slot_marginal_rank, truncate_sequences
from cs2bot.testing.synthetic import SynthUniverse

from .rv_helpers import POOL7, universe, veto_steps

T1, T2 = TeamSlot.TEAM1, TeamSlot.TEAM2
Key = tuple[tuple[str, TeamSlot | None], ...]


@pytest.fixture(scope="module")
def fitted() -> tuple[VetoModel, SynthUniverse]:
    uni = universe(seed=3)
    return VetoModel().fit(uni.veto_records()), uni


def _key(seq: MapSequenceProbability) -> Key:
    return tuple(zip(seq.maps, seq.pickers, strict=True))


def _max_slot_error(
    full: list[MapSequenceProbability], cut: list[MapSequenceProbability], pool: list[str]
) -> float:
    want, got = map_slot_distribution(full), map_slot_distribution(cut)
    assert set(got) == set(want)
    return max(abs(want[k].get(m, 0.0) - got[k].get(m, 0.0)) for k in want for m in pool)


@pytest.mark.parametrize("fmt", [MatchFormat.BO2, MatchFormat.BO3, MatchFormat.BO5])
@pytest.mark.parametrize("first", [T1, None])
def test_default_top_k_keeps_slot_marginals_exact(
    fitted: tuple[VetoModel, SynthUniverse], fmt: MatchFormat, first: TeamSlot | None
) -> None:
    model, uni = fitted
    for team1, team2 in [(1, 2), (5, 9), (7, 12)]:
        full = model.predict_sequences(team1, team2, fmt, uni.map_pool, first_veto=first, top_k=None)
        cut = model.predict_sequences(team1, team2, fmt, uni.map_pool, first_veto=first)
        assert len(cut) <= 50
        assert len({_key(s) for s in cut}) == len(cut)  # still aggregated: no duplicates
        assert sum(s.probability for s in cut) == pytest.approx(1.0)
        assert all(s.probability > 0.0 for s in cut)
        assert [s.probability for s in cut] == sorted((s.probability for s in cut), reverse=True)
        assert all(len(set(s.maps)) == fmt.n_maps == len(s.pickers) for s in cut)
        exact = {_key(s): s.probability for s in full}
        assert {_key(s) for s in cut} <= set(exact)  # only sequences the veto can produce
        if len(full) <= 50:
            assert {_key(s): s.probability for s in cut} == pytest.approx(exact)
            continue
        # every slot's map distribution - and so the chance that a map is played - is exact
        assert _max_slot_error(full, cut, uni.map_pool) < 1e-9
        for m in uni.map_pool:
            assert p_map_played(cut, m) == pytest.approx(p_map_played(full, m), abs=1e-9)
        # the most likely sequences are all kept and never lose probability
        rank = marginal_rank([_key(s) for s in full])
        head = full[: 50 - rank]
        got = {_key(s): s.probability for s in cut}
        assert len(head) >= 12
        assert all(got[_key(s)] >= s.probability - 1e-12 for s in head)


def test_plain_truncation_distorts_what_is_played(fitted: tuple[VetoModel, SynthUniverse]) -> None:
    """Why the default preserves marginals: rescaling the top 50 moves map probabilities a lot."""
    model, uni = fitted
    worst_plain = 0.0
    for team1, team2 in [(1, 2), (5, 9), (7, 12), (10, 16)]:
        kw = {"first_veto": None}
        full = model.predict_sequences(team1, team2, MatchFormat.BO3, uni.map_pool, top_k=None, **kw)
        plain = model.predict_sequences(
            team1, team2, MatchFormat.BO3, uni.map_pool, preserve_marginals=False, **kw
        )
        assert len(plain) == 50
        assert sum(s.probability for s in plain) == pytest.approx(1.0)
        worst_plain = max(worst_plain, _max_slot_error(full, plain, uni.map_pool))
    assert worst_plain > 0.05


def test_marginal_rank_of_the_standard_formats() -> None:
    def all_sequences(n_slots: int) -> list[Key]:
        return [tuple((m, None) for m in maps) for maps in itertools.permutations(POOL7, n_slots)]

    # #cells - #slots + 1: every slot's cells sum to the same total
    assert slot_marginal_rank(all_sequences(1)) == 7
    assert slot_marginal_rank(all_sequences(2)) == 13
    assert slot_marginal_rank(all_sequences(3)) == 19
    assert slot_marginal_rank(all_sequences(5)) == 31
    assert slot_marginal_rank([]) == 0


def test_truncate_sequences_on_a_hand_made_distribution() -> None:
    maps = ["a", "b", "c", "d"]
    weights = {"a": 4.0, "b": 3.0, "c": 2.0, "d": 1.0}
    totals: dict[Key, float] = {}
    for first, second in itertools.permutations(maps, 2):
        totals[((first, T1), (second, T2))] = weights[first] * weights[second] ** 2
    mass = sum(totals.values())
    assert len(totals) == 12
    # nothing to cut: normalised, most likely first
    everything = truncate_sequences(totals, None)
    assert len(everything) == 12
    assert [p for _, p in everything] == sorted((p for _, p in everything), reverse=True)
    assert dict(everything) == pytest.approx({k: v / mass for k, v in totals.items()})
    assert dict(truncate_sequences(totals, 12)) == pytest.approx(dict(everything))
    # rank = 8 cells - 2 slots + 1 = 7: nine sequences reproduce both slot distributions
    assert slot_marginal_rank(list(totals)) == 7
    cut = truncate_sequences(totals, 9)
    assert len(cut) <= 9
    assert sum(p for _, p in cut) == pytest.approx(1.0)
    for slot in (0, 1):
        for m in maps:
            want = sum(v for k, v in totals.items() if k[slot][0] == m) / mass
            got = sum(p for k, p in cut if k[slot][0] == m)
            assert got == pytest.approx(want, abs=1e-9)
    # too short a list for that, or switched off: plain top-k, rescaled
    for plain in (truncate_sequences(totals, 3), truncate_sequences(totals, 9, preserve_marginals=False)):
        top = everything[: len(plain)]
        kept = sum(p for _, p in top)
        assert [k for k, _ in plain] == [k for k, _ in top]
        assert [p for _, p in plain] == pytest.approx([p / kept for _, p in top])


def test_truncate_sequences_edge_cases() -> None:
    with pytest.raises(ValueError, match="top_k"):
        truncate_sequences({(("a", None),): 1.0}, 0)
    with pytest.raises(ValueError, match="no positive probability"):
        truncate_sequences({}, 5)
    with pytest.raises(ValueError, match="no positive probability"):
        truncate_sequences({(("a", None),): 0.0}, 5)
    # zero-probability sequences are dropped; exact ties (incl. None vs team pickers) sort stably
    ties: dict[Key, float] = {
        (("a", None),): 1.0,
        (("a", T1),): 1.0,
        (("b", T2),): 1.0,
        (("c", None),): 0.0,
    }
    out = truncate_sequences(ties, 2, preserve_marginals=False)
    assert [k for k, _ in out] == [(("a", None),), (("a", T1),)]
    assert [p for _, p in out] == pytest.approx([0.5, 0.5])
    assert truncate_sequences(ties, 2, preserve_marginals=False) == out
    assert len(truncate_sequences(ties, None)) == 3


def test_known_steps_and_truncation_work_together(fitted: tuple[VetoModel, SynthUniverse]) -> None:
    """Conditioning on a published first ban shrinks the support; the cut stays marginal-exact."""
    model, uni = fitted
    known = veto_steps([("ban", 1, "nuke")])
    full = model.predict_sequences(4, 8, MatchFormat.BO5, uni.map_pool, known_steps=known, top_k=None)
    cut = model.predict_sequences(4, 8, MatchFormat.BO5, uni.map_pool, known_steps=known, top_k=40)
    assert len(full) > 40 >= len(cut)
    assert all("nuke" not in s.maps for s in cut)
    assert _max_slot_error(full, cut, uni.map_pool) < 1e-9


def _picker_marginals(seqs: list[MapSequenceProbability]) -> dict[tuple[str, object], float]:
    """P(slot k picked by p) and P(map m picked by p) of a sequence list."""
    out: dict[tuple[str, object], float] = {}
    total = sum(s.probability for s in seqs)
    for s in seqs:
        for k, (m, p) in enumerate(zip(s.maps, s.pickers, strict=True)):
            for cell in ((f"slot{k}", p), (m, p)):
                out[cell] = out.get(cell, 0.0) + s.probability / total
    return out


@pytest.mark.parametrize("fmt", [MatchFormat.BO3, MatchFormat.BO5])
def test_truncation_keeps_picker_marginals_when_the_first_veto_is_unknown(
    fitted: tuple[VetoModel, SynthUniverse], fmt: MatchFormat
) -> None:
    """Review round 1: ties between mirrored sequences of unknown (evenly rated) teams were broken
    toward team1, so the cut list said team1 picks map 1 with probability ~0.95-0.99 instead of 0.5."""
    model, uni = fitted
    for team1, team2 in [(990001, 990002), (1, 2)]:
        full = model.predict_sequences(team1, team2, fmt, uni.map_pool, first_veto=None, top_k=None)
        cut = model.predict_sequences(team1, team2, fmt, uni.map_pool, first_veto=None)
        assert len(cut) <= 50 < len(full)
        want, got = _picker_marginals(full), _picker_marginals(cut)
        assert set(got) <= set(want)
        assert max(abs(want[c] - got.get(c, 0.0)) for c in want) < 1e-9
        if team1 == 990001:
            assert got[("slot0", T1)] == pytest.approx(0.5, abs=1e-9)
        assert _max_slot_error(full, cut, uni.map_pool) < 1e-9


def test_per_map_estimates_are_the_same_from_the_cut_list(fitted: tuple[VetoModel, SynthUniverse]) -> None:
    """``predict_for_sequences`` (which reads who picked each map) gives identical estimates."""
    model, uni = fitted
    recs = uni.map_results()
    ratings = MapRatingModel(RatingConfig(pick_advantage=0.3)).fit(recs)
    as_of = max(r.played_at for r in recs) + timedelta(days=1)
    full = model.predict_sequences(3, 11, MatchFormat.BO5, uni.map_pool, first_veto=None, top_k=None)
    cut = model.predict_sequences(3, 11, MatchFormat.BO5, uni.map_pool, first_veto=None)
    a = ratings.predict_for_sequences(3, 11, full, as_of)
    b = ratings.predict_for_sequences(3, 11, cut, as_of)
    assert set(a) == set(b)
    for m in a:
        assert b[m].p_team1_win == pytest.approx(a[m].p_team1_win, abs=1e-9)
        assert b[m].sigma == pytest.approx(a[m].sigma, abs=1e-9)


def test_marginal_rank_with_unknown_first_veto() -> None:
    def keys(n_slots: int, pattern: tuple[TeamSlot | None, ...]) -> list[Key]:
        return [tuple(zip(maps, pattern, strict=True)) for maps in itertools.permutations(POOL7, n_slots)]

    bo3, bo3_rev = keys(3, (T1, T2, None)), keys(3, (T2, T1, None))
    bo5, bo5_rev = keys(5, (T1, T2, T1, T2, None)), keys(5, (T2, T1, T2, T1, None))
    # a known order: the pickers follow from the slots, nothing to add
    assert marginal_rank(bo3) == slot_marginal_rank(bo3) == 19
    assert marginal_rank(bo5) == slot_marginal_rank(bo5) == 31
    # unknown order: one extra constraint per map
    assert marginal_rank(bo3 + bo3_rev) == 26
    assert marginal_rank(bo5 + bo5_rev) == 38
    assert marginal_rank([]) == 0
