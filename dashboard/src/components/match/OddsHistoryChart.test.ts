import { describe, expect, it } from "vitest";
import { oddsHistory } from "../../test/fixtures";
import { groupOddsSeries, mergeSeries } from "./OddsHistoryChart";

const t = (iso: string) => new Date(iso).getTime();

describe("mergeSeries", () => {
  it("carries each price forward and leaves gaps while suspended", () => {
    const { rows, keys } = mergeSeries(oddsHistory.series);
    expect(keys).toEqual(["s1", "s2"]);
    expect(rows.map((r) => r.t)).toEqual(
      ["09:00", "09:30", "10:00", "10:30", "11:00"].map((hm) => t(`2026-11-20T${hm}:00Z`)),
    );
    expect(rows.map((r) => r.s1)).toEqual([2.05, 2.05, 2.0, 2.0, 1.95]);
    // selection 2 has no price before its first snapshot and is suspended from 10:30
    expect(rows.map((r) => r.s2)).toEqual([null, 1.75, 1.75, null, null]);
  });

  it("extends the last prices to `until` so an unchanged price is still drawn", () => {
    const single = [{ ...oddsHistory.series[0]!, points: [{ captured_at: "2026-11-20T09:00:00Z", odds: "1.90", is_available: true }] }];
    expect(mergeSeries(single).rows).toHaveLength(1);
    const until = t("2026-11-20T12:00:00Z");
    const { rows } = mergeSeries(single, until);
    expect(rows).toEqual([
      { t: t("2026-11-20T09:00:00Z"), s1: 1.9 },
      { t: until, s1: 1.9 },
    ]);
    // `until` before the last snapshot (e.g. the match start of an old price) adds nothing
    expect(mergeSeries(single, t("2026-11-20T08:00:00Z")).rows).toHaveLength(1);
    expect(mergeSeries([], until).rows).toEqual([]);
  });
});

describe("groupOddsSeries", () => {
  it("groups both sides of a market and keeps unmapped markets apart", () => {
    const unmapped = { ...oddsHistory.series[0]!, selection_id: 9, spec_key: null, market_raw: "Dziwny rynek" };
    const groups = groupOddsSeries([...oddsHistory.series, unmapped]);
    expect(groups.map((g) => g.series.length)).toEqual([2, 1]);
    expect(groups[1]?.label).toBe("Dziwny rynek (unmapped)");
  });
});
