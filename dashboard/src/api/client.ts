/**
 * Minimal fetch wrapper for the local API.
 *
 * - every request uses `credentials: "include"` (session cookie when auth is enabled);
 * - every request carries `X-Requested-With: cs2bot` (the API's CSRF check requires it on
 *   POST / PUT / PATCH / DELETE; sending it on GETs too is harmless and matches docs/API.md);
 * - JSON in, JSON out; non-2xx answers become an {@link ApiError};
 * - a 401 notifies the registered unauthorized listeners (the auth provider shows the login page).
 */
import type {
  AuthMe,
  Bankroll,
  BankrollMode,
  BetDetail,
  BetPage,
  Calibration,
  ConfigIn,
  ConfigOut,
  DecisionPage,
  Health,
  JobRunPage,
  LoginIn,
  LoginLaunch,
  MarketPerf,
  MatchDetail,
  MatchPage,
  OddsHistory,
  PerfMode,
  ReviewActionIn,
  ReviewActionOut,
  ReviewPage,
  SwitchState,
  TeamDetail,
  TradingState,
  ValidationErrorItem,
} from "./types";

export const API_BASE = "/api";
export const CSRF_HEADER = "X-Requested-With";
export const CSRF_VALUE = "cs2bot";

export type QueryValue = string | number | boolean | null | undefined | readonly (string | number)[];
export type QueryParams = Record<string, QueryValue>;

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, message: string, detail: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }

  /** FastAPI 422 items (`[]` for other errors). */
  get validationErrors(): ValidationErrorItem[] {
    return Array.isArray(this.detail) ? (this.detail.filter(isValidationItem) as ValidationErrorItem[]) : [];
  }
}

function isValidationItem(value: unknown): boolean {
  return (
    typeof value === "object" &&
    value !== null &&
    Array.isArray((value as { loc?: unknown }).loc) &&
    typeof (value as { msg?: unknown }).msg === "string"
  );
}

type UnauthorizedListener = () => void;
const unauthorizedListeners = new Set<UnauthorizedListener>();

/** Subscribe to 401 answers (returns the unsubscribe function). */
export function onUnauthorized(listener: UnauthorizedListener): () => void {
  unauthorizedListeners.add(listener);
  return () => {
    unauthorizedListeners.delete(listener);
  };
}

/** `?a=1&status=won&status=lost`; null / undefined / "" values and empty lists are skipped. */
export function buildQuery(params?: QueryParams): string {
  if (!params) return "";
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "") continue;
    if (Array.isArray(value)) {
      for (const item of value as readonly (string | number)[]) search.append(key, String(item));
    } else {
      search.append(key, String(value));
    }
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

/** Human readable message from an error body (`{"detail": "..."}` or a 422 list). */
export function errorMessage(status: number, detail: unknown, fallback: string): string {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .filter(isValidationItem)
      .map((item: ValidationErrorItem) => {
        const loc = item.loc.filter((p) => p !== "body").join(".");
        return loc ? `${loc}: ${item.msg}` : item.msg;
      });
    if (parts.length) return parts.join("; ");
  }
  const generic: Record<number, string> = {
    400: "Bad request",
    401: "Not logged in",
    403: "Request refused (CSRF check failed)",
    404: "Not found",
    409: "Conflict",
    422: "Invalid input",
    429: "Too many attempts, try again later",
    501: "Not configured",
    503: "Database unavailable",
  };
  return generic[status] ?? fallback;
}

export interface RequestOptions {
  query?: QueryParams;
  body?: unknown;
  signal?: AbortSignal;
}

