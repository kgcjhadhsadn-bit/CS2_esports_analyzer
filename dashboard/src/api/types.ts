/**
 * Response / request types of the local dashboard API (src/cs2bot/api/schemas.py, docs/API.md).
 *
 * Conventions of the API:
 * - datetimes are ISO-8601 UTC strings ("2026-11-20T12:00:00Z"), dates "YYYY-MM-DD";
 * - money is a string with exactly two decimals ("12.50"), odds a string ("1.85");
 * - probabilities, EV, CLV and rates are numbers;
 * - team1 / team2 always mean the match's team1 / team2 (never the bookmaker's display order).
 */

export type Iso = string;
export type Money = string;
export type Odds = string;

// ---------------------------------------------------------------------------------------------
// enums (cs2bot.domain.enums)
// ---------------------------------------------------------------------------------------------

export type Side = "ct" | "t";
export type TeamSlot = "team1" | "team2";
export type MatchFormat = "bo1" | "bo2" | "bo3" | "bo5";
export type MatchStatus = "scheduled" | "live" | "finished" | "canceled" | "postponed" | "unknown";
export type Tier = "s" | "a" | "b" | "c" | "d" | "unranked";
export type VetoAction = "ban" | "pick" | "leftover";
export type PlayerRole =
  | "awper"
  | "entry"
  | "lurker"
  | "support"
  | "anchor"
  | "rotator"
  | "igl"
  | "rifler"
  | "unknown";
export type LinkStatus = "auto" | "confirmed" | "pending_review" | "rejected" | "unlinked";
export type BetStatus =
  | "pending"
  | "placing"
  | "placed"
  | "won"
  | "lost"
  | "void"
  | "failed"
  | "cancelled"
  | "dry_run";
export type BetResult = "won" | "lost" | "void";
export type LedgerKind =
  | "deposit"
  | "withdrawal"
  | "stake"
  | "payout"
  | "refund"
  | "adjustment"
  | "reconciliation";
export type ContextFlagType =
  | "stand_in"
  | "roster_change"
  | "illness"
  | "visa_issue"
  | "travel_fatigue"
  | "bootcamp"
  | "elimination_stakes"
  | "dead_rubber"
  | "internal_conflict"
  | "coach_change"
  | "patch_change"
  | "map_pool_change"
  | "form_slump"
  | "motivation"
  | "other";
export type JobStatus = "running" | "success" | "partial" | "failed";
export type AlertLevel = "critical" | "warning" | "info";
export type BankrollMode = "real" | "dry_run";
export type PerfMode = "real" | "dry_run" | "all";

/** cs2bot.domain.markets.MarketType, in taxonomy order (the API sorts market rows the same way). */
export const MARKET_TYPES = [
  "match_winner",
  "map_winner",
  "correct_map_score",
  "map_handicap",
  "total_maps",
  "both_teams_win_a_map",
  "round_handicap",
  "total_rounds",
  "team_total_rounds",
  "total_rounds_odd_even",
  "map_correct_score",
  "map_overtime",
  "pistol_round_winner",
  "race_to_rounds",
  "half_winner",
  "player_kills",
  "player_kills_h2h",
  "top_fragger",
  "other",
] as const;
export type MarketType = (typeof MARKET_TYPES)[number];

// ---------------------------------------------------------------------------------------------
// shared references
// ---------------------------------------------------------------------------------------------

export interface Page<T> {
  total: number;
  limit: number;
  offset: number;
  items: T[];
}

export interface TeamRef {
  id: number;
  name: string;
  country: string | null;
  logo_url: string | null;
  hltv_rank: number | null;
}

export interface TournamentRef {
  id: number;
  name: string;
  tier: Tier;
  is_lan: boolean | null;
}

export interface BookmakerEventRef {
  id: number;
  bookmaker: string;
  external_id: string;
  name: string;
  team1_raw: string;
  team2_raw: string;
  competition_raw: string | null;
  starts_at: Iso | null;
  url: string | null;
  is_live: boolean;
  match_id: number | null;
  link_status: LinkStatus;
  link_confidence: number | null;
  teams_swapped: boolean;
  first_seen_at: Iso | null;
  last_seen_at: Iso | null;
}

export interface MatchBrief {
  id: number;
  scheduled_at: Iso | null;
  status: MatchStatus;
  format: MatchFormat | null;
  team1: TeamRef | null;
  team2: TeamRef | null;
  tournament: string | null;
}

