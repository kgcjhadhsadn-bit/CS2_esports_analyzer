/**
 * Display formatting. Money is Polish zloty (Intl pl-PL), every time is shown in Europe/Warsaw,
 * probabilities / rates as percentages. All helpers accept the API's raw values (money and odds
 * arrive as strings) and render a dash for missing / unparsable input instead of throwing.
 */

export const TIME_ZONE = "Europe/Warsaw";
export const DASH = "—";

const moneyFormat = new Intl.NumberFormat("pl-PL", { style: "currency", currency: "PLN" });
const moneySignedFormat = new Intl.NumberFormat("pl-PL", {
  style: "currency",
  currency: "PLN",
  signDisplay: "exceptZero",
});

/** Number from an API value (`"12.50"`, `12.5`); null for missing / non-finite input. */
export function toNumber(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

/** "12,50 zł" (pl-PL, PLN). `signed` adds "+" to positive amounts (P&L). */
export function formatMoney(value: string | number | null | undefined, opts: { signed?: boolean } = {}): string {
  const n = toNumber(value);
  if (n === null) return DASH;
  return (opts.signed ? moneySignedFormat : moneyFormat).format(n);
}

/** Decimal odds with 2-3 decimals: "1.85", "1.875", "2.00". */
export function formatOdds(value: string | number | null | undefined): string {
  const n = toNumber(value);
  if (n === null) return DASH;
  const three = n.toFixed(3);
  return three.endsWith("0") ? n.toFixed(2) : three;
}

/** 0.753 -> "75.3%". */
export function formatPct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
  return `${(value * 100).toFixed(digits)}%`;
}

/** Signed percentage for EV / ROI / CLV: 0.0372 -> "+3.7%", -0.1 -> "−10.0%". */
export function formatSignedPct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
  const text = (Math.abs(value) * 100).toFixed(digits);
  if (Number(text) === 0) return `${(0).toFixed(digits)}%`;
  return `${value > 0 ? "+" : "−"}${text}%`;
}

/** Plain number with fixed decimals ("0.031"). */
export function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
  return value.toFixed(digits);
}

/** Signed plain number: 0.12 -> "+0.12", -0.5 -> "−0.50". */
export function formatSigned(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
  const text = Math.abs(value).toFixed(digits);
  if (Number(text) === 0) return (0).toFixed(digits);
  return `${value > 0 ? "+" : "\u2212"}${text}`;
}

/** "pos" / "neg" / "" for colouring signed values. */
export function signClass(value: string | number | null | undefined): "pos" | "neg" | "" {
  const n = toNumber(value);
  if (n === null || n === 0) return "";
  return n > 0 ? "pos" : "neg";
}

function parseDate(value: string | Date | null | undefined): Date | null {
  if (value === null || value === undefined || value === "") return null;
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

const dateTimeFormat = new Intl.DateTimeFormat("en-GB", {
  timeZone: TIME_ZONE,
  weekday: "short",
  day: "2-digit",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});
const fullDateTimeFormat = new Intl.DateTimeFormat("en-GB", {
  timeZone: TIME_ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});
const dateFormat = new Intl.DateTimeFormat("en-GB", {
  timeZone: TIME_ZONE,
  day: "2-digit",
  month: "short",
  year: "numeric",
});
const timeFormat = new Intl.DateTimeFormat("en-GB", {
  timeZone: TIME_ZONE,
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});
const shortDateFormat = new Intl.DateTimeFormat("en-GB", { timeZone: TIME_ZONE, day: "2-digit", month: "short" });

/** "Sat 21 Nov, 13:00" in Warsaw time. */
export function formatDateTime(value: string | Date | null | undefined): string {
  const d = parseDate(value);
  return d ? dateTimeFormat.format(d) : DASH;
}

/** "21/11/2026, 13:00:05" (Warsaw) - for tooltips and audit fields. */
export function formatFullDateTime(value: string | Date | null | undefined): string {
  const d = parseDate(value);
  return d ? `${fullDateTimeFormat.format(d)} (Warsaw)` : DASH;
}

/** "21 Nov 2026" (Warsaw). */
export function formatDate(value: string | Date | null | undefined): string {
  const d = parseDate(value);
  return d ? dateFormat.format(d) : DASH;
}

/** "21 Nov" (Warsaw). */
export function formatShortDate(value: string | Date | null | undefined): string {
  const d = parseDate(value);
  return d ? shortDateFormat.format(d) : DASH;
}

/** "13:00" (Warsaw). */
export function formatTime(value: string | Date | null | undefined): string {
  const d = parseDate(value);
  return d ? timeFormat.format(d) : DASH;
}

/** A duration in seconds as "45 s", "12 min", "3.5 h", "2.0 d". */
export function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return DASH;
  const s = Math.abs(seconds);
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 90 * 60) return `${Math.round(s / 60)} min`;
  if (s < 36 * 3600) return `${(s / 3600).toFixed(1)} h`;
  return `${(s / 86400).toFixed(1)} d`;
}