export async function apiRequest<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
  const { query, body, signal } = options;
  const headers: Record<string, string> = { Accept: "application/json", [CSRF_HEADER]: CSRF_VALUE };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}${buildQuery(query)}`, {
      method,
      credentials: "include",
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, "Cannot reach the cs2bot API - is `cs2bot up` running?", null);
  }
  const text = await response.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!response.ok) {
    const detail = typeof data === "object" && data !== null && "detail" in data ? (data as { detail: unknown }).detail : data;
    if (response.status === 401) unauthorizedListeners.forEach((listener) => listener());
    throw new ApiError(
      response.status,
      errorMessage(response.status, detail, `HTTP ${response.status}`),
      detail,
    );
  }
  return data as T;
}

const get = <T>(path: string, query?: QueryParams, signal?: AbortSignal) =>
  apiRequest<T>("GET", path, { query, signal });
const post = <T>(path: string, body?: unknown) => apiRequest<T>("POST", path, { body: body ?? {} });
const put = <T>(path: string, body: unknown) => apiRequest<T>("PUT", path, { body });

// ---------------------------------------------------------------------------------------------
// endpoint functions (docs/API.md)
// ---------------------------------------------------------------------------------------------

export interface MatchQuery extends QueryParams {
  status?: readonly string[];
  from?: string;
  to?: string;
  in_scope?: boolean;
  linked?: boolean;
  team_id?: number;
  order?: "asc" | "desc";
  limit?: number;
  offset?: number;
}

export interface BetQuery extends QueryParams {
  status?: readonly string[];
  dry_run?: boolean;
  from?: string;
  to?: string;
  match_id?: number;
  limit?: number;
  offset?: number;
}

export const api = {
  // auth
  me: (signal?: AbortSignal) => get<AuthMe>("/auth/me", undefined, signal),
  login: (body: LoginIn) => post<AuthMe>("/auth/login", body),
  logout: () => post<AuthMe>("/auth/logout"),
  // read
  health: (signal?: AbortSignal) => get<Health>("/health", undefined, signal),
  matches: (query: MatchQuery, signal?: AbortSignal) => get<MatchPage>("/matches", query, signal),
  match: (id: number, signal?: AbortSignal) => get<MatchDetail>(`/matches/${id}`, undefined, signal),
  oddsHistory: (id: number, query: { selection_id?: number; since?: string; max_points?: number } = {}, signal?: AbortSignal) =>
    get<OddsHistory>(`/matches/${id}/odds-history`, query, signal),
  bets: (query: BetQuery, signal?: AbortSignal) => get<BetPage>("/bets", query, signal),
  bet: (id: number, signal?: AbortSignal) => get<BetDetail>(`/bets/${id}`, undefined, signal),
  screenshotUrl: (id: number) => `${API_BASE}/bets/${id}/screenshot`,
  bankroll: (mode: BankrollMode, signal?: AbortSignal) =>
    get<Bankroll>("/bankroll", { dry_run: mode === "dry_run" }, signal),
  calibration: (query: { bins?: number; mode?: PerfMode; from?: string; to?: string }, signal?: AbortSignal) =>
    get<Calibration>("/performance/calibration", query, signal),
  marketPerformance: (query: { mode?: PerfMode; from?: string; to?: string }, signal?: AbortSignal) =>
    get<MarketPerf>("/performance/markets", query, signal),
  decisions: (query: { match_id?: number; accepted?: boolean; limit?: number; offset?: number }, signal?: AbortSignal) =>
    get<DecisionPage>("/decisions", query, signal),
  jobs: (query: { name?: string; status?: readonly string[]; limit?: number; offset?: number }, signal?: AbortSignal) =>
    get<JobRunPage>("/jobs", query, signal),
  team: (id: number, signal?: AbortSignal) => get<TeamDetail>(`/teams/${id}`, undefined, signal),
  reviewLinks: (query: { status?: readonly string[]; limit?: number; offset?: number }, signal?: AbortSignal) =>
    get<ReviewPage>("/review/links", query, signal),
  // controls
  tradingState: (signal?: AbortSignal) => get<TradingState>("/control/state", undefined, signal),
  setKillSwitch: (on: boolean, reason: string) => post<SwitchState>("/control/kill-switch", { on, reason }),
  pause: (reason: string) => post<TradingState>("/control/pause", reason ? { reason } : {}),
  resume: (reason = "") => post<TradingState>("/control/resume", { reason }),
  config: (signal?: AbortSignal) => get<ConfigOut>("/control/config", undefined, signal),
  putConfig: (body: ConfigIn) => put<ConfigOut>("/control/config", body),
  betclicLoginStatus: (signal?: AbortSignal) => get<LoginLaunch>("/control/betclic-login", undefined, signal),
  startBetclicLogin: () => post<LoginLaunch>("/control/betclic-login"),
  reviewAction: (eventId: number, body: ReviewActionIn) => post<ReviewActionOut>(`/review/links/${eventId}`, body),
};