export interface MatchSummary {
  id: number;
  scheduled_at: Iso | null;
  status: MatchStatus;
  format: MatchFormat | null;
  tier: Tier;
  stage: string | null;
  is_lan: boolean | null;
  in_scope: boolean;
  data_completeness: number | null;
  team1: TeamRef | null;
  team2: TeamRef | null;
  tournament: TournamentRef | null;
  team1_score: number | null;
  team2_score: number | null;
  winner_team_id: number | null;
  betclic: BookmakerEventRef | null;
  accepted_decisions: number;
  best_ev: number | null;
  has_analysis: boolean;
}

export type MatchPage = Page<MatchSummary>;

// ---------------------------------------------------------------------------------------------
// match detail
// ---------------------------------------------------------------------------------------------

export interface LineupPlayer {
  player_id: number;
  nickname: string | null;
  is_standin: boolean;
  role: PlayerRole | null;
}

export interface Lineups {
  team1: LineupPlayer[];
  team2: LineupPlayer[];
}

export interface VetoStepOut {
  step: number;
  team_id: number | null;
  team: TeamSlot | null;
  action: VetoAction;
  map_name: string;
}

export interface MapOut {
  id: number;
  map_number: number;
  map_name: string;
  status: MatchStatus;
  team1_rounds: number | null;
  team2_rounds: number | null;
  winner_team_id: number | null;
  winner: TeamSlot | null;
  team1_start_side: Side | null;
  team1_ct_rounds: number | null;
  team1_t_rounds: number | null;
  team2_ct_rounds: number | null;
  team2_t_rounds: number | null;
  overtime_rounds: number;
  picked_by_team_id: number | null;
  picked_by: TeamSlot | null;
  is_decider: boolean;
  played_at: Iso | null;
}

export interface ContextFlagOut {
  id: number;
  team_id: number | null;
  team: TeamSlot | null;
  flag_type: ContextFlagType;
  severity: number;
  confidence: number;
  /** From team1's perspective. */
  logit_shift: number;
  veto: boolean;
  rationale: string;
  evidence: unknown[];
  model: string | null;
  active: boolean;
  created_at: Iso;
  expires_at: Iso | null;
}

export interface AnalysisOut {
  id: number;
  created_at: Iso;
  model_version: string;
  /** The MatchPrediction JSON (see MatchPredictionPayload); parse it defensively (lib/analysis.ts). */
  payload: Record<string, unknown>;
  rationale: string | null;
  rationale_model: string | null;
}

export interface DecisionBrief {
  id: number;
  accepted: boolean;
  reasons: unknown[];
  decided_at: Iso;
  odds: Odds | null;
  p_win: number;
  sigma: number;
  ev: number;
  kelly_fraction: number;
  proposed_stake: Money | null;
}

export interface BetBrief {
  id: number;
  status: BetStatus;
  result: BetResult | null;
  dry_run: boolean;
  stake: Money;
  requested_odds: Odds | null;
  placed_odds: Odds | null;
  placed_at: Iso | null;
  pnl: Money | null;
}

export interface MarketSpecOut {
  market_type: string;
  map_number: number | null;
  maps_scope: number[] | null;
  line: number | null;
  team: string | null;
  player_id: number | null;
  player_name: string | null;
  opponent_player_id: number | null;
  half: number | null;
  round_number: number | null;
}

/** One outcome: latest model price joined with the Betclic price (either side may be missing). */
export interface MarketRow {
  market_type: string;
  spec_key: string;
  spec: Partial<MarketSpecOut> & Record<string, unknown>;
  selection_key: string;
  label: string;
  prediction_id: number | null;
  model_version: string | null;
  predicted_at: Iso | null;
  /** Unconditional probabilities: p_win + p_push + p_void + p_lose = 1. */
  p_win: number | null;
  p_push: number | null;
  p_void: number | null;
  p_lose: number | null;
  /** P(win | not refunded): what the engine compares with min_probability. */
  p_win_settled: number | null;
  sigma: number | null;
  fair_odds: Odds | null;
  breakeven_odds: Odds | null;
  selection_id: number | null;
  bookmaker_event_id: number | null;
  market_raw: string | null;
  selection_raw: string | null;
  odds: Odds | null;
  effective_odds: Odds | null;
  odds_captured_at: Iso | null;
  odds_age_s: number | null;
  odds_stale: boolean | null;
  is_available: boolean | null;
  event_is_live: boolean | null;
  ev: number | null;
  decision: DecisionBrief | null;
  bet: BetBrief | null;
}

