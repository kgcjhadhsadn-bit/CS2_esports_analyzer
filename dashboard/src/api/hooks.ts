/**
 * TanStack Query hooks for every endpoint the dashboard uses. Query keys start with the endpoint
 * name so mutations can invalidate whole groups (e.g. a kill-switch change refreshes "control",
 * "health" and "bankroll").
 */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type { BetQuery, MatchQuery } from "./client";
import type { BankrollMode, ConfigIn, PerfMode, ReviewActionIn } from "./types";
import { toast } from "../components/toast";

export const qk = {
  me: ["auth", "me"] as const,
  health: ["health"] as const,
  control: ["control", "state"] as const,
  config: ["control", "config"] as const,
  betclicLogin: ["control", "betclic-login"] as const,
  matches: (q: MatchQuery) => ["matches", "list", q] as const,
  match: (id: number) => ["matches", "detail", id] as const,
  oddsHistory: (id: number) => ["matches", "odds-history", id] as const,
  bets: (q: BetQuery) => ["bets", "list", q] as const,
  bet: (id: number) => ["bets", "detail", id] as const,
  bankroll: (mode: BankrollMode) => ["bankroll", mode] as const,
  calibration: (mode: PerfMode, bins: number) => ["performance", "calibration", mode, bins] as const,
  marketPerf: (mode: PerfMode) => ["performance", "markets", mode] as const,
  review: (status: readonly string[], offset: number) => ["review", status, offset] as const,
  jobs: (status: readonly string[], name: string, offset: number) => ["jobs", status, name, offset] as const,
  decisions: (matchId: number) => ["decisions", matchId] as const,
};

/** Polling intervals (ms): the dashboard is local, so modest polling is cheap. */
export const POLL = { fast: 15_000, normal: 30_000, slow: 60_000 } as const;

export const useHealth = () =>
  useQuery({ queryKey: qk.health, queryFn: ({ signal }) => api.health(signal), refetchInterval: POLL.fast });

export const useTradingState = () =>
  useQuery({ queryKey: qk.control, queryFn: ({ signal }) => api.tradingState(signal), refetchInterval: POLL.fast });

export const useConfig = () => useQuery({ queryKey: qk.config, queryFn: ({ signal }) => api.config(signal) });

/** Built-in default of `decision.min_data_completeness` (used until the config has loaded). */
export const DEFAULT_MIN_DATA_COMPLETENESS = 0.95;

/**
 * The effective `min_data_completeness` the engine uses (dashboard overrides included), so the
 * data-completeness badges turn green exactly when a match passes the engine's check. Shares the
 * config query (and its cache) with the settings page.
 */
export function useMinDataCompleteness(): number {
  const q = useQuery({
    queryKey: qk.config,
    queryFn: ({ signal }) => api.config(signal),
    select: (data) => data.effective.decision.min_data_completeness,
    staleTime: 60_000,
  });
  return typeof q.data === "number" && Number.isFinite(q.data) ? q.data : DEFAULT_MIN_DATA_COMPLETENESS;
}

export const useMatches = (q: MatchQuery, opts: { poll?: boolean } = {}) =>
  useQuery({
    queryKey: qk.matches(q),
    queryFn: ({ signal }) => api.matches(q, signal),
    placeholderData: keepPreviousData,
    refetchInterval: opts.poll ? POLL.slow : false,
  });

export const useMatch = (id: number) =>
  useQuery({
    queryKey: qk.match(id),
    queryFn: ({ signal }) => api.match(id, signal),
    enabled: Number.isInteger(id) && id > 0,
    refetchInterval: POLL.slow,
  });

export const useOddsHistory = (id: number, enabled = true) =>
  useQuery({
    queryKey: qk.oddsHistory(id),
    queryFn: ({ signal }) => api.oddsHistory(id, { max_points: 2000 }, signal),
    enabled: enabled && Number.isInteger(id) && id > 0,
    // The chart carries the last price forward to "now", so keep it as fresh as the markets table.
    refetchInterval: POLL.slow,
  });

export const useBets = (q: BetQuery, opts: { poll?: boolean } = {}) =>
  useQuery({
    queryKey: qk.bets(q),
    queryFn: ({ signal }) => api.bets(q, signal),
    placeholderData: keepPreviousData,
    refetchInterval: opts.poll ? POLL.normal : false,
  });

export const useBet = (id: number | null) =>
  useQuery({
    queryKey: qk.bet(id ?? 0),
    queryFn: ({ signal }) => api.bet(id ?? 0, signal),
    enabled: id !== null && id > 0,
  });

