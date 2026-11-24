/**
 * Field metadata and client-side validation of the decision / bankroll overrides editor.
 * The ranges mirror `src/cs2bot/api/configedit.py` (the server re-validates and answers 422 with
 * one item per problem, which the settings page shows next to the field).
 */
import { MARKET_TYPES } from "../api/types";
import type { MarketThresholds } from "../api/types";

export type Section = "decision" | "bankroll";
export type FieldKind = "float" | "int" | "decimal" | "bool" | "currency";

export interface FieldSpec {
  key: string;
  label: string;
  kind: FieldKind;
  min?: number;
  max?: number;
  minOpen?: boolean;
  maxOpen?: boolean;
  step?: string;
  hint: string;
  /** Show the value as a percentage hint next to the input. */
  percent?: boolean;
}

export const DECISION_FIELDS: FieldSpec[] = [
  { key: "min_probability", label: "Min probability", kind: "float", min: 0, max: 1, minOpen: true, maxOpen: true, step: "0.01", percent: true, hint: "P(win | not refunded) must be at least this" },
  { key: "min_ev", label: "Min EV", kind: "float", min: -1, max: 1, step: "0.005", percent: true, hint: "-1 = odds ignored (confidence only, the owner's rule); 0 = break-even guard after the 12% stake tax" },
  { key: "max_sigma", label: "Max sigma", kind: "float", min: 0, max: 0.5, minOpen: true, step: "0.005", hint: "Maximum model uncertainty of the outcome probability" },
  { key: "min_data_completeness", label: "Min data completeness", kind: "float", min: 0, max: 1, step: "0.01", percent: true, hint: "Share of the inputs the model needs that are available" },
  { key: "min_event_link_confidence", label: "Min Betclic link confidence", kind: "float", min: 0, max: 1, step: "0.01", percent: true, hint: "Auto links below this are never bet" },
  { key: "min_odds", label: "Min odds", kind: "decimal", min: 1, max: 1000, step: "0.01", hint: "Decimal odds, at least 1" },
  { key: "max_odds", label: "Max odds", kind: "decimal", min: 1, max: 1000, minOpen: true, step: "0.01", hint: "Greater than min odds, at most 1000" },
  { key: "min_minutes_before_start", label: "Min minutes before start", kind: "int", min: 0, max: 10080, step: "1", hint: "No bet closer to the start than this" },
  { key: "max_hours_before_start", label: "Max hours before start", kind: "int", min: 0, max: 720, minOpen: true, step: "1", hint: "No bet earlier than this before the start" },
  { key: "prefer_known_veto", label: "Prefer known veto", kind: "bool", hint: "Wait for the published veto for map-level markets when possible" },
  { key: "require_profitable_win", label: "A win must be a win", kind: "bool", hint: "Skip odds below 1.137: after the 12% stake tax such a win pays back less than the stake" },
  { key: "use_confidence_guard", label: "Confidence guard", kind: "bool", hint: "Shrink probabilities above 75% where the backtest shows over-confidence (without history: half of the excess over 75%)" },
  { key: "require_trusted_calibration", label: "Require trusted calibration", kind: "bool", hint: "Bet a market type only when the backtest marks its calibration as trusted" },
];

export const BANKROLL_FIELDS: FieldSpec[] = [
  { key: "starting_bankroll", label: "Starting bankroll", kind: "decimal", min: 0, max: 1_000_000_000, step: "0.01", hint: "Used when the ledger has no deposit yet (PLN)" },
  { key: "currency", label: "Currency", kind: "currency", hint: "3-letter code (PLN)" },
  { key: "kelly_fraction", label: "Kelly fraction", kind: "float", min: 0, max: 1, minOpen: true, step: "0.05", percent: true, hint: "Share of full Kelly that is staked (only with staking mode kelly)" },
  { key: "max_stake_fraction", label: "Max stake per bet", kind: "float", min: 0, max: 1, minOpen: true, step: "0.005", percent: true, hint: "Of the bankroll" },
  { key: "max_match_exposure_fraction", label: "Max exposure per match", kind: "float", min: 0, max: 1, minOpen: true, step: "0.005", percent: true, hint: "All open stakes on one match, of the bankroll" },
  { key: "daily_loss_limit_fraction", label: "Daily loss limit", kind: "float", min: 0, max: 1, minOpen: true, step: "0.01", percent: true, hint: "The bot pauses itself when reached (Warsaw day)" },
  { key: "weekly_loss_limit_fraction", label: "Weekly loss limit", kind: "float", min: 0, max: 1, minOpen: true, step: "0.01", percent: true, hint: "Warsaw week starting Monday" },
  { key: "max_bets_per_day", label: "Max bets per day", kind: "int", min: 0, max: 1000, step: "1", hint: "0 disables betting" },
  { key: "stake_step", label: "Stake step", kind: "decimal", min: 0, max: 1000, minOpen: true, step: "0.01", hint: "Stakes are rounded down to a multiple of this (PLN)" },
];