export interface MatchDetail {
  match: MatchSummary;
  lineups: Lineups;
  vetoes: VetoStepOut[];
  maps: MapOut[];
  context_flags: ContextFlagOut[];
  analysis: AnalysisOut | null;
  betclic_events: BookmakerEventRef[];
  markets: MarketRow[];
  unmapped_selections: number;
  tax_rate: string;
}

export interface OddsPoint {
  captured_at: Iso;
  odds: Odds;
  is_available: boolean;
}

export interface OddsSeries {
  selection_id: number;
  bookmaker_event_id: number;
  market_raw: string;
  selection_raw: string;
  spec_key: string | null;
  selection_key: string | null;
  label: string | null;
  current_odds: Odds | null;
  points: OddsPoint[];
}

export interface OddsHistory {
  match_id: number;
  series: OddsSeries[];
}

// ---------------------------------------------------------------------------------------------
// analysis payload (cs2bot.domain.predictions.MatchPrediction) - the API passes it through as JSON
// ---------------------------------------------------------------------------------------------

export interface PlayerProjection {
  player_id: number;
  nickname?: string | null;
  team: TeamSlot;
  /** "17" -> p */
  kills_pmf: Record<string, number>;
  kills_mean: number;
  kills_var: number;
  deaths_mean?: number | null;
}

/** All distributions are conditional on the map being played; p_played is separate. */
export interface MapSlotPrediction {
  map_number: number;
  p_played: number;
  map_distribution: Record<string, number>;
  p_team1_win: number;
  /** "13-9" (team1 rounds first) -> p */
  score_pmf: Record<string, number>;
  p_overtime: number;
  /** "1" / "13" -> P(team1 wins that pistol round) */
  p_team1_pistol?: Record<string, number>;
  p_team1_half?: Record<string, number>;
  p_half_draw?: Record<string, number>;
  p_team1_race_to?: Record<string, number>;
  p_neither_race_to?: Record<string, number>;
  players?: Record<string, PlayerProjection>;
}

export interface PredictionCore {
  p_team1_win: number;
  p_draw?: number;
  /** "2-1" (team1 maps first) -> p */
  map_score_pmf: Record<string, number>;
  slots: MapSlotPrediction[];
  /** scope ("map:1", "maps:1,2", "match") -> player id -> projection */
  player_kills_by_scope?: Record<string, Record<string, PlayerProjection>>;
  top_fragger?: Record<string, Record<string, number>>;
}

export interface MapWinEstimate {
  map_name: string;
  p_team1_win: number;
  sigma: number;
  picked_by?: TeamSlot | null;
  components?: Record<string, number>;
}

export interface MapSequenceProbability {
  maps: string[];
  pickers: (TeamSlot | null)[];
  probability: number;
}

export interface MatchPredictionPayload {
  match_id: number;
  created_at?: Iso;
  model_version?: string;
  format: MatchFormat;
  team1_id: number;
  team2_id: number;
  central: PredictionCore;
  low?: PredictionCore | null;
  high?: PredictionCore | null;
  sigma_team1_win: number;
  map_estimates?: Record<string, MapWinEstimate>;
  veto_distribution?: MapSequenceProbability[];
  veto_known?: boolean;
  logit_shift_applied?: number;
  data_completeness?: number;
  n_simulations?: number;
}

// ---------------------------------------------------------------------------------------------
// bets & decisions
// ---------------------------------------------------------------------------------------------

export interface BetOut {
  id: number;
  match_id: number | null;
  match: MatchBrief | null;
  decision_id: number | null;
  selection_id: number | null;
  bookmaker: string;
  external_event_id: string;
  market_raw: string;
  selection_raw: string;
  market_type: string;
  spec_key: string;
  selection_key: string;
  label: string;
  requested_odds: Odds;
  min_acceptable_odds: Odds;
  placed_odds: Odds | null;
  stake: Money;
  tax: Money;
  potential_payout: Money | null;
  /** Stored status; paper bets keep "dry_run" after settlement - read `result` instead. */
  status: BetStatus;
  result: BetResult | null;
  is_open: boolean;
  dry_run: boolean;
  external_bet_id: string | null;
  has_screenshot: boolean;
  screenshot_url: string | null;
  error: string | null;
  created_at: Iso | null;
  placed_at: Iso | null;
  settled_at: Iso | null;
  payout: Money | null;
  pnl: Money | null;
  closing_odds: Odds | null;
  clv: number | null;
  p_win: number | null;
  ev: number | null;
}

