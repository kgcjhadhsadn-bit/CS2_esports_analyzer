"""RoundModel: priors, vectorised probabilities, fitting on the synthetic universe, persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from cs2bot.domain.enums import BuyType, RoundEndReason, Side, TeamSlot
from cs2bot.domain.records import RoundRecord
from cs2bot.models.economy import BUY_CODE
from cs2bot.models.round_model import RoundModel, classify_buy
from cs2bot.testing.synthetic import BUY_VALUE, DEFAULT_CT_BIAS, SynthUniverse


def test_priors_reflect_public_knowledge() -> None:
    rm = RoundModel.default()
    # CT round win rates per map roughly 45-58 %
    for m in ("nuke", "ancient", "anubis", "mirage", "inferno", "dust2", "train", "overpass"):
        p_ct = float(rm.round_win_prob(Side.CT, BuyType.FULL, BuyType.FULL, m))
        assert 0.44 <= p_ct <= 0.58, (m, p_ct)
    assert rm.ct_bias_for("de_nuke") > rm.ct_bias_for("anubis")
    # full buy vs eco ~85-90 %, pistol rounds ~50/50 apart from the side bias
    p_full_eco = float(rm.round_win_prob(Side.T, BuyType.FULL, BuyType.ECO, "dust2"))
    assert 0.84 <= p_full_eco <= 0.91
    assert abs(float(rm.round_win_prob(Side.CT, BuyType.PISTOL, BuyType.PISTOL, "dust2")) - 0.5) < 0.02
    # unknown maps fall back to a small CT edge
    assert 0.5 < float(rm.round_win_prob(Side.CT, BuyType.FULL, BuyType.FULL, "brandnewmap")) < 0.53


def test_buy_table_antisymmetric_and_vectorised() -> None:
    rm = RoundModel.default()
    table = rm.buy_table
    np.testing.assert_allclose(table, -table.T)
    assert np.all(np.diag(table) == 0)
    rng = np.random.default_rng(0)
    a_ct = rng.random(1000) < 0.5
    ba = rng.integers(0, 4, 1000)
    bb = rng.integers(0, 4, 1000)
    d = rng.normal(0, 0.5, 1000)
    p = rm.round_win_prob_codes(a_ct, ba, bb, "mirage", d)
    q = rm.round_win_prob_codes(~a_ct, bb, ba, "mirage", -d)
    assert p.shape == (1000,)
    np.testing.assert_allclose(p + q, 1.0, atol=1e-12)  # the two teams' probabilities are complementary
    # enum / sequence input gives the same answer as codes
    p_enum = rm.round_win_prob(
        [Side.CT, Side.T], [BuyType.FULL, BuyType.ECO], [BuyType.FORCE, BuyType.FULL], "nuke", 0.3
    )
    p_code = rm.round_win_prob_codes(
        np.array([True, False]),
        np.array([BUY_CODE[BuyType.FULL], BUY_CODE[BuyType.ECO]]),
        np.array([BUY_CODE[BuyType.FORCE], BUY_CODE[BuyType.FULL]]),
        "nuke",
        0.3,
    )
    np.testing.assert_allclose(p_enum, p_code)
    # monotone in strength
    ps = rm.round_win_prob_codes(
        np.ones(5, bool), np.full(5, 3), np.full(5, 3), "nuke", np.linspace(-1, 1, 5)
    )
    assert np.all(np.diff(ps) > 0)


def test_fit_recovers_synthetic_truth(fitted_round_model: RoundModel) -> None:
    rm = fitted_round_model
    assert rm.fit_stats["converged"] and rm.fit_stats["n_rounds"] > 5000
    for m, truth in DEFAULT_CT_BIAS.items():
        assert abs(rm.ct_bias[m] - truth) < 0.15, (m, rm.ct_bias[m], truth)
    est = np.array([rm.ct_bias[m] for m in DEFAULT_CT_BIAS])
    true = np.array(list(DEFAULT_CT_BIAS.values()))
    assert np.corrcoef(est, true)[0, 1] > 0.6
    for a, b in ((BuyType.FULL, BuyType.ECO), (BuyType.FULL, BuyType.FORCE), (BuyType.FORCE, BuyType.ECO)):
        assert abs(rm.buy_effect(a, b) - (BUY_VALUE[a] - BUY_VALUE[b])) < 0.2, (a, b)
    assert abs(rm.pistol_ct_adj) < 0.2  # synthetic pistol rounds have no extra side bias


def _rounds(n: int, *, ct_win_rate: float, map_name: str = "nuke", seed: int = 0) -> list[RoundRecord]:
    rng = np.random.default_rng(seed)
    t0 = datetime(2025, 1, 1, tzinfo=UTC)
    out = []
    for i in range(n):
        side = Side.CT if i % 2 == 0 else Side.T
        ct_wins = rng.random() < ct_win_rate
        winner = TeamSlot.TEAM1 if (side is Side.CT) == ct_wins else TeamSlot.TEAM2
        out.append(
            RoundRecord(
                match_id=i // 24,
                map_number=1,
                map_name=map_name,
                played_at=t0 + timedelta(hours=i // 24),
                round_number=3 + i % 9,
                team1_id=1,
                team2_id=2,
                team1_side=side,
                winner=winner,
                end_reason=RoundEndReason.ELIMINATION,
                team1_buy=BuyType.FULL,
                team2_buy=BuyType.FULL,
            )
        )
    return out


def test_thin_data_stays_near_priors_and_lots_of_data_moves() -> None:
    prior = RoundModel.default().ct_bias_for("nuke")
    thin = RoundModel().fit(_rounds(40, ct_win_rate=0.30))
    raw = float(np.log(0.3 / 0.7))
    # regularised: the estimate moves only a small part of the way from the prior to the raw logit
    assert raw < thin.ct_bias["nuke"] < prior
    assert abs(thin.ct_bias["nuke"] - prior) < 0.25 * abs(raw - prior)
    lots = RoundModel().fit(_rounds(6000, ct_win_rate=0.30, seed=1))
    assert lots.ct_bias["nuke"] < -0.5  # logit(0.30) = -0.85; data dominate
    empty = RoundModel().fit([])
    assert empty.fit_stats["n_rounds"] == 0 and empty.ct_bias_for("nuke") == prior


def test_fit_respects_as_of_and_skips_unknown_buys() -> None:
    recs = _rounds(2000, ct_win_rate=0.70, seed=3)
    late = [
        r.model_copy(update={"played_at": r.played_at + timedelta(days=400)})
        for r in _rounds(2000, ct_win_rate=0.2)
    ]
    cutoff = datetime(2025, 6, 1, tzinfo=UTC)
    m_cut = RoundModel().fit(recs + late, as_of=cutoff)
    m_all = RoundModel().fit(recs + late)
    assert m_cut.fit_stats["n_rounds"] == 2000
    assert m_cut.ct_bias["nuke"] > m_all.ct_bias["nuke"] + 0.3  # future (T-sided) rounds were excluded
    unknown = [r.model_copy(update={"team1_buy": None, "team2_buy": None}) for r in recs[:10]]
    stats = RoundModel().fit(unknown + recs[10:20]).fit_stats
    assert stats["n_skipped"] == 10 and stats["n_rounds"] == 10


def test_classify_buy() -> None:
    assert classify_buy(None, 1) is BuyType.PISTOL and classify_buy(20000, 13) is BuyType.PISTOL
    assert classify_buy(3000, 5) is BuyType.ECO
    assert classify_buy(12000, 5) is BuyType.FORCE
    assert classify_buy(24000, 5) is BuyType.FULL
    assert classify_buy(None, 5) is None


def test_save_load_roundtrip(tmp_path: Path, synthetic_universe: SynthUniverse) -> None:
    rm = RoundModel().fit(synthetic_universe.round_records()[:3000])
    path = rm.save(tmp_path / "round_model.json")
    back = RoundModel.load(path)
    assert back.fingerprint() == rm.fingerprint()
    assert back.ct_bias == pytest.approx(rm.ct_bias)
    np.testing.assert_allclose(back.buy_table, rm.buy_table)
    assert back.fit_stats["n_rounds"] == rm.fit_stats["n_rounds"]
    with pytest.raises(ValueError):
        RoundModel.from_dict({"kind": "something-else"})