/** "in 2.0 h" / "5 min ago" relative to `now`. */
export function formatRelative(value: string | Date | null | undefined, now: Date = new Date()): string {
  const d = parseDate(value);
  if (!d) return DASH;
  const diff = (d.getTime() - now.getTime()) / 1000;
  if (Math.abs(diff) < 30) return "now";
  return diff > 0 ? `in ${formatAge(diff)}` : `${formatAge(-diff)} ago`;
}

/** Offset of `timeZone` from UTC in minutes at the instant `date` (Warsaw: +60 or +120). */
export function timeZoneOffsetMinutes(date: Date, timeZone: string = TIME_ZONE): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(date);
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value ?? "0");
  const asUtc = Date.UTC(get("year"), get("month") - 1, get("day"), get("hour"), get("minute"), get("second"));
  return Math.round((asUtc - Math.floor(date.getTime() / 1000) * 1000) / 60000);
}

/**
 * UTC ISO instant of a Warsaw calendar date ("2026-11-21" -> "2026-11-20T23:00:00.000Z").
 * `endOfDay` gives the last millisecond of that Warsaw day (the API's `to` bounds are inclusive).
 * Returns null for an invalid date string.
 */
export function warsawDateToUtcIso(date: string, endOfDay = false): string | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  if (!m) return null;
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const naive = Date.UTC(y, mo - 1, d, endOfDay ? 23 : 0, endOfDay ? 59 : 0, endOfDay ? 59 : 0, endOfDay ? 999 : 0);
  if (Number.isNaN(naive)) return null;
  // Two passes handle the DST switch (the offset at the guess may differ from the final one).
  let instant = naive - timeZoneOffsetMinutes(new Date(naive)) * 60000;
  instant = naive - timeZoneOffsetMinutes(new Date(instant)) * 60000;
  return new Date(instant).toISOString();
}

/** Today's date in Warsaw as "YYYY-MM-DD". */
export function warsawToday(now: Date = new Date()): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(now);
  return parts;
}

/** "match_winner" -> "Match winner". */
export function humanize(value: string | null | undefined): string {
  if (!value) return DASH;
  const text = value.replace(/_/g, " ").trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Team name with a fallback for a missing team. */
export function teamName(team: { name: string } | null | undefined, slot: "team1" | "team2" = "team1"): string {
  return team?.name ?? (slot === "team1" ? "Team 1" : "Team 2");
}

/** "map_handicap|map=1|line=-1.5" -> "Map handicap (map 1, line -1.5)". */
export function specKeyLabel(specKey: string): string {
  const [head, ...parts] = specKey.split("|");
  const extras = parts.map((p) => p.replace("=", " ").replace(/^opp /, "vs player "));
  return `${humanize(head ?? specKey)}${extras.length ? ` (${extras.join(", ")})` : ""}`;
}