export interface BetsSummary {
  count: number;
  stake: Money;
  pnl: Money;
  open: number;
  won: number;
  lost: number;
  void: number;
}

export interface BetPage extends Page<BetOut> {
  summary: BetsSummary;
}

export interface DecisionOut {
  id: number;
  match_id: number;
  match: MatchBrief | null;
  prediction_id: number | null;
  selection_id: number | null;
  market_type: string;
  spec_key: string;
  selection_key: string;
  label: string;
  odds: Odds;
  odds_captured_at: Iso | null;
  decided_at: Iso;
  accepted: boolean;
  reasons: unknown[];
  p_win: number;
  sigma: number;
  ev: number;
  kelly_fraction: number;
  proposed_stake: Money | null;
  bet_id: number | null;
}

export interface DecisionDetail extends DecisionOut {
  settings_snapshot: Record<string, unknown>;
}

export type DecisionPage = Page<DecisionOut>;

export interface LedgerEntry {
  id: number;
  ts: Iso;
  kind: LedgerKind;
  amount: Money;
  balance_after: Money;
  bet_id: number | null;
  note: string | null;
}

export interface BetDetail {
  bet: BetOut;
  decision: DecisionDetail | null;
  ledger: LedgerEntry[];
  analysis_id: number | null;
  rationale: string | null;
}

// ---------------------------------------------------------------------------------------------
// bankroll & performance
// ---------------------------------------------------------------------------------------------

export interface LossLimit {
  period: "day" | "week";
  period_start: Iso;
  start_bankroll: Money;
  limit_fraction: number;
  limit_amount: Money;
  realized_pnl: Money;
  open_stakes: Money;
  worst_case_pnl: Money;
  remaining: Money;
  stake_allowance: Money;
  used_fraction: number;
  reached: boolean;
}

export interface Exposure {
  match_id: number | null;
  stake: Money;
  bets: number;
}

export interface EquityPoint {
  ts: Iso;
  balance: Money;
  kind: string | null;
  amount: Money | null;
}

export interface PnlPoint {
  ts: Iso;
  cumulative_pnl: Money;
}

export interface PeriodPnl {
  period: string;
  start: string;
  bets: number;
  won: number;
  lost: number;
  void: number;
  staked: Money;
  pnl: Money;
  yield_rate: number | null;
}

export interface BankrollStats {
  bets: number;
  open_bets: number;
  settled_bets: number;
  won: number;
  lost: number;
  void: number;
  turnover: Money;
  realized_pnl: Money;
  capital_base: Money;
  roi: number | null;
  yield_rate: number | null;
  hit_rate: number | null;
  avg_odds: Odds | null;
  avg_clv: number | null;
  clv_bets: number;
  max_drawdown: Money;
  max_drawdown_pct: number | null;
}

export interface Bankroll {
  mode: BankrollMode;
  currency: string;
  as_of: Iso;
  balance: Money;
  balance_source: "ledger" | "starting_bankroll" | "paper";
  available_balance: Money;
  open_exposure: Money;
  open_bets: number;
  bankroll_reference: Money;
  exposure_by_match: Exposure[];
  daily: LossLimit;
  weekly: LossLimit;
  trading_halted: boolean;
  halt_reason: string | null;
  stats: BankrollStats;
  equity_curve: EquityPoint[];
  pnl_curve: PnlPoint[];
  pnl_by_day: PeriodPnl[];
  pnl_by_week: PeriodPnl[];
  pnl_by_month: PeriodPnl[];
  settings: Record<string, unknown>;
}

export interface CalibrationBin {
  lower: number;
  upper: number;
  count: number;
  mean_predicted: number | null;
  observed_rate: number | null;
  ci_low: number | null;
  ci_high: number | null;
}

export interface BrierPoint {
  period: string;
  n: number;
  brier: number;
}

export interface CalibrationSet {
  n: number;
  base_rate: number | null;
  mean_predicted: number | null;
  brier: number | null;
  log_loss: number | null;
  ece: number | null;
  bins: CalibrationBin[];
  brier_by_month: BrierPoint[];
}

export interface Calibration {
  n_bins: number;
  bets: CalibrationSet;
  predictions: CalibrationSet;
  predictions_by_market: Record<string, CalibrationSet>;
}

