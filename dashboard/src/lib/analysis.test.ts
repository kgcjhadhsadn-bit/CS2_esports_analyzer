import { describe, expect, it } from "vitest";
import { analysisPayload } from "../test/fixtures";
import {
  parseAnalysis,
  pmfStats,
  probOver,
  roundMarginBars,
  roundTotalBars,
  scopeLabel,
  slotMapDistribution,
  sortScopes,
} from "./analysis";

describe("parseAnalysis", () => {
  it("extracts the match, slot and uncertainty views", () => {
    const view = parseAnalysis(analysisPayload);
    expect(view).not.toBeNull();
    if (!view) return;
    expect(view.pTeam1Win).toBe(0.8);
    expect(view.pTeam1WinLow).toBe(0.75);
    expect(view.pTeam1WinHigh).toBe(0.85);
    expect(view.slots).toHaveLength(3);
    expect(view.slots[0]?.pTeam1WinLow).toBe(0.66);
    expect(view.slots[0]?.pTeam1WinHigh).toBe(0.74);
    expect(view.slots[1]?.pTeam1WinLow).toBeNull();
    expect(view.slots[0]?.mapDistribution[0]).toEqual({ map: "mirage", p: 0.6 });
    expect(view.slots[0]?.pistol).toEqual([
      { round: "1", pTeam1: 0.55 },
      { round: "13", pTeam1: 0.52 },
    ]);
    expect(view.mapScore.map((s) => s.score)).toEqual(["2-0", "2-1", "1-2", "0-2"]);
    expect(view.mapEstimates[0]?.map).toBe("mirage");
    expect(Object.keys(view.playerScopes).sort()).toEqual(["map:1", "match"]);
    expect(view.playerScopes.match?.[0]?.nickname).toBe("s1_awper");
  });

  it("returns null without a central core and skips malformed entries", () => {
    expect(parseAnalysis(null)).toBeNull();
    expect(parseAnalysis({ central: "nope" })).toBeNull();
    const view = parseAnalysis({
      central: {
        p_team1_win: "0.5",
        map_score_pmf: { "2-0": 0.5, garbage: 0.5, "1-2": "x" },
        slots: [null, { map_number: 1, score_pmf: { "13-5": 1 }, players: { "1": { kills_mean: "x" } } }],
        player_kills_by_scope: { match: { "9": { kills_mean: 20, kills_var: -1 } } },
      },
      map_estimates: { bad: { p_team1_win: null } },
      veto_distribution: [{ maps: "x" }, { maps: ["a"], probability: 1 }],
    });
    expect(view?.pTeam1Win).toBeNull();
    expect(view?.mapScore).toEqual([{ score: "2-0", p: 0.5 }]);
    expect(view?.slots).toHaveLength(1);
    expect(view?.slots[0]?.players).toEqual([]);
    expect(view?.mapEstimates).toEqual([]);
    expect(view?.vetoSequences).toHaveLength(1);
    expect(view?.playerScopes.match?.[0]?.sd).toBe(0);
  });
});

describe("score distributions", () => {
  const pmf = { "13-9": 0.3, "13-11": 0.2, "11-13": 0.2, "9-13": 0.1, "16-14": 0.2 };

  it("builds total-round and margin histograms", () => {
    expect(roundTotalBars(pmf).map((b) => [b.x, Number(b.p.toFixed(3))])).toEqual([
      [22, 0.4],
      [24, 0.4],
      [30, 0.2],
    ]);
    const margins = roundMarginBars(pmf);
    expect(margins.map((b) => b.label)).toEqual(["-4", "-2", "+2", "+4"]);
    expect(margins.find((b) => b.x === 2)?.p).toBeCloseTo(0.4);
  });

  it("computes stats and tail probabilities", () => {
    const totals = roundTotalBars(pmf);
    expect(pmfStats(totals).mean).toBeCloseTo(24.4);
    expect(probOver(totals, 23.5)).toBeCloseTo(0.6);
    expect(probOver([], 1)).toBeNull();
    expect(pmfStats([]).mean).toBeNull();
  });
});

describe("veto slots", () => {
  it("uses the slot distribution, else the veto sequences", () => {
    const view = parseAnalysis(analysisPayload);
    if (!view) throw new Error("no view");
    const slots = slotMapDistribution(view);
    expect(slots).toHaveLength(3);
    expect(slots[0]?.maps[0]).toEqual({ map: "mirage", p: 0.6 });
    // slot 3 has an empty map_distribution: falls back to the sequences (ancient 0.6, anubis 0.4)
    expect(slots[2]?.maps).toEqual([
      { map: "ancient", p: 0.6 },
      { map: "anubis", p: 0.4 },
    ]);
    expect(slots[2]?.pPlayed).toBe(0.5);
  });

  it("labels and orders player scopes", () => {
    expect(scopeLabel("match")).toBe("Whole match");
    expect(scopeLabel("map:2")).toBe("Map 2");
    expect(scopeLabel("maps:1,2")).toBe("Maps 1+2");
    expect(sortScopes(["maps:1,2", "map:2", "match", "map:1"])).toEqual(["match", "map:1", "map:2", "maps:1,2"]);
  });
});
