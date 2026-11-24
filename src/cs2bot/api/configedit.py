"""Validation of dashboard config overrides (decision + bankroll sections).

The foundation models accept any float, so on top of type validation the API enforces sane ranges
(e.g. a Kelly fraction in ``(0, 1]``, a non-negative minimum EV) and rejects unknown field names
instead of silently ignoring typos. Every problem is returned as a FastAPI-style 422 error item.

Semantics of ``PUT /api/control/config``: a section that is omitted (``null``) keeps its stored
overrides, ``{}`` clears that section, otherwise the given fields replace the section's overrides.
Fields equal to the default are dropped so the stored overrides stay minimal.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from cs2bot.api.formatting import utc
from cs2bot.api.schemas import ConfigIn, ConfigOut, ConfigSections
from cs2bot.config import (
    BankrollSettings,
    DecisionSettings,
    MarketThresholds,
    Settings,
    confidence_tiers_problem,
)
from cs2bot.db import state
from cs2bot.db.models import SystemState
from cs2bot.domain.markets import MarketType

ErrorItem = dict[str, Any]
Check = Callable[[Any], str | None]  # receives the merged DecisionSettings / BankrollSettings

_MARKET_TYPES = {mt.value for mt in MarketType}
_CURRENCY = re.compile(r"^[A-Z]{3}$")


def _between(field: str, lo: float, hi: float, *, lo_open: bool = False, hi_open: bool = False) -> Check:
    def check(model: Any) -> str | None:
        v = float(getattr(model, field))
        too_low = v <= lo if lo_open else v < lo
        too_high = v >= hi if hi_open else v > hi
        if too_low or too_high:
            left = "(" if lo_open else "["
            right = ")" if hi_open else "]"
            return f"must be in {left}{lo:g}, {hi:g}{right}"
        return None

    return check


def _thresholds_error(prefix: str, th: MarketThresholds) -> str | None:
    if th.min_probability is not None and not 0 < th.min_probability < 1:
        return f"{prefix}.min_probability must be in (0, 1)"
    if th.min_ev is not None and not -1 <= th.min_ev <= 1:
        return f"{prefix}.min_ev must be in [-1, 1] (-1 = odds ignored)"
    if th.max_sigma is not None and not 0 < th.max_sigma <= 0.5:
        return f"{prefix}.max_sigma must be in (0, 0.5]"
    return None


def _check_market_overrides(model: DecisionSettings) -> str | None:
    for market, th in model.market_overrides.items():
        if market not in _MARKET_TYPES:
            return f"unknown market type {market!r}"
        problem = _thresholds_error(market, th)
        if problem:
            return problem
    return None


def _check_disabled_markets(model: DecisionSettings) -> str | None:
    unknown = sorted(set(model.disabled_markets) - _MARKET_TYPES)
    return f"unknown market type(s): {', '.join(unknown)}" if unknown else None


def _check_odds_range(model: DecisionSettings) -> str | None:
    if model.min_odds < Decimal("1"):
        return "min_odds must be >= 1"
    if model.max_odds <= model.min_odds or model.max_odds > Decimal("1000"):
        return "max_odds must be greater than min_odds and <= 1000"
    return None


def _check_tiers(model: BankrollSettings) -> str | None:
    """Confidence tiers against the merged section (also when only ``max_stake_fraction`` changes)."""
    return confidence_tiers_problem(model)


def _check_currency(model: BankrollSettings) -> str | None:
    return None if _CURRENCY.match(model.currency) else "must be a 3-letter currency code (e.g. PLN)"


def _check_positive_decimal(field: str, hi: Decimal, *, allow_zero: bool) -> Check:
    def check(model: Any) -> str | None:
        value = Decimal(getattr(model, field))
        if (value < 0 if allow_zero else value <= 0) or value > hi:
            return f"must be {'>= 0' if allow_zero else '> 0'} and <= {hi}"
        return None

    return check


# field -> checks; the odds check runs when either odds field is overridden
_DECISION_CHECKS: dict[str, list[Check]] = {
    "min_probability": [_between("min_probability", 0, 1, lo_open=True, hi_open=True)],
    # -1 switches the EV rule off (confidence only, odds ignored - the owner's rule since 2026-11-24)
    "min_ev": [_between("min_ev", -1, 1)],
    "max_sigma": [_between("max_sigma", 0, 0.5, lo_open=True)],
    "min_data_completeness": [_between("min_data_completeness", 0, 1)],
    "min_event_link_confidence": [_between("min_event_link_confidence", 0, 1)],
    "min_odds": [_check_odds_range],
    "max_odds": [_check_odds_range],
    "min_minutes_before_start": [_between("min_minutes_before_start", 0, 7 * 24 * 60)],
    "max_hours_before_start": [_between("max_hours_before_start", 0, 24 * 30, lo_open=True)],
    "market_overrides": [_check_market_overrides],
    "disabled_markets": [_check_disabled_markets],
}
_BANKROLL_CHECKS: dict[str, list[Check]] = {
    "starting_bankroll": [
        _check_positive_decimal("starting_bankroll", Decimal("1000000000"), allow_zero=True)
    ],
    "currency": [_check_currency],
    "kelly_fraction": [_between("kelly_fraction", 0, 1, lo_open=True)],
    "max_stake_fraction": [_between("max_stake_fraction", 0, 1, lo_open=True), _check_tiers],
    "confidence_tiers": [_check_tiers],
    "staking_mode": [_check_tiers],
    "max_match_exposure_fraction": [_between("max_match_exposure_fraction", 0, 1, lo_open=True)],
    "daily_loss_limit_fraction": [_between("daily_loss_limit_fraction", 0, 1, lo_open=True)],
    "weekly_loss_limit_fraction": [_between("weekly_loss_limit_fraction", 0, 1, lo_open=True)],
    "max_bets_per_day": [_between("max_bets_per_day", 0, 1000)],
    "stake_step": [_check_positive_decimal("stake_step", Decimal("1000"), allow_zero=False)],
}


def _validate_section(
    section: str,
    model_cls: type[DecisionSettings] | type[BankrollSettings],
    defaults: BaseModel,
    raw: Mapping[str, Any],
    checks: Mapping[str, list[Check]],
    errors: list[ErrorItem],
) -> dict[str, Any]:
    unknown = sorted(set(raw) - set(model_cls.model_fields))
    for name in unknown:
        errors.append({"loc": ["body", section, name], "msg": "unknown setting", "type": "extra_forbidden"})
    if unknown:
        return {}
    try:
        merged = model_cls.model_validate({**defaults.model_dump(), **raw})
    except ValidationError as exc:
        for err in exc.errors(include_url=False, include_context=False, include_input=False):
            errors.append({"loc": ["body", section, *err["loc"]], "msg": err["msg"], "type": err["type"]})
        return {}
    seen: set[int] = set()
    for name in raw:
        value = getattr(merged, name)
        if isinstance(value, float) and not math.isfinite(value):
            # Python's JSON parser accepts NaN / Infinity. Every comparison with NaN is false, so a
            # NaN threshold would pass the range checks below AND switch the engine's check off.
            errors.append(
                {"loc": ["body", section, name], "msg": "must be a finite number", "type": "finite_number"}
            )
            continue
        for check in checks.get(name, []):
            if id(check) in seen:
                continue
            seen.add(id(check))
            problem = check(merged)
            if problem:
                errors.append({"loc": ["body", section, name], "msg": problem, "type": "value_error"})
    merged_json = merged.model_dump(mode="json")
    default_json = defaults.model_dump(mode="json")
    return {name: merged_json[name] for name in raw if merged_json[name] != default_json[name]}


def normalize_overrides(body: ConfigIn, settings: Settings, current: Mapping[str, Any]) -> dict[str, Any]:
    """Validated, JSON-safe overrides to store; raises :class:`ConfigValidationError` (422 items)."""
    errors: list[ErrorItem] = []
    result: dict[str, Any] = {}
    sections: tuple[
        tuple[str, type[DecisionSettings] | type[BankrollSettings], BaseModel, dict[str, Any] | None, Any],
        ...,
    ] = (
        ("decision", DecisionSettings, settings.decision, body.decision, _DECISION_CHECKS),
        ("bankroll", BankrollSettings, settings.bankroll, body.bankroll, _BANKROLL_CHECKS),
    )
    for section, model_cls, defaults, raw, checks in sections:
        if raw is None:
            kept = current.get(section)
            if isinstance(kept, dict) and kept:
                result[section] = dict(kept)
            continue
        normalized = _validate_section(section, model_cls, defaults, raw, checks, errors)
        if normalized:
            result[section] = normalized
    if errors:
        raise ConfigValidationError(errors)
    return result


class ConfigValidationError(ValueError):
    def __init__(self, errors: list[ErrorItem]) -> None:
        super().__init__(f"{len(errors)} invalid setting(s)")
        self.errors = errors


def config_view(session: Session, settings: Settings) -> ConfigOut:
    row = session.get(SystemState, state.CONFIG_OVERRIDES)
    overrides = state.get_config_overrides(session)
    warnings: list[str] = []
    try:
        decision = state.effective_decision_settings(session, settings).model_dump(mode="json")
    except (ValidationError, ValueError, TypeError):
        decision = settings.decision.model_dump(mode="json")
        warnings.append("stored decision overrides are invalid and are ignored")
    try:
        bankroll = state.effective_bankroll_settings(session, settings).model_dump(mode="json")
    except (ValidationError, ValueError, TypeError):
        bankroll = settings.bankroll.model_dump(mode="json")
        warnings.append("stored bankroll overrides are invalid and are ignored")
    stored_decision = overrides.get("decision")
    stored_bankroll = overrides.get("bankroll")
    return ConfigOut(
        effective=ConfigSections(decision=decision, bankroll=bankroll),
        defaults=ConfigSections(
            decision=settings.decision.model_dump(mode="json"),
            bankroll=settings.bankroll.model_dump(mode="json"),
        ),
        overrides=ConfigSections(
            decision=dict(stored_decision) if isinstance(stored_decision, dict) else {},
            bankroll=dict(stored_bankroll) if isinstance(stored_bankroll, dict) else {},
        ),
        updated_at=utc(row.updated_at) if row is not None else None,
        updated_by=row.updated_by if row is not None else None,
        warnings=warnings,
    )
