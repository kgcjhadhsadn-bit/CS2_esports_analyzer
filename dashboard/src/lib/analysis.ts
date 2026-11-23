/**
 * Chart-ready views of the match analysis payload (`MatchDetail.analysis.payload`, the
 * `cs2bot.domain.predictions.MatchPrediction` JSON).
 *
 * The payload is typed `Record<string, unknown>` by the API and may come from an older model
 * version, so everything here is parsed defensively: malformed entries are skipped, never thrown.
 *
 * Conventions (cs2bot/domain/predictions.py): score keys are "13-9" / "2-1" with team1 first;
 * every distribution inside a map slot is conditional on that map being played (`p_played` is
 * separate); `low` / `high` are the same prediction with team1's strength shifted by -1 / +1 sigma.
 */
import type { TeamSlot } from "../api/types";

export function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

export function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function asSlot(value: unknown): TeamSlot | null {
  return value === "team1" || value === "team2" ? value : null;
}

/** `{key: number}` keeping only finite numbers. */
export function numRecord(value: unknown): Record<string, number> {
  const rec = asRecord(value);
  const out: Record<string, number> = {};
  if (!rec) return out;
  for (const [k, v] of Object.entries(rec)) {
    const n = asNumber(v);
    if (n !== null) out[k] = n;
  }
  return out;
}

/** "13-9" -> [13, 9]; null when malformed. */
export function parseScore(key: string): [number, number] | null {
  const m = /^\s*(\d+)\s*-\s*(\d+)\s*$/.exec(key);
  return m ? [Number(m[1]), Number(m[2])] : null;
}

export interface Bar {
  x: number;
  label: string;
  p: number;
}

export interface PmfStats {
  mean: number | null;
  sd: number | null;
  total: number;
}

export function pmfStats(bars: readonly { x: number; p: number }[]): PmfStats {
  const total = bars.reduce((s, b) => s + b.p, 0);
  if (total <= 0) return { mean: null, sd: null, total };
  const mean = bars.reduce((s, b) => s + b.x * b.p, 0) / total;
  const variance = bars.reduce((s, b) => s + (b.x - mean) ** 2 * b.p, 0) / total;
  return { mean, sd: Math.sqrt(Math.max(0, variance)), total };
}

/** Integer-keyed pmf ("17" -> p) as sorted bars. */
export function pmfBars(pmf: Record<string, number>): Bar[] {
  return Object.entries(pmf)
    .map(([k, p]) => ({ x: Number(k), label: k, p }))
    .filter((b) => Number.isFinite(b.x) && b.p >= 0)
    .sort((a, b) => a.x - b.x);
}

function groupScores(scorePmf: Record<string, number>, fn: (a: number, b: number) => number): Bar[] {
  const acc = new Map<number, number>();
  for (const [key, p] of Object.entries(scorePmf)) {
    const score = parseScore(key);
    if (!score || p < 0) continue;
    const x = fn(score[0], score[1]);
    acc.set(x, (acc.get(x) ?? 0) + p);
  }
  return [...acc.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([x, p]) => ({ x, label: String(x), p }));
}

/** Distribution of total rounds (team1 + team2) from a joint score pmf. */
export function roundTotalBars(scorePmf: Record<string, number>): Bar[] {
  return groupScores(scorePmf, (a, b) => a + b);
}

/** Distribution of the round margin team1 - team2 (positive = team1 wins by that many). */
export function roundMarginBars(scorePmf: Record<string, number>): Bar[] {
  return groupScores(scorePmf, (a, b) => a - b).map((b) => ({ ...b, label: b.x > 0 ? `+${b.x}` : String(b.x) }));
}

/** P(X > line) for a bar distribution (normalized by the total mass). */
export function probOver(bars: readonly Bar[], line: number): number | null {
  const total = bars.reduce((s, b) => s + b.p, 0);
  if (total <= 0) return null;
  return bars.filter((b) => b.x > line).reduce((s, b) => s + b.p, 0) / total;
}

export interface PlayerView {
  playerId: number;
  nickname: string;
  team: TeamSlot | null;
  mean: number;
  sd: number;
  deathsMean: number | null;
  bars: Bar[];
}

function parsePlayer(key: string, value: unknown): PlayerView | null {
  const rec = asRecord(value);
  if (!rec) return null;
  const mean = asNumber(rec.kills_mean);
  if (mean === null) return null;
  const variance = asNumber(rec.kills_var);
  const id = asNumber(rec.player_id) ?? Number(key);
  return {
    playerId: Number.isFinite(id) ? id : -1,
    nickname: asString(rec.nickname) ?? `#${key}`,
    team: asSlot(rec.team),
    mean,
    sd: variance !== null && variance >= 0 ? Math.sqrt(variance) : 0,
    deathsMean: asNumber(rec.deaths_mean),
    bars: pmfBars(numRecord(rec.kills_pmf)),
  };
}