export const FIELDS: Record<Section, FieldSpec[]> = { decision: DECISION_FIELDS, bankroll: BANKROLL_FIELDS };

export const THRESHOLD_KEYS = ["min_probability", "min_ev", "max_sigma"] as const;
export type ThresholdKey = (typeof THRESHOLD_KEYS)[number];

/** Bettable market types (everything except "other", which is never bet). */
export const BETTABLE_MARKETS = MARKET_TYPES.filter((m) => m !== "other");

/** Form value of a setting (inputs work on strings; booleans stay booleans). */
export type FormValue = string | boolean;

export function toFormValue(value: unknown): FormValue {
  if (typeof value === "boolean") return value;
  if (value === null || value === undefined) return "";
  return String(value);
}

function rangeText(spec: FieldSpec): string {
  const left = spec.minOpen ? "(" : "[";
  const right = spec.maxOpen ? ")" : "]";
  return `${left}${spec.min}, ${spec.max}${right}`;
}

/** Parse + validate one field. Returns the JSON value to send, or an error message. */
export function parseField(spec: FieldSpec, raw: FormValue): { value?: unknown; error?: string } {
  if (spec.kind === "bool") return { value: raw === true };
  const text = typeof raw === "string" ? raw.trim() : "";
  if (spec.kind === "currency") {
    return /^[A-Z]{3}$/.test(text) ? { value: text } : { error: "must be a 3-letter currency code (e.g. PLN)" };
  }
  if (!text) return { error: "required" };
  if (!/^-?\d+(\.\d+)?$/.test(text)) return { error: "must be a number" };
  const n = Number(text);
  if (!Number.isFinite(n)) return { error: "must be a finite number" };
  if (spec.kind === "int" && !Number.isInteger(n)) return { error: "must be a whole number" };
  if (spec.min !== undefined && (spec.minOpen ? n <= spec.min : n < spec.min)) return { error: `must be in ${rangeText(spec)}` };
  if (spec.max !== undefined && (spec.maxOpen ? n >= spec.max : n > spec.max)) return { error: `must be in ${rangeText(spec)}` };
  if (spec.kind === "decimal") return { value: text };
  return { value: n };
}

/** Equality of a parsed value with a default from the API (decimals compare numerically). */
export function sameValue(a: unknown, b: unknown): boolean {
  if (typeof a === "string" && typeof b === "string" && /^-?\d/.test(a) && /^-?\d/.test(b)) return Number(a) === Number(b);
  if (typeof a === "number" || typeof b === "number") return Number(a) === Number(b);
  return JSON.stringify(a) === JSON.stringify(b);
}

export interface MarketOverrideRow {
  market: string;
  min_probability: string;
  min_ev: string;
  max_sigma: string;
}

const THRESHOLD_RANGES: Record<ThresholdKey, FieldSpec> = {
  min_probability: { key: "min_probability", label: "min p", kind: "float", min: 0, max: 1, minOpen: true, maxOpen: true, hint: "" },
  min_ev: { key: "min_ev", label: "min EV", kind: "float", min: -1, max: 1, hint: "" },
  max_sigma: { key: "max_sigma", label: "max σ", kind: "float", min: 0, max: 0.5, minOpen: true, hint: "" },
};