export interface MarketPerfRow {
  market_type: string;
  bets: number;
  open: number;
  settled: number;
  won: number;
  lost: number;
  void: number;
  hit_rate: number | null;
  staked: Money;
  pnl: Money;
  roi: number | null;
  avg_odds: Odds | null;
  avg_clv: number | null;
  avg_ev: number | null;
}

export interface MarketPerf {
  mode: PerfMode;
  items: MarketPerfRow[];
  total: MarketPerfRow;
}

// ---------------------------------------------------------------------------------------------
// controls
// ---------------------------------------------------------------------------------------------

export interface SwitchState {
  on: boolean;
  reason: string | null;
  updated_at: Iso | null;
  updated_by: string | null;
}

export interface TradingState {
  halted: boolean;
  reason: string | null;
  kill_switch: SwitchState;
  paused: SwitchState;
}

export interface MarketThresholds {
  min_probability?: number | null;
  min_ev?: number | null;
  max_sigma?: number | null;
}

/** cs2bot.config.DecisionSettings in JSON mode (Decimals are strings). */
export interface DecisionConfig {
  min_probability: number;
  min_ev: number;
  max_sigma: number;
  min_data_completeness: number;
  min_event_link_confidence: number;
  min_odds: string;
  max_odds: string;
  min_minutes_before_start: number;
  max_hours_before_start: number;
  prefer_known_veto: boolean;
  market_overrides: Record<string, MarketThresholds>;
  disabled_markets: string[];
}

/** cs2bot.config.BankrollSettings in JSON mode (Decimals are strings). */
export interface BankrollConfig {
  starting_bankroll: string;
  currency: string;
  kelly_fraction: number;
  max_stake_fraction: number;
  max_match_exposure_fraction: number;
  daily_loss_limit_fraction: number;
  weekly_loss_limit_fraction: number;
  max_bets_per_day: number;
  stake_step: string;
}

export interface ConfigSections {
  decision: Partial<DecisionConfig> & Record<string, unknown>;
  bankroll: Partial<BankrollConfig> & Record<string, unknown>;
}

export interface ConfigOut {
  effective: ConfigSections;
  defaults: ConfigSections;
  overrides: ConfigSections;
  updated_at: Iso | null;
  updated_by: string | null;
  warnings: string[];
}

/**
 * PUT /api/control/config body. A section that is omitted / null keeps its stored overrides,
 * `{}` clears them, fields replace that section's overrides.
 */
export interface ConfigIn {
  decision?: Record<string, unknown> | null;
  bankroll?: Record<string, unknown> | null;
}

export interface LoginLaunch {
  configured: boolean;
  running: boolean;
  started: boolean;
  last_started_at: Iso | null;
  last_finished_at: Iso | null;
  last_result: boolean | null;
  last_error: string | null;
}

// ---------------------------------------------------------------------------------------------
// link review queue
// ---------------------------------------------------------------------------------------------

export interface ReviewCandidate {
  match: MatchBrief;
  score: number;
  name_score: number;
  time_diff_minutes: number | null;
  /** true: Betclic lists our team2 first. */
  teams_swapped: boolean;
  is_current: boolean;
}

export interface ReviewItem {
  event: BookmakerEventRef;
  n_markets: number;
  current_match: MatchBrief | null;
  candidates: ReviewCandidate[];
}

export type ReviewPage = Page<ReviewItem>;

export type ReviewAction = "confirm" | "reject" | "relink";

export interface ReviewActionIn {
  action: ReviewAction;
  match_id?: number | null;
  teams_swapped?: boolean | null;
}

export interface ReviewActionOut {
  event: BookmakerEventRef;
  action: ReviewAction;
  previous_match_id: number | null;
  previous_status: LinkStatus;
  previous_teams_swapped: boolean;
  markets_reset: number;
  remapped: boolean | null;
  open_bets: number;
}

// ---------------------------------------------------------------------------------------------
// jobs, teams
// ---------------------------------------------------------------------------------------------

export interface JobRun {
  id: number;
  job_name: string;
  status: JobStatus;
  started_at: Iso;
  finished_at: Iso | null;
  duration_s: number | null;
  items: number;
  error: string | null;
  details: Record<string, unknown>;
}

export type JobRunPage = Page<JobRun>;

export interface TeamOut {
  id: number;
  name: string;
  slug: string | null;
  country: string | null;
  logo_url: string | null;
  hltv_rank: number | null;
  hltv_rank_updated_at: Iso | null;
  pandascore_id: number | null;
  hltv_id: number | null;
  aliases: string[];
}