export const useBankroll = (mode: BankrollMode, opts: { poll?: boolean } = {}) =>
  useQuery({
    queryKey: qk.bankroll(mode),
    queryFn: ({ signal }) => api.bankroll(mode, signal),
    refetchInterval: opts.poll ? POLL.normal : false,
  });

export const useCalibration = (mode: PerfMode, bins = 10) =>
  useQuery({
    queryKey: qk.calibration(mode, bins),
    queryFn: ({ signal }) => api.calibration({ mode, bins }, signal),
  });

export const useMarketPerformance = (mode: PerfMode) =>
  useQuery({ queryKey: qk.marketPerf(mode), queryFn: ({ signal }) => api.marketPerformance({ mode }, signal) });

export const useReviewLinks = (status: readonly string[], offset: number, limit: number) =>
  useQuery({
    queryKey: qk.review(status, offset),
    queryFn: ({ signal }) => api.reviewLinks({ status, offset, limit }, signal),
    placeholderData: keepPreviousData,
  });

export const useJobRuns = (status: readonly string[], name: string, offset: number, limit: number) =>
  useQuery({
    queryKey: qk.jobs(status, name, offset),
    queryFn: ({ signal }) => api.jobs({ status, name: name || undefined, offset, limit }, signal),
    placeholderData: keepPreviousData,
    refetchInterval: POLL.normal,
  });

export const useDecisions = (matchId: number) =>
  useQuery({
    queryKey: qk.decisions(matchId),
    queryFn: ({ signal }) => api.decisions({ match_id: matchId, limit: 200 }, signal),
    enabled: matchId > 0,
  });

/** Betclic login window status; polls every 2 s while a window is open. */
export const useBetclicLoginStatus = (enabled = true) =>
  useQuery({
    queryKey: qk.betclicLogin,
    queryFn: ({ signal }) => api.betclicLoginStatus(signal),
    enabled,
    refetchInterval: (query) => (query.state.data?.running ? 2000 : false),
  });

// ---------------------------------------------------------------------------------------------
// mutations
// ---------------------------------------------------------------------------------------------

function useInvalidate() {
  const qc = useQueryClient();
  return (...groups: string[]) =>
    Promise.all(groups.map((g) => qc.invalidateQueries({ queryKey: [g] }))).then(() => undefined);
}

export function useKillSwitch() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ on, reason }: { on: boolean; reason: string }) => api.setKillSwitch(on, reason),
    onSuccess: (state) => {
      if (state.on) toast.warning("Kill switch is ON: the bot will not place any bet.", "Trading stopped");
      else toast.success("Kill switch is off.");
      return invalidate("control", "health", "bankroll");
    },
  });
}

export function usePause() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (reason: string) => api.pause(reason),
    onSuccess: () => {
      toast.warning("Trading paused until you resume.");
      return invalidate("control", "health", "bankroll");
    },
  });
}

export function useResume() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (reason: string) => api.resume(reason),
    onSuccess: (state) => {
      if (state.kill_switch.on) toast.warning("Pause cleared, but the kill switch is still ON.");
      else toast.success("Trading resumed.");
      return invalidate("control", "health", "bankroll");
    },
  });
}

export function useSaveConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ConfigIn) => api.putConfig(body),
    // 422s are shown next to the fields by the settings page, not as a toast.
    meta: { silent422: true },
    onSuccess: (data) => {
      qc.setQueryData(qk.config, data);
      toast.success("Settings saved.");
      return qc.invalidateQueries({ queryKey: ["bankroll"] });
    },
  });
}

export function useStartBetclicLogin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.startBetclicLogin(),
    onSuccess: (data) => {
      qc.setQueryData(qk.betclicLogin, data);
      toast.info("A Betclic window opened on the PC: log in there (incl. SMS / 2FA).", "Betclic login");
    },
  });
}

export function useReviewAction() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ eventId, body }: { eventId: number; body: ReviewActionIn }) => api.reviewAction(eventId, body),
    onSuccess: (res) => {
      const parts = [`Event ${res.event.external_id}: ${res.action}ed.`];
      if (res.markets_reset > 0) {
        parts.push(
          res.remapped
            ? `${res.markets_reset} market(s) re-mapped.`
            : `${res.markets_reset} market(s) cleared; the odds collector re-maps them on its next pass.`,
        );
      }
      if (res.open_bets > 0) toast.warning(`${res.open_bets} open bet(s) exist on this event - check them.`);
      toast.success(parts.join(" "));
      return invalidate("review", "matches", "health");
    },
  });
}