export function overridesToRows(overrides: Record<string, MarketThresholds> | undefined): MarketOverrideRow[] {
  return Object.entries(overrides ?? {}).map(([market, th]) => ({
    market,
    min_probability: th.min_probability != null ? String(th.min_probability) : "",
    min_ev: th.min_ev != null ? String(th.min_ev) : "",
    max_sigma: th.max_sigma != null ? String(th.max_sigma) : "",
  }));
}

/** Rows -> `market_overrides` dict; empty cells mean "use the global threshold". */
export function rowsToOverrides(rows: MarketOverrideRow[]): { value?: Record<string, MarketThresholds>; errors: string[] } {
  const errors: string[] = [];
  const out: Record<string, MarketThresholds> = {};
  for (const row of rows) {
    if (!row.market) {
      errors.push("choose a market for every override row");
      continue;
    }
    if (out[row.market]) {
      errors.push(`${row.market} is listed twice`);
      continue;
    }
    const th: MarketThresholds = {};
    for (const key of THRESHOLD_KEYS) {
      const raw = row[key].trim();
      if (!raw) continue;
      const parsed = parseField(THRESHOLD_RANGES[key], raw);
      if (parsed.error) errors.push(`${row.market}.${key} ${parsed.error}`);
      else th[key] = parsed.value as number;
    }
    if (!Object.keys(th).length) errors.push(`${row.market}: set at least one threshold or remove the row`);
    out[row.market] = th;
  }
  return errors.length ? { errors } : { value: out, errors };
}

export interface SectionResult {
  /** Overrides to send (fields that differ from the defaults). */
  body: Record<string, unknown>;
  /** field key -> error message */
  errors: Record<string, string>;
}

/**
 * Validate a whole section form and build its overrides: every field that differs from the
 * default is sent (PUT replaces the section's overrides, so unchanged overrides must be resent).
 */
export function buildSection(
  section: Section,
  form: Record<string, FormValue>,
  defaults: Record<string, unknown>,
  extra: { marketRows?: MarketOverrideRow[]; disabledMarkets?: string[] } = {},
): SectionResult {
  const errors: Record<string, string> = {};
  const body: Record<string, unknown> = {};
  for (const spec of FIELDS[section]) {
    // a setting the server does not report (an older API) is never sent: it would be rejected as unknown
    if (!(spec.key in defaults)) continue;
    const parsed = parseField(spec, form[spec.key] ?? "");
    if (parsed.error) {
      errors[spec.key] = parsed.error;
      continue;
    }
    if (!sameValue(parsed.value, defaults[spec.key])) body[spec.key] = parsed.value;
  }
  if (section === "decision") {
    const minOdds = Number(form.min_odds);
    const maxOdds = Number(form.max_odds);
    if (!errors.min_odds && !errors.max_odds && Number.isFinite(minOdds) && Number.isFinite(maxOdds) && maxOdds <= minOdds) {
      errors.max_odds = "must be greater than min odds";
    }
    if (extra.marketRows) {
      const res = rowsToOverrides(extra.marketRows);
      if (res.errors.length) errors.market_overrides = res.errors.join("; ");
      else if (!sameValue(res.value ?? {}, defaults.market_overrides ?? {})) body.market_overrides = res.value;
    }
    if (extra.disabledMarkets) {
      const sorted = [...extra.disabledMarkets].sort();
      const defaultList = Array.isArray(defaults.disabled_markets) ? [...(defaults.disabled_markets as string[])].sort() : [];
      if (!sameValue(sorted, defaultList)) body.disabled_markets = sorted;
    }
  }
  return { body, errors };
}

/** Map FastAPI 422 items (`loc = ["body", section, field, ...]`) to field errors of a section. */
export function serverFieldErrors(
  items: { loc: (string | number)[]; msg: string }[],
  section: Section,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const item of items) {
    const loc = item.loc[0] === "body" ? item.loc.slice(1) : item.loc;
    if (loc[0] !== section) continue;
    const field = String(loc[1] ?? "_section");
    out[field] = out[field] ? `${out[field]}; ${item.msg}` : item.msg;
  }
  return out;
}