function parsePlayers(value: unknown): PlayerView[] {
  const rec = asRecord(value);
  if (!rec) return [];
  const out: PlayerView[] = [];
  for (const [k, v] of Object.entries(rec)) {
    const p = parsePlayer(k, v);
    if (p) out.push(p);
  }
  return out.sort((a, b) => (a.team ?? "z").localeCompare(b.team ?? "z") || b.mean - a.mean);
}

export interface SlotView {
  mapNumber: number;
  pPlayed: number | null;
  /** map name -> P(slot is that map | played), sorted by probability desc. */
  mapDistribution: { map: string; p: number }[];
  pTeam1Win: number | null;
  pTeam1WinLow: number | null;
  pTeam1WinHigh: number | null;
  scorePmf: Record<string, number>;
  pOvertime: number | null;
  pistol: { round: string; pTeam1: number }[];
  halves: { half: string; pTeam1: number; pDraw: number | null }[];
  players: PlayerView[];
}

export interface MapEstimateView {
  key: string;
  map: string;
  pTeam1Win: number;
  sigma: number | null;
  pickedBy: TeamSlot | null;
}

export interface VetoSequenceView {
  maps: string[];
  pickers: (TeamSlot | null)[];
  probability: number;
}

export interface AnalysisView {
  format: string | null;
  pTeam1Win: number | null;
  pTeam1WinLow: number | null;
  pTeam1WinHigh: number | null;
  sigmaTeam1Win: number | null;
  pDraw: number | null;
  mapScore: { score: string; p: number }[];
  slots: SlotView[];
  mapEstimates: MapEstimateView[];
  vetoKnown: boolean;
  vetoSequences: VetoSequenceView[];
  /** scope key ("match", "map:1", "maps:1,2") -> players */
  playerScopes: Record<string, PlayerView[]>;
  dataCompleteness: number | null;
  nSimulations: number | null;
  logitShiftApplied: number | null;
  modelVersion: string | null;
}

function coreSlots(core: Record<string, unknown> | null): Record<string, unknown>[] {
  const slots = core?.slots;
  return Array.isArray(slots) ? (slots.map(asRecord).filter(Boolean) as Record<string, unknown>[]) : [];
}

function slotByNumber(core: Record<string, unknown> | null, mapNumber: number): Record<string, unknown> | null {
  return coreSlots(core).find((s) => asNumber(s.map_number) === mapNumber) ?? null;
}

