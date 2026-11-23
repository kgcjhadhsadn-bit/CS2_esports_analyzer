"""Rationale building blocks: a compact digest of a prediction and a deterministic template.

* :func:`build_rationale_digest` condenses a :class:`MatchPrediction` (whose score distributions are
  far too large to send) plus the bet decisions into a small JSON-able dict. It is the only thing
  the rationale model sees, and it is also what the template renders - so both describe the same
  numbers.
* :func:`render_template_rationale` produces the Markdown used when Claude is disabled or a request
  fails. It is deterministic: the same inputs always give the same text.
* :func:`finalize_rationale` post-processes model output: HTML and image embeds are removed (the
  text is rendered in a browser and partly derives from untrusted news) and the risk disclaimer is
  appended when the text does not already say that losses are possible.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

from cs2bot.domain.betting import BetDecision
from cs2bot.domain.context import ContextFlag
from cs2bot.domain.enums import TeamSlot
from cs2bot.domain.match import MatchContext
from cs2bot.domain.predictions import MapSlotPrediction, MatchPrediction

RISK_DISCLAIMER = (
    "No outcome is certain: these are model estimates, even high-probability bets lose regularly, "
    "and losses are possible."
)
# English wording plus the Polish equivalents ("strata", "przegrać") in case the language is switched.
_LOSS_PATTERN = re.compile(
    r"\b(loss|losses|lose|loses|losing|strat(?:a|y|ę|ą|om|ami|ach)?|przegra\w*)\b", re.IGNORECASE
)
# A tag starts with a letter, "/" or "!" right after "<" - so "p < 0.75 and EV > 0.03" is left alone.
_HTML_TAG = re.compile(r"</?[A-Za-z!][^>\n]{0,400}>")
_MD_IMAGE = re.compile(r"!\[([^\]\n]*)\]\([^)\n]*\)")
# Inline links "[text](target)" and reference definitions "[label]: target".
_MD_LINK = re.compile(r"\[([^\]\n]*)\]\(\s*<?([^)\s>]*)[^)\n]*\)")
_MD_LINK_DEFINITION = re.compile(r"^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*<?(\S*).*$", re.MULTILINE)
_SAFE_LINK = re.compile(r"^(https?://|#|/(?!/))", re.IGNORECASE)
_MAX_SEQUENCES = 5
_MAX_SCORES = 6


def team_names(prediction: MatchPrediction, ctx: MatchContext) -> tuple[str, str]:
    """Display names of team1 / team2 (falling back to ``Team <id>``)."""
    return (
        ctx.team1_name or f"Team {prediction.team1_id}",
        ctx.team2_name or f"Team {prediction.team2_id}",
    )


def _expected_rounds(slot: MapSlotPrediction) -> float | None:
    """Mean total rounds of a map slot from its joint score pmf (``"13-9"`` -> p)."""
    total_p = 0.0
    total = 0.0
    for key, prob in slot.score_pmf.items():
        try:
            left, right = key.split("-")
            rounds = int(left) + int(right)
        except ValueError:
            continue
        total += rounds * prob
        total_p += prob
    return total / total_p if total_p > 1e-9 else None


def _top(distribution: dict[str, float], n: int) -> dict[str, float]:
    ranked = sorted(distribution.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    return {key: round(prob, 4) for key, prob in ranked}


def _flag_digest(flag: ContextFlag) -> dict[str, Any]:
    return {
        "type": flag.flag_type.value,
        "team": flag.team.value if flag.team else None,
        "severity": round(flag.severity, 2),
        "confidence": round(flag.confidence, 2),
        "logit_shift_team1": round(flag.logit_shift, 3),
        "veto": flag.veto,
        "rationale": flag.rationale,
    }


def _decision_digest(decision: BetDecision) -> dict[str, Any]:
    priced = decision.priced
    outcome, quote = priced.outcome, priced.quote
    return {
        "market": quote.market_raw_name,
        "selection": quote.selection_raw_name,
        "market_key": outcome.spec_key,
        "selection_key": outcome.selection,
        "odds": str(quote.odds),
        "odds_after_tax": str(priced.effective_odds),
        "model_probability": round(outcome.p_win_given_settled, 4),
        "sigma": round(outcome.sigma, 4),
        "ev_after_tax": round(priced.ev, 4),
        "accepted": decision.accepted,
        "stake": str(decision.stake),
        "reasons": list(decision.reasons),
        "notes": list(decision.notes),
    }


def _decision_sort_key(decision: BetDecision) -> tuple[float, str, str]:
    return (-decision.priced.ev, decision.priced.outcome.spec_key, decision.priced.outcome.selection)


def build_rationale_digest(
    prediction: MatchPrediction,
    decisions: Sequence[BetDecision],
    ctx: MatchContext,
    *,
    max_rejected: int = 25,
) -> dict[str, Any]:
    """Compact, JSON-able description of the prediction and the decisions.

    All accepted decisions are listed. Rejected ones are listed individually only up to
    ``max_rejected`` (highest expected value first); the rest are summarised by rejection reason in
    ``rejected_reason_counts`` and counted in ``rejected_not_listed`` so nothing is silently dropped.
    """
    team1, team2 = team_names(prediction, ctx)
    central = prediction.central
    accepted = sorted((d for d in decisions if d.accepted), key=_decision_sort_key)
    rejected = sorted((d for d in decisions if not d.accepted), key=_decision_sort_key)
    reason_counts: Counter[str] = Counter()
    for decision in rejected:
        for reason in decision.reasons or ["(no reason recorded)"]:
            reason_counts[_reason_family(reason)] += 1
    listed_rejected = rejected[: max(0, max_rejected)]
    sequences = sorted(prediction.veto_distribution, key=lambda s: -s.probability)[:_MAX_SEQUENCES]
    return {
        "match": {
            "match_id": prediction.match_id,
            "team1": team1,
            "team2": team2,
            "format": prediction.format.value,
            "tournament": ctx.tournament_name,
            "stage": ctx.stage,
            "tier": ctx.tier.value,
            "setting": "LAN" if ctx.is_lan else "online",
            "scheduled_at_utc": ctx.scheduled_at.isoformat(timespec="minutes"),
        },
        "model": {
            "version": prediction.model_version,
            "n_simulations": prediction.n_simulations,
            "data_completeness": round(prediction.data_completeness, 3),
            "p_team1_win": round(central.p_team1_win, 4),
            "p_draw": round(central.p_draw, 4),
            "p_team2_win": round(max(0.0, 1.0 - central.p_team1_win - central.p_draw), 4),
            "sigma_team1_win": round(prediction.sigma_team1_win, 4),
            "map_score_probabilities_team1_first": _top(central.map_score_pmf, _MAX_SCORES),
        },
        "veto": {
            "known": prediction.veto_known,
            "most_likely_sequences": [
                {
                    "maps": seq.maps,
                    "picked_by": [p.value if p else "decider" for p in seq.pickers],
                    "probability": round(seq.probability, 4),
                }
                for seq in sequences
            ],
        },
        "map_estimates": {
            name: {
                "p_team1_win": round(est.p_team1_win, 4),
                "sigma": round(est.sigma, 4),
                "picked_by": est.picked_by.value if est.picked_by else None,
                "components": {k: round(v, 4) for k, v in sorted(est.components.items())},
            }
            for name, est in sorted(prediction.map_estimates.items())
        },
        "map_slots": [
            {
                "map_number": slot.map_number,
                "p_played": round(slot.p_played, 4),
                "likely_maps": _top(slot.map_distribution, 3),
                "p_team1_win_if_played": round(slot.p_team1_win, 4),
                "p_overtime": round(slot.p_overtime, 4),
                "expected_total_rounds": (
                    round(rounds, 1) if (rounds := _expected_rounds(slot)) is not None else None
                ),
            }
            for slot in central.slots
        ],
        "context": {
            "flags": [_flag_digest(flag) for flag in prediction.context_flags],
            "logit_shift_applied_team1": round(prediction.logit_shift_applied, 4),
            "veto_flag_present": any(flag.veto for flag in prediction.context_flags),
        },
        "decisions": {
            "n_total": len(decisions),
            "n_accepted": len(accepted),
            "n_rejected": len(rejected),
            "accepted": [_decision_digest(d) for d in accepted],
            "rejected_listed": [_decision_digest(d) for d in listed_rejected],
            "rejected_not_listed": len(rejected) - len(listed_rejected),
            "rejected_reason_counts": dict(sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        },
    }


def _reason_family(reason: str) -> str:
    """Rejection reason code used for counting (``"low_ev: EV +0.0100 < 0.0300"`` -> ``"low_ev"``).

    The decision engine writes reasons as ``"<code>: <detail>"`` (``engine.decision.make_reason``);
    a reason without a colon is used as is. Only for the counts in the digest - the full text of the
    listed decisions is kept.
    """
    family = reason.split(":", 1)[0].strip()
    return (family or reason.strip() or "(no reason recorded)")[:60]


# ---------------------------------------------------------------------------------------------
# Deterministic template
# ---------------------------------------------------------------------------------------------


def _pct(value: float) -> str:
    return f"{100.0 * value:.1f}%"


def _slot_name(slot: TeamSlot | None, team1: str, team2: str) -> str:
    if slot is TeamSlot.TEAM1:
        return team1
    if slot is TeamSlot.TEAM2:
        return team2
    return "match"


def _clean_cell(text: Any) -> str:
    """Make external text safe for a one-line Markdown bullet / table cell."""
    collapsed = " ".join(str(text).split())
    return sanitize_markdown(collapsed).replace("|", "/")


def render_template_rationale(
    prediction: MatchPrediction, decisions: Sequence[BetDecision], ctx: MatchContext
) -> str:
    """Deterministic Markdown summary built only from the prediction and the decisions (no API call)."""
    digest = build_rationale_digest(prediction, decisions, ctx, max_rejected=0)
    team1, team2 = _clean_cell(digest["match"]["team1"]), _clean_cell(digest["match"]["team2"])
    model = digest["model"]
    lines: list[str] = [
        f"## {team1} vs {team2} ({str(digest['match']['format']).upper()})",
        "",
        "*Automatic summary from the model output (no Claude rationale for this analysis).*",
        "",
        "### Summary",
        f"- Match winner: **{team1} {_pct(model['p_team1_win'])}** · {team2} {_pct(model['p_team2_win'])}"
        + (f" · draw {_pct(model['p_draw'])}" if model["p_draw"] > 0 else "")
        + f" (uncertainty ±{100.0 * model['sigma_team1_win']:.1f} pp)",
    ]
    scores = model["map_score_probabilities_team1_first"]
    if scores:
        lines.append("- Map score: " + " · ".join(f"{score} {_pct(p)}" for score, p in scores.items()))
    lines.append(
        f"- Model {_clean_cell(model['version'])}, {model['n_simulations']} simulations, "
        f"data completeness {_pct(model['data_completeness'])}"
    )

    lines += ["", "### Veto"]
    sequences = digest["veto"]["most_likely_sequences"]
    if digest["veto"]["known"]:
        lines.append("- The veto is published; the map order below is final.")
    else:
        lines.append("- The veto is not published yet; map order is the model's expectation.")
    for seq in sequences[:3]:
        lines.append(f"- {' → '.join(_clean_cell(m) for m in seq['maps'])} ({_pct(seq['probability'])})")

    lines += ["", "### Map by map"]
    for slot in digest["map_slots"]:
        maps = ", ".join(f"{_clean_cell(name)} {_pct(p)}" for name, p in slot["likely_maps"].items())
        rounds = slot["expected_total_rounds"]
        lines.append(
            f"- Map {slot['map_number']} (played {_pct(slot['p_played'])}; {maps or 'map unknown'}): "
            f"{team1} wins {_pct(slot['p_team1_win_if_played'])}, overtime {_pct(slot['p_overtime'])}"
            + (f", about {rounds:.1f} rounds" if rounds is not None else "")
        )
    if not digest["map_slots"]:
        lines.append("- No map-level estimates available.")

    lines += ["", "### Context flags"]
    flags = digest["context"]["flags"]
    for flag in flags:
        who = _slot_name(TeamSlot(flag["team"]) if flag["team"] else None, team1, team2)
        lines.append(
            f"- **{flag['type']}** ({who}; confidence {_pct(flag['confidence'])}, "
            f"shift {flag['logit_shift_team1']:+.3f}{', NO-BET VETO' if flag['veto'] else ''}): "
            f"{_clean_cell(flag['rationale'])}"
        )
    if not flags:
        lines.append("- No context flags.")
    applied = digest["context"]["logit_shift_applied_team1"]
    lines.append(f"- Total adjustment applied to {team1}'s map log-odds: {applied:+.3f}")

    dec = digest["decisions"]
    lines += ["", "### Bets"]
    for item in dec["accepted"]:
        lines.append(
            f"- **Bet:** {_clean_cell(item['market'])} – {_clean_cell(item['selection'])} @ {item['odds']} "
            f"(model {_pct(item['model_probability'])} ±{100.0 * item['sigma']:.1f} pp, "
            f"EV after tax {100.0 * item['ev_after_tax']:+.1f}%, stake {item['stake']})"
        )
    if not dec["accepted"]:
        lines.append("- No bet placed on this match.")
    if dec["n_rejected"]:
        reasons = "; ".join(
            f"{_clean_cell(reason)} ×{count}"
            for reason, count in list(dec["rejected_reason_counts"].items())[:6]
        )
        lines.append(f"- {dec['n_rejected']} priced outcomes were not bet. Reasons: {reasons}.")
    elif not dec["n_total"]:
        lines.append("- No priced outcomes were available for this match.")

    lines += ["", "### Risks", f"- {RISK_DISCLAIMER}"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# Post-processing of model output
# ---------------------------------------------------------------------------------------------


def sanitize_markdown(text: str) -> str:
    """Remove raw HTML tags, image embeds and links with an unsafe target; keep everything else.

    The rationale partly derives from untrusted news (via flag texts). The dashboard should render it
    as Markdown without raw HTML anyway; stripping here is defence in depth (no script tags, no
    remote images that would leak that the page was opened, no ``javascript:`` / ``data:`` links).
    Links to ``http(s)://`` addresses, in-page anchors and site-relative paths are kept; any other
    link is reduced to its text.
    """
    without_images = _MD_IMAGE.sub(lambda m: m.group(1), text)
    without_links = _MD_LINK.sub(
        lambda m: m.group(0) if _SAFE_LINK.match(m.group(2)) else m.group(1), without_images
    )
    without_definitions = _MD_LINK_DEFINITION.sub(
        lambda m: m.group(0) if _SAFE_LINK.match(m.group(1)) else "", without_links
    )
    return _HTML_TAG.sub("", without_definitions)


def finalize_rationale(text: str) -> str:
    """Sanitise model-written Markdown and make sure it says that losses are possible."""
    cleaned = sanitize_markdown(text).strip()
    if not cleaned:
        return ""
    if not _LOSS_PATTERN.search(cleaned):
        cleaned = f"{cleaned}\n\n*{RISK_DISCLAIMER}*"
    return cleaned
