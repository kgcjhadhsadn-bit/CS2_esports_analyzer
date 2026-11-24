import { describe, expect, it } from "vitest";
import {
  DASH,
  formatAge,
  formatDateTime,
  formatMoney,
  formatOdds,
  formatPct,
  formatRelative,
  formatSigned,
  formatSignedPct,
  formatTime,
  humanize,
  signClass,
  specKeyLabel,
  timeZoneOffsetMinutes,
  toNumber,
  warsawDateToUtcIso,
  warsawToday,
} from "./format";

/** Intl uses non-breaking (and narrow non-breaking) spaces; normalise them for assertions. */
const plain = (s: string) => s.replace(/[\u00a0\u202f]/g, " ");

describe("money (pl-PL, PLN)", () => {
  it("formats API money strings as Polish zloty", () => {
    expect(plain(formatMoney("12.50"))).toBe("12,50 zł");
    expect(plain(formatMoney("-30.00"))).toBe("-30,00 zł");
    expect(plain(formatMoney(12345.6))).toBe("12 345,60 zł");
  });

  it("matches Intl.NumberFormat pl-PL / PLN exactly", () => {
    const reference = new Intl.NumberFormat("pl-PL", { style: "currency", currency: "PLN" });
    expect(formatMoney("968.44")).toBe(reference.format(968.44));
  });

  it("adds a sign for P&L when asked", () => {
    expect(plain(formatMoney("13.44", { signed: true }))).toBe("+13,44 zł");
    expect(plain(formatMoney("-16.56", { signed: true }))).toBe("-16,56 zł");
    expect(plain(formatMoney("0.00", { signed: true }))).toBe("0,00 zł");
  });

  it("renders a dash for missing or garbage values", () => {
    expect(formatMoney(null)).toBe(DASH);
    expect(formatMoney(undefined)).toBe(DASH);
    expect(formatMoney("")).toBe(DASH);
    expect(formatMoney("abc")).toBe(DASH);
  });
});

describe("odds, percentages, numbers", () => {
  it("keeps 2-3 decimals for odds", () => {
    expect(formatOdds("1.85")).toBe("1.85");
    expect(formatOdds("1.875")).toBe("1.875");
    expect(formatOdds("2")).toBe("2.00");
    expect(formatOdds(null)).toBe(DASH);
  });

  it("formats probabilities and signed rates", () => {
    expect(formatPct(0.753)).toBe("75.3%");
    expect(formatPct(0.5, 0)).toBe("50%");
    expect(formatPct(Number.NaN)).toBe(DASH);
    expect(formatSignedPct(0.0372)).toBe("+3.7%");
    expect(formatSignedPct(-0.1)).toBe("−10.0%");
    expect(formatSignedPct(0.00001)).toBe("0.0%");
    expect(formatSigned(0.12)).toBe("+0.12");
    expect(formatSigned(-0.5)).toBe("−0.50");
  });

  it("classifies signs and parses numbers defensively", () => {
    expect(signClass("-0.01")).toBe("neg");
    expect(signClass(2)).toBe("pos");
    expect(signClass("0.00")).toBe("");
    expect(signClass(null)).toBe("");
    expect(toNumber("1e400")).toBeNull();
    expect(toNumber("3.5")).toBe(3.5);
  });
});

describe("Europe/Warsaw times", () => {
  it("shows UTC instants in Warsaw time (CET in winter, CEST in summer)", () => {
    expect(formatTime("2026-11-21T12:00:00Z")).toBe("13:00");
    expect(formatTime("2026-07-01T12:00:00Z")).toBe("14:00");
    expect(formatDateTime("2026-11-21T12:00:00Z")).toContain("13:00");
    expect(formatDateTime("2026-11-21T12:00:00Z")).toContain("21 Nov");
    expect(formatDateTime("not a date")).toBe(DASH);
  });

  it("knows the Warsaw offset", () => {
    expect(timeZoneOffsetMinutes(new Date("2026-01-15T12:00:00Z"))).toBe(60);
    expect(timeZoneOffsetMinutes(new Date("2026-07-15T12:00:00Z"))).toBe(120);
  });

  it("converts Warsaw calendar dates to UTC bounds", () => {
    expect(warsawDateToUtcIso("2026-11-21")).toBe("2026-11-20T23:00:00.000Z");
    expect(warsawDateToUtcIso("2026-11-21", true)).toBe("2026-11-21T22:59:59.999Z");
    expect(warsawDateToUtcIso("2026-07-01")).toBe("2026-06-30T22:00:00.000Z");
    // DST change days (2026-03-29 spring forward, 2026-10-25 fall back)
    expect(warsawDateToUtcIso("2026-03-29")).toBe("2026-03-28T23:00:00.000Z");
    expect(warsawDateToUtcIso("2026-03-29", true)).toBe("2026-03-29T21:59:59.999Z");
    expect(warsawDateToUtcIso("2026-10-25")).toBe("2026-10-24T22:00:00.000Z");
    expect(warsawDateToUtcIso("2026-10-25", true)).toBe("2026-10-25T22:59:59.999Z");
    expect(warsawDateToUtcIso("21/11/2026")).toBeNull();
  });

  it("gives today's Warsaw date", () => {
    expect(warsawToday(new Date("2026-11-20T23:30:00Z"))).toBe("2026-11-21");
  });

  it("formats ages and relative times", () => {
    expect(formatAge(45)).toBe("45 s");
    expect(formatAge(600)).toBe("10 min");
    expect(formatAge(3 * 3600)).toBe("3.0 h");
    expect(formatAge(3 * 86400)).toBe("3.0 d");
    expect(formatAge(null)).toBe(DASH);
    const now = new Date("2026-11-20T12:00:00Z");
    expect(formatRelative("2026-11-20T14:00:00Z", now)).toBe("in 2.0 h");
    expect(formatRelative("2026-11-20T11:55:00Z", now)).toBe("5 min ago");
    expect(formatRelative("2026-11-20T12:00:10Z", now)).toBe("now");
  });
});

describe("labels", () => {
  it("humanizes identifiers and spec keys", () => {
    expect(humanize("match_winner")).toBe("Match winner");
    expect(humanize(null)).toBe(DASH);
    expect(specKeyLabel("map_handicap|line=-1.5")).toBe("Map handicap (line -1.5)");
    expect(specKeyLabel("total_rounds|map=2|line=21.5")).toBe("Total rounds (map 2, line 21.5)");
  });
});
