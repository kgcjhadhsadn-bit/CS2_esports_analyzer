"""Request / response models of the dashboard API (the JSON contract documented in docs/API.md).

Conventions: datetimes are ISO-8601 UTC (``"2026-11-22T18:00:00Z"``); money is a string with two
decimals (``"12.50"``); odds are strings (``"1.85"``); probabilities, EV, CLV and rates are floats.
Lists are paginated with ``limit`` / ``offset`` and return ``total``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from cs2bot.domain.enums import (
    BetStatus,
    ContextFlagType,
    JobStatus,
    LedgerKind,
    LinkStatus,
    MatchFormat,
    MatchStatus,
    PlayerRole,
    Side,
    TeamSlot,
    Tier,
    VetoAction,
)


class InputModel(BaseModel):
    """Request bodies reject unknown fields (typos become HTTP 422 instead of being ignored)."""

    model_config = ConfigDict(extra="forbid")


AlertLevel = Literal["critical", "warning", "info"]
BankrollMode = Literal["real", "dry_run"]
# Settlement result of a bet. Paper bets keep ``status = "dry_run"`` after settlement, so clients
# should read ``result`` (``None`` = not settled) rather than ``status`` to tell won / lost / void.
BetResult = Literal["won", "lost", "void"]

# --------------------------------------------------------------------------------------------
# shared references
# --------------------------------------------------------------------------------------------


class TeamRef(BaseModel):
    id: int
    name: str
    country: str | None = None
    logo_url: str | None = None
    hltv_rank: int | None = None


class TournamentRef(BaseModel):
    id: int
    name: str
    tier: Tier
    is_lan: bool | None = None


class BookmakerEventRef(BaseModel):
    id: int
    bookmaker: str
    external_id: str
    name: str
    team1_raw: str
    team2_raw: str
    competition_raw: str | None = None
    starts_at: datetime | None = None
    url: str | None = None
    is_live: bool = False
    match_id: int | None = None
    link_status: LinkStatus
    link_confidence: float | None = None
    teams_swapped: bool = False
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


class MatchBrief(BaseModel):
    id: int
    scheduled_at: datetime | None = None
    status: MatchStatus
    format: MatchFormat | None = None
    team1: TeamRef | None = None
    team2: TeamRef | None = None
    tournament: str | None = None


class MatchSummary(BaseModel):
    id: int
    scheduled_at: datetime | None = None
    status: MatchStatus
    format: MatchFormat | None = None
    tier: Tier
    stage: str | None = None
    is_lan: bool | None = None
    in_scope: bool
    data_completeness: float | None = None
    team1: TeamRef | None = None
    team2: TeamRef | None = None
    tournament: TournamentRef | None = None
    team1_score: int | None = None
    team2_score: int | None = None
    winner_team_id: int | None = None
    betclic: BookmakerEventRef | None = None
    accepted_decisions: int = 0
    best_ev: float | None = None
    has_analysis: bool = False


class MatchPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[MatchSummary]


# --------------------------------------------------------------------------------------------
# match detail
# --------------------------------------------------------------------------------------------


class LineupPlayerOut(BaseModel):
    player_id: int
    nickname: str | None = None
    is_standin: bool = False
    role: PlayerRole | None = None


class LineupsOut(BaseModel):
    team1: list[LineupPlayerOut] = Field(default_factory=list)
    team2: list[LineupPlayerOut] = Field(default_factory=list)


class VetoOut(BaseModel):
    step: int
    team_id: int | None = None
    team: TeamSlot | None = None
    action: VetoAction
    map_name: str


class MapOut(BaseModel):
    id: int
    map_number: int
    map_name: str
    status: MatchStatus
    team1_rounds: int | None = None
    team2_rounds: int | None = None
    winner_team_id: int | None = None
    winner: TeamSlot | None = None
    team1_start_side: Side | None = None
    team1_ct_rounds: int | None = None
    team1_t_rounds: int | None = None
    team2_ct_rounds: int | None = None
    team2_t_rounds: int | None = None
    overtime_rounds: int = 0
    picked_by_team_id: int | None = None
    picked_by: TeamSlot | None = None
    is_decider: bool = False
    played_at: datetime | None = None


class ContextFlagOut(BaseModel):
    id: int
    team_id: int | None = None
    team: TeamSlot | None = None
    flag_type: ContextFlagType
    severity: float
    confidence: float
    logit_shift: float
    veto: bool
    rationale: str
    evidence: list[Any] = Field(default_factory=list)
    model: str | None = None
    active: bool
    created_at: datetime
    expires_at: datetime | None = None


class AnalysisOut(BaseModel):
    id: int
    created_at: datetime
    model_version: str
    payload: dict[str, Any]
    rationale: str | None = None
    rationale_model: str | None = None


class DecisionBrief(BaseModel):
    id: int
    accepted: bool
    reasons: list[Any] = Field(default_factory=list)
    decided_at: datetime
    odds: str | None = None
    p_win: float
    sigma: float
    ev: float
    kelly_fraction: float
    proposed_stake: str | None = None


class BetBrief(BaseModel):
    id: int
    status: BetStatus
    result: BetResult | None = None
    dry_run: bool
    stake: str
    requested_odds: str | None = None
    placed_odds: str | None = None
    placed_at: datetime | None = None
    pnl: str | None = None


class MarketRow(BaseModel):
    """One outcome: latest model price joined with the bookmaker price (either side may be missing)."""

    market_type: str
    spec_key: str
    spec: dict[str, Any]
    selection_key: str
    label: str
    # model
    prediction_id: int | None = None
    model_version: str | None = None
    predicted_at: datetime | None = None
    p_win: float | None = None
    p_push: float | None = None
    p_void: float | None = None
    p_lose: float | None = None
    p_win_settled: float | None = None
    sigma: float | None = None
    fair_odds: str | None = None
    breakeven_odds: str | None = None
    # bookmaker
    selection_id: int | None = None
    bookmaker_event_id: int | None = None
    market_raw: str | None = None
    selection_raw: str | None = None
    odds: str | None = None
    effective_odds: str | None = None
    odds_captured_at: datetime | None = None
    odds_age_s: float | None = None
    odds_stale: bool | None = None
    is_available: bool | None = None
    event_is_live: bool | None = None
    # value
    ev: float | None = None
    decision: DecisionBrief | None = None
    bet: BetBrief | None = None


class MatchDetail(BaseModel):
    match: MatchSummary
    lineups: LineupsOut
    vetoes: list[VetoOut]
    maps: list[MapOut]
    context_flags: list[ContextFlagOut]
    analysis: AnalysisOut | None = None
    betclic_events: list[BookmakerEventRef]
    markets: list[MarketRow]
    unmapped_selections: int = 0
    tax_rate: str


class OddsPoint(BaseModel):
    captured_at: datetime
    odds: str
    is_available: bool


class OddsSeries(BaseModel):
    selection_id: int
    bookmaker_event_id: int
    market_raw: str
    selection_raw: str
    spec_key: str | None = None
    selection_key: str | None = None
    label: str | None = None
    current_odds: str | None = None
    points: list[OddsPoint]


class OddsHistoryOut(BaseModel):
    match_id: int
    series: list[OddsSeries]


# --------------------------------------------------------------------------------------------
# bets & decisions
# --------------------------------------------------------------------------------------------


class BetOut(BaseModel):
    id: int
    match_id: int | None = None
    match: MatchBrief | None = None
    decision_id: int | None = None
    selection_id: int | None = None
    bookmaker: str
    external_event_id: str
    market_raw: str
    selection_raw: str
    market_type: str
    spec_key: str
    selection_key: str
    label: str
    requested_odds: str
    min_acceptable_odds: str
    placed_odds: str | None = None
    stake: str
    tax: str
    potential_payout: str | None = None
    status: BetStatus
    result: BetResult | None = None
    is_open: bool = False
    dry_run: bool
    external_bet_id: str | None = None
    has_screenshot: bool = False
    screenshot_url: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    placed_at: datetime | None = None
    settled_at: datetime | None = None
    payout: str | None = None
    pnl: str | None = None
    closing_odds: str | None = None
    clv: float | None = None
    p_win: float | None = None
    ev: float | None = None


class BetsSummary(BaseModel):
    count: int
    stake: str
    pnl: str
    open: int
    won: int
    lost: int
    void: int


class BetPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[BetOut]
    summary: BetsSummary


class DecisionOut(BaseModel):
    id: int
    match_id: int
    match: MatchBrief | None = None
    prediction_id: int | None = None
    selection_id: int | None = None
    market_type: str
    spec_key: str
    selection_key: str
    label: str
    odds: str
    odds_captured_at: datetime | None = None
    decided_at: datetime
    accepted: bool
    reasons: list[Any] = Field(default_factory=list)
    p_win: float
    sigma: float
    ev: float
    kelly_fraction: float
    proposed_stake: str | None = None
    bet_id: int | None = None


class DecisionDetail(DecisionOut):
    settings_snapshot: dict[str, Any] = Field(default_factory=dict)


class DecisionPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[DecisionOut]


class LedgerEntryOut(BaseModel):
    id: int
    ts: datetime
    kind: LedgerKind
    amount: str
    balance_after: str
    bet_id: int | None = None
    note: str | None = None


class BetDetail(BaseModel):
    bet: BetOut
    decision: DecisionDetail | None = None
    ledger: list[LedgerEntryOut]
    analysis_id: int | None = None
    rationale: str | None = None


# --------------------------------------------------------------------------------------------
# bankroll & performance
# --------------------------------------------------------------------------------------------


class LossLimitOut(BaseModel):
    period: Literal["day", "week"]
    period_start: datetime
    start_bankroll: str
    limit_fraction: float
    limit_amount: str
    realized_pnl: str
    open_stakes: str
    worst_case_pnl: str
    remaining: str
    stake_allowance: str
    used_fraction: float
    reached: bool


class ExposureOut(BaseModel):
    match_id: int | None = None
    stake: str
    bets: int


class EquityPoint(BaseModel):
    ts: datetime
    balance: str
    kind: str | None = None
    amount: str | None = None


class PnlPoint(BaseModel):
    ts: datetime
    cumulative_pnl: str


class PeriodPnl(BaseModel):
    period: str
    start: date
    bets: int
    won: int
    lost: int
    void: int
    staked: str
    pnl: str
    yield_rate: float | None = None


class BankrollStats(BaseModel):
    bets: int
    open_bets: int
    settled_bets: int
    won: int
    lost: int
    void: int
    turnover: str
    realized_pnl: str
    capital_base: str
    roi: float | None = None
    yield_rate: float | None = None
    hit_rate: float | None = None
    avg_odds: str | None = None
    avg_clv: float | None = None
    clv_bets: int = 0
    max_drawdown: str
    max_drawdown_pct: float | None = None


class BankrollOut(BaseModel):
    mode: BankrollMode
    currency: str
    as_of: datetime
    balance: str
    balance_source: Literal["ledger", "starting_bankroll", "paper"]
    available_balance: str
    open_exposure: str
    open_bets: int
    bankroll_reference: str
    exposure_by_match: list[ExposureOut]
    daily: LossLimitOut
    weekly: LossLimitOut
    trading_halted: bool
    halt_reason: str | None = None
    stats: BankrollStats
    equity_curve: list[EquityPoint]
    pnl_curve: list[PnlPoint]
    pnl_by_day: list[PeriodPnl]
    pnl_by_week: list[PeriodPnl]
    pnl_by_month: list[PeriodPnl]
    settings: dict[str, Any]


class CalibrationBin(BaseModel):
    lower: float
    upper: float
    count: int
    mean_predicted: float | None = None
    observed_rate: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None


class BrierPoint(BaseModel):
    period: str
    n: int
    brier: float


class CalibrationSet(BaseModel):
    n: int
    base_rate: float | None = None
    mean_predicted: float | None = None
    brier: float | None = None
    log_loss: float | None = None
    ece: float | None = None
    bins: list[CalibrationBin]
    brier_by_month: list[BrierPoint] = Field(default_factory=list)


class CalibrationOut(BaseModel):
    n_bins: int
    bets: CalibrationSet
    predictions: CalibrationSet
    predictions_by_market: dict[str, CalibrationSet]


class MarketPerfRow(BaseModel):
    market_type: str
    bets: int
    open: int
    settled: int
    won: int
    lost: int
    void: int
    hit_rate: float | None = None
    staked: str
    pnl: str
    roi: float | None = None
    avg_odds: str | None = None
    avg_clv: float | None = None
    avg_ev: float | None = None


class MarketPerfOut(BaseModel):
    mode: Literal["real", "dry_run", "all"]
    items: list[MarketPerfRow]
    total: MarketPerfRow


# --------------------------------------------------------------------------------------------
# controls
# --------------------------------------------------------------------------------------------


class KillSwitchIn(InputModel):
    on: bool
    reason: str = Field(default="", max_length=500)


class PauseIn(InputModel):
    reason: str = Field(default="paused from the dashboard", max_length=500)


class ResumeIn(InputModel):
    reason: str = Field(default="", max_length=500)


class SwitchState(BaseModel):
    on: bool
    reason: str | None = None
    updated_at: datetime | None = None
    updated_by: str | None = None


class PauseReasonOut(BaseModel):
    """One structured pause reason (cloud autonomy)."""

    code: str  # loss_limit_daily, uncertain_bet, login_required, 2fa_required, manual, circuit_open ...
    label: str
    message: str
    since: datetime | None = None
    actor: str | None = None
    count: int = 1
    auto_resume: bool = False  # the auto-resume service will lift it once its cause is resolved
    eta: datetime | None = None  # expected auto-resume (loss limits: exact; login: next attempt)
    last_check: str | None = None  # what the last autonomy check found


class AutonomyStatusOut(BaseModel):
    enabled: bool
    auto_login: bool
    last_check_at: datetime | None = None
    last_login_status: str | None = None
    last_login_at: datetime | None = None
    last_login_message: str | None = None
    login_blocked: bool = False  # wrong password / locked account: waiting for the owner


class TelegramStatusOut(BaseModel):
    enabled: bool
    configured: bool  # token and chat id set
    last_scan_at: datetime | None = None
    last_ok_at: datetime | None = None
    last_error: str | None = None
    sent: int | None = None
    last_poll_at: datetime | None = None
    commands_handled: int | None = None


class TradingStateOut(BaseModel):
    halted: bool
    reason: str | None = None
    kill_switch: SwitchState
    paused: SwitchState
    pause_reasons: list[PauseReasonOut] = Field(default_factory=list)
    circuit_open: bool = False
    circuit_codes: list[str] = Field(default_factory=list)
    autonomy: AutonomyStatusOut | None = None
    telegram: TelegramStatusOut | None = None


class ConfigSections(BaseModel):
    decision: dict[str, Any] = Field(default_factory=dict)
    bankroll: dict[str, Any] = Field(default_factory=dict)


class ConfigOut(BaseModel):
    effective: ConfigSections
    defaults: ConfigSections
    overrides: ConfigSections
    updated_at: datetime | None = None
    updated_by: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ConfigIn(InputModel):
    decision: dict[str, Any] | None = None
    bankroll: dict[str, Any] | None = None


class LoginLaunchOut(BaseModel):
    configured: bool
    running: bool
    started: bool = False
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_result: bool | None = None
    last_error: str | None = None


# --------------------------------------------------------------------------------------------
# link review queue
# --------------------------------------------------------------------------------------------


class ReviewCandidate(BaseModel):
    match: MatchBrief
    score: float
    name_score: float
    time_diff_minutes: float | None = None
    teams_swapped: bool
    is_current: bool = False


class ReviewItem(BaseModel):
    event: BookmakerEventRef
    n_markets: int
    current_match: MatchBrief | None = None
    candidates: list[ReviewCandidate]


class ReviewPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[ReviewItem]


class ReviewActionIn(InputModel):
    action: Literal["confirm", "reject", "relink"]
    match_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    teams_swapped: bool | None = None


class ReviewActionOut(BaseModel):
    event: BookmakerEventRef
    action: Literal["confirm", "reject", "relink"]
    previous_match_id: int | None = None
    previous_status: LinkStatus
    previous_teams_swapped: bool
    markets_reset: int = 0
    remapped: bool | None = None
    open_bets: int = 0


# --------------------------------------------------------------------------------------------
# jobs, teams
# --------------------------------------------------------------------------------------------


class JobRunOut(BaseModel):
    id: int
    job_name: str
    status: JobStatus
    started_at: datetime
    finished_at: datetime | None = None
    duration_s: float | None = None
    items: int
    error: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class JobRunPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[JobRunOut]


class TeamOut(BaseModel):
    id: int
    name: str
    slug: str | None = None
    country: str | None = None
    logo_url: str | None = None
    hltv_rank: int | None = None
    hltv_rank_updated_at: datetime | None = None
    pandascore_id: int | None = None
    hltv_id: int | None = None
    aliases: list[str] = Field(default_factory=list)


class TeamMapResult(BaseModel):
    map_number: int
    map_name: str
    team_rounds: int | None = None
    opponent_rounds: int | None = None
    won: bool | None = None


class TeamResult(BaseModel):
    match_id: int
    scheduled_at: datetime | None = None
    opponent: TeamRef | None = None
    tournament: str | None = None
    tier: Tier
    format: MatchFormat | None = None
    team_score: int | None = None
    opponent_score: int | None = None
    result: Literal["W", "L", "D"] | None = None
    maps: list[TeamMapResult] = Field(default_factory=list)


class TeamMapStat(BaseModel):
    map_name: str
    played: int
    won: int
    win_rate: float | None = None
    rounds_for: int
    rounds_against: int
    avg_round_diff: float | None = None
    picked: int
    picked_by_opponent: int
    deciders: int
    bans: int
    opponent_bans: int
    last_played_at: datetime | None = None


class TeamPlayerOut(BaseModel):
    id: int
    nickname: str
    real_name: str | None = None
    country: str | None = None
    primary_role: PlayerRole | None = None
    role_confidence: float | None = None
    hltv_id: int | None = None
    maps: int = 0
    kills: int = 0
    deaths: int = 0
    kd: float | None = None
    kpr: float | None = None
    avg_rating: float | None = None
    avg_adr: float | None = None
    last_played_at: datetime | None = None


class TeamDetail(BaseModel):
    team: TeamOut
    window_days: int
    form: list[Literal["W", "L", "D"]]
    recent_results: list[TeamResult]
    upcoming: list[MatchBrief]
    maps: list[TeamMapStat]
    players: list[TeamPlayerOut]


# --------------------------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------------------------


class HealthAlert(BaseModel):
    level: AlertLevel
    code: str
    message: str


class DbHealth(BaseModel):
    ok: bool
    latency_ms: float | None = None
    error: str | None = None


class JobHealth(BaseModel):
    name: str
    kind: str | None = None
    status: JobStatus
    last_started_at: datetime
    last_finished_at: datetime | None = None
    age_s: float
    duration_s: float | None = None
    items: int
    error: str | None = None
    last_success_at: datetime | None = None
    last_success_age_s: float | None = None
    consecutive_failures: int
    expected_interval_s: float | None = None
    stale: bool
    # from the built-in scheduler (``None`` when the API has no scheduler status)
    scheduled: bool | None = None
    next_due_at: datetime | None = None
    scheduler_failures: int | None = None


class SchedulerJobOut(BaseModel):
    """One job of the built-in scheduler (``cs2bot.flows.scheduler.Scheduler.status``)."""

    name: str
    lane: str | None = None
    enabled: bool = True
    last_run_at: datetime | None = None
    next_due_at: datetime | None = None
    due_in_s: float | None = None  # negative = that many seconds past its due time
    failures: int = 0  # consecutive failed runs (drives the scheduler's back-off)
    running: bool = False
    overdue: bool = False


class SchedulerOut(BaseModel):
    ok: bool  # False: the status provider raised
    jobs: list[SchedulerJobOut] = Field(default_factory=list)


class ExecutorHealthOut(BaseModel):
    source: Literal["provider", "system_state", "none"]
    enabled: bool
    dry_run: bool
    ok: bool | None = None
    logged_in: bool | None = None
    balance: str | None = None
    selectors_ok: dict[str, bool] = Field(default_factory=dict)
    message: str = ""
    checked_at: datetime | None = None
    age_s: float | None = None


class SyncJobFreshness(BaseModel):
    kind: str
    name: str
    last_success_at: datetime | None = None
    age_s: float | None = None


class FreshnessOut(BaseModel):
    last_odds_snapshot_at: datetime | None = None
    last_odds_snapshot_age_s: float | None = None
    last_event_seen_at: datetime | None = None
    last_event_seen_age_s: float | None = None
    last_match_sync_at: datetime | None = None
    last_match_sync_age_s: float | None = None
    last_prediction_at: datetime | None = None
    last_analysis_at: datetime | None = None
    last_decision_at: datetime | None = None
    sync_jobs: list[SyncJobFreshness] = Field(default_factory=list)


class HealthOut(BaseModel):
    status: Literal["ok", "warning", "critical"]
    now: datetime
    version: str
    db: DbHealth
    trading: TradingStateOut | None = None
    dry_run: bool
    executor: ExecutorHealthOut
    jobs: list[JobHealth] = Field(default_factory=list)
    scheduler: SchedulerOut | None = None  # None: no scheduler runs in this process
    freshness: FreshnessOut | None = None
    pending_link_reviews: int | None = None
    alerts: list[HealthAlert] = Field(default_factory=list)


# --------------------------------------------------------------------------------------------
# auth / misc
# --------------------------------------------------------------------------------------------


class LoginIn(InputModel):
    username: str = Field(max_length=256)
    password: SecretStr


class AuthMe(BaseModel):
    auth_enabled: bool
    authenticated: bool
    username: str | None = None


class RootInfo(BaseModel):
    name: str
    version: str
    dashboard: str
    api_docs: str