/** Parse the payload; returns null when it has no usable `central` core. */
export function parseAnalysis(payload: unknown): AnalysisView | null {
  const root = asRecord(payload);
  const central = asRecord(root?.central);
  if (!root || !central) return null;
  const low = asRecord(root.low);
  const high = asRecord(root.high);

  const slots: SlotView[] = [];
  coreSlots(central).forEach((slot, idx) => {
    const mapNumber = asNumber(slot.map_number) ?? idx + 1;
    const pistolRec = numRecord(slot.p_team1_pistol);
    const halfRec = numRecord(slot.p_team1_half);
    const halfDraw = numRecord(slot.p_half_draw);
    slots.push({
      mapNumber,
      pPlayed: asNumber(slot.p_played),
      mapDistribution: Object.entries(numRecord(slot.map_distribution))
        .map(([map, p]) => ({ map, p }))
        .sort((a, b) => b.p - a.p),
      pTeam1Win: asNumber(slot.p_team1_win),
      pTeam1WinLow: asNumber(slotByNumber(low, mapNumber)?.p_team1_win),
      pTeam1WinHigh: asNumber(slotByNumber(high, mapNumber)?.p_team1_win),
      scorePmf: numRecord(slot.score_pmf),
      pOvertime: asNumber(slot.p_overtime),
      pistol: Object.entries(pistolRec)
        .map(([round, pTeam1]) => ({ round, pTeam1 }))
        .sort((a, b) => Number(a.round) - Number(b.round)),
      halves: Object.entries(halfRec)
        .map(([half, pTeam1]) => ({ half, pTeam1, pDraw: halfDraw[half] ?? null }))
        .sort((a, b) => Number(a.half) - Number(b.half)),
      players: parsePlayers(slot.players),
    });
  });
  slots.sort((a, b) => a.mapNumber - b.mapNumber);

  const estimates: MapEstimateView[] = [];
  const estRec = asRecord(root.map_estimates);
  if (estRec) {
    for (const [key, value] of Object.entries(estRec)) {
      const rec = asRecord(value);
      const p = asNumber(rec?.p_team1_win);
      if (!rec || p === null) continue;
      estimates.push({
        key,
        map: asString(rec.map_name) ?? key,
        pTeam1Win: p,
        sigma: asNumber(rec.sigma),
        pickedBy: asSlot(rec.picked_by),
      });
    }
  }
  estimates.sort((a, b) => b.pTeam1Win - a.pTeam1Win);

  const sequences: VetoSequenceView[] = [];
  if (Array.isArray(root.veto_distribution)) {
    for (const item of root.veto_distribution) {
      const rec = asRecord(item);
      const probability = asNumber(rec?.probability);
      if (!rec || probability === null || !Array.isArray(rec.maps)) continue;
      const maps = rec.maps.filter((m): m is string => typeof m === "string");
      const pickers = Array.isArray(rec.pickers) ? rec.pickers.map(asSlot) : maps.map(() => null);
      sequences.push({ maps, pickers, probability });
    }
  }
  sequences.sort((a, b) => b.probability - a.probability);

  const playerScopes: Record<string, PlayerView[]> = {};
  const scopes = asRecord(central.player_kills_by_scope);
  if (scopes) {
    for (const [scope, players] of Object.entries(scopes)) {
      const parsed = parsePlayers(players);
      if (parsed.length) playerScopes[scope] = parsed;
    }
  }
  for (const slot of slots) {
    const key = `map:${slot.mapNumber}`;
    if (!playerScopes[key] && slot.players.length) playerScopes[key] = slot.players;
  }

  const lowP = asNumber(low?.p_team1_win);
  const highP = asNumber(high?.p_team1_win);
  return {
    format: asString(root.format),
    pTeam1Win: asNumber(central.p_team1_win),
    pTeam1WinLow: lowP,
    pTeam1WinHigh: highP,
    sigmaTeam1Win: asNumber(root.sigma_team1_win),
    pDraw: asNumber(central.p_draw),
    mapScore: Object.entries(numRecord(central.map_score_pmf))
      .filter(([score]) => parseScore(score) !== null)
      .map(([score, p]) => ({ score, p }))
      .sort((a, b) => {
        const sa = parseScore(a.score) ?? [0, 0];
        const sb = parseScore(b.score) ?? [0, 0];
        return sb[0] - sb[1] - (sa[0] - sa[1]) || sb[0] - sa[0];
      }),
    slots,
    mapEstimates: estimates,
    vetoKnown: root.veto_known === true,
    vetoSequences: sequences,
    playerScopes,
    dataCompleteness: asNumber(root.data_completeness),
    nSimulations: asNumber(root.n_simulations),
    logitShiftApplied: asNumber(root.logit_shift_applied),
    modelVersion: asString(root.model_version),
  };
}

/**
 * Predicted map per slot. Uses the slots' `map_distribution` (conditional on the slot being
 * played); when a slot has none, falls back to the veto sequence distribution (probability mass of
 * sequences whose k-th map is m, normalised by the mass of sequences that reach slot k).
 */
export function slotMapDistribution(view: AnalysisView): { mapNumber: number; pPlayed: number | null; maps: { map: string; p: number }[] }[] {
  const nSlots = Math.max(view.slots.length, ...view.vetoSequences.map((s) => s.maps.length), 0);
  const totalMass = view.vetoSequences.reduce((s, q) => s + q.probability, 0);
  const out: { mapNumber: number; pPlayed: number | null; maps: { map: string; p: number }[] }[] = [];
  for (let k = 0; k < nSlots; k++) {
    const slot = view.slots.find((s) => s.mapNumber === k + 1);
    if (slot && slot.mapDistribution.length) {
      out.push({ mapNumber: k + 1, pPlayed: slot.pPlayed, maps: slot.mapDistribution });
      continue;
    }
    const acc = new Map<string, number>();
    let reach = 0;
    for (const seq of view.vetoSequences) {
      const map = seq.maps[k];
      if (map === undefined) continue;
      reach += seq.probability;
      acc.set(map, (acc.get(map) ?? 0) + seq.probability);
    }
    const maps = [...acc.entries()]
      .map(([map, p]) => ({ map, p: reach > 0 ? p / reach : 0 }))
      .sort((a, b) => b.p - a.p);
    out.push({
      mapNumber: k + 1,
      pPlayed: slot?.pPlayed ?? (totalMass > 0 ? reach / totalMass : null),
      maps,
    });
  }
  return out;
}

/** Display name for a player scope key. */
export function scopeLabel(scope: string): string {
  if (scope === "match") return "Whole match";
  const single = /^map:(\d+)$/.exec(scope);
  if (single) return `Map ${single[1]}`;
  const multi = /^maps:([\d,]+)$/.exec(scope);
  if (multi) return `Maps ${multi[1]?.replace(/,/g, "+")}`;
  return scope;
}

/** Scope keys in a sensible order: match, then single maps, then multi-map scopes. */
export function sortScopes(scopes: string[]): string[] {
  const rank = (s: string) => (s === "match" ? 0 : s.startsWith("map:") ? 1 : 2);
  return [...scopes].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b, "en", { numeric: true }));
}