export interface TeamMapResult {
  map_number: number;
  map_name: string;
  team_rounds: number | null;
  opponent_rounds: number | null;
  won: boolean | null;
}

export interface TeamResult {
  match_id: number;
  scheduled_at: Iso | null;
  opponent: TeamRef | null;
  tournament: string | null;
  tier: Tier;
  format: MatchFormat | null;
  team_score: number | null;
  opponent_score: number | null;
  result: "W" | "L" | "D" | null;
  maps: TeamMapResult[];
}

export interface TeamMapStat {
  map_name: string;
  played: number;
  won: number;
  win_rate: number | null;
  rounds_for: number;
  rounds_against: number;
  avg_round_diff: number | null;
  picked: number;
  picked_by_opponent: number;
  deciders: number;
  bans: number;
  opponent_bans: number;
  last_played_at: Iso | null;
}

export interface TeamPlayer {
  id: number;
  nickname: string;
  real_name: string | null;
  country: string | null;
  primary_role: PlayerRole | null;
  role_confidence: number | null;
  hltv_id: number | null;
  maps: number;
  kills: number;
  deaths: number;
  kd: number | null;
  kpr: number | null;
  avg_rating: number | null;
  avg_adr: number | null;
  last_played_at: Iso | null;
}

export interface TeamDetail {
  team: TeamOut;
  window_days: number;
  form: ("W" | "L" | "D")[];
  recent_results: TeamResult[];
  upcoming: MatchBrief[];
  maps: TeamMapStat[];
  players: TeamPlayer[];
}

// ---------------------------------------------------------------------------------------------
// health
// ---------------------------------------------------------------------------------------------

export interface HealthAlert {
  level: AlertLevel;
  code: string;
  message: string;
}

export interface DbHealth {
  ok: boolean;
  latency_ms: number | null;
  error: string | null;
}

export interface JobHealth {
  name: string;
  kind: string | null;
  status: JobStatus;
  last_started_at: Iso;
  last_finished_at: Iso | null;
  age_s: number;
  duration_s: number | null;
  items: number;
  error: string | null;
  last_success_at: Iso | null;
  last_success_age_s: number | null;
  consecutive_failures: number;
  expected_interval_s: number | null;
  stale: boolean;
  scheduled: boolean | null;
  next_due_at: Iso | null;
  scheduler_failures: number | null;
}

export interface SchedulerJob {
  name: string;
  lane: string | null;
  enabled: boolean;
  last_run_at: Iso | null;
  next_due_at: Iso | null;
  /** Negative = that many seconds past its due time. */
  due_in_s: number | null;
  failures: number;
  running: boolean;
  overdue: boolean;
}

export interface SchedulerStatus {
  ok: boolean;
  jobs: SchedulerJob[];
}

export interface ExecutorHealthOut {
  source: "provider" | "system_state" | "none";
  enabled: boolean;
  dry_run: boolean;
  ok: boolean | null;
  logged_in: boolean | null;
  balance: Money | null;
  selectors_ok: Record<string, boolean>;
  message: string;
  checked_at: Iso | null;
  age_s: number | null;
}

export interface SyncJobFreshness {
  kind: string;
  name: string;
  last_success_at: Iso | null;
  age_s: number | null;
}

export interface Freshness {
  last_odds_snapshot_at: Iso | null;
  last_odds_snapshot_age_s: number | null;
  last_event_seen_at: Iso | null;
  last_event_seen_age_s: number | null;
  last_match_sync_at: Iso | null;
  last_match_sync_age_s: number | null;
  last_prediction_at: Iso | null;
  last_analysis_at: Iso | null;
  last_decision_at: Iso | null;
  sync_jobs: SyncJobFreshness[];
}

export interface Health {
  status: "ok" | "warning" | "critical";
  now: Iso;
  version: string;
  db: DbHealth;
  trading: TradingState | null;
  dry_run: boolean;
  executor: ExecutorHealthOut;
  jobs: JobHealth[];
  scheduler: SchedulerStatus | null;
  freshness: Freshness | null;
  pending_link_reviews: number | null;
  alerts: HealthAlert[];
}

// ---------------------------------------------------------------------------------------------
// auth
// ---------------------------------------------------------------------------------------------

export interface AuthMe {
  auth_enabled: boolean;
  authenticated: boolean;
  username: string | null;
}

export interface LoginIn {
  username: string;
  password: string;
}

/** FastAPI validation error item (422). */
export interface ValidationErrorItem {
  loc: (string | number)[];
  msg: string;
  type: string;
}
