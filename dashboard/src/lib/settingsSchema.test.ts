import { describe, expect, it } from "vitest";
import { configOut } from "../test/fixtures";
import {
  BANKROLL_FIELDS,
  buildSection,
  DECISION_FIELDS,
  parseField,
  rowsToOverrides,
  sameValue,
  serverFieldErrors,
  toFormValue,
} from "./settingsSchema";

const spec = (key: string) => [...DECISION_FIELDS, ...BANKROLL_FIELDS].find((f) => f.key === key)!;

describe("parseField", () => {
  it("enforces open and closed ranges like the API", () => {
    expect(parseField(spec("min_probability"), "0.8")).toEqual({ value: 0.8 });
    expect(parseField(spec("min_probability"), "1").error).toBe("must be in (0, 1)");
    expect(parseField(spec("min_probability"), "0").error).toBe("must be in (0, 1)");
    expect(parseField(spec("min_ev"), "0").value).toBe(0);
    expect(parseField(spec("max_sigma"), "0.6").error).toBe("must be in (0, 0.5]");
    expect(parseField(spec("kelly_fraction"), "1").value).toBe(1);
    expect(parseField(spec("max_bets_per_day"), "2.5").error).toBe("must be a whole number");
    expect(parseField(spec("stake_step"), "0").error).toBe("must be in (0, 1000]");
    expect(parseField(spec("min_odds"), "1.50").value).toBe("1.50");
    expect(parseField(spec("currency"), "pln").error).toContain("3-letter");
    expect(parseField(spec("min_ev"), "").error).toBe("required");
    expect(parseField(spec("min_ev"), "NaN").error).toBe("must be a number");
    expect(parseField(spec("prefer_known_veto"), false).value).toBe(false);
  });
});

describe("buildSection", () => {
  const decisionForm = () =>
    Object.fromEntries(DECISION_FIELDS.map((f) => [f.key, toFormValue(configOut.effective.decision[f.key])]));

  it("sends only values that differ from the defaults", () => {
    const res = buildSection("decision", decisionForm(), configOut.defaults.decision, { marketRows: [], disabledMarkets: [] });
    expect(res.errors).toEqual({});
    expect(res.body).toEqual({ min_ev: 0.05 });
  });

  it("checks max odds > min odds", () => {
    const form = { ...decisionForm(), min_odds: "3", max_odds: "2" };
    expect(buildSection("decision", form, configOut.defaults.decision).errors.max_odds).toBe("must be greater than min odds");
  });

  it("includes market overrides and disabled markets", () => {
    const res = buildSection("decision", decisionForm(), configOut.defaults.decision, {
      marketRows: [{ market: "map_winner", min_probability: "0.8", min_ev: "", max_sigma: "" }],
      disabledMarkets: ["top_fragger", "player_kills"],
    });
    expect(res.body.market_overrides).toEqual({ map_winner: { min_probability: 0.8 } });
    expect(res.body.disabled_markets).toEqual(["player_kills", "top_fragger"]);
  });

  it("validates override rows", () => {
    expect(rowsToOverrides([{ market: "", min_probability: "", min_ev: "", max_sigma: "" }]).errors).toHaveLength(1);
    expect(rowsToOverrides([{ market: "map_winner", min_probability: "2", min_ev: "", max_sigma: "" }]).errors[0]).toContain(
      "min_probability",
    );
    expect(
      rowsToOverrides([
        { market: "map_winner", min_probability: "0.8", min_ev: "", max_sigma: "" },
        { market: "map_winner", min_probability: "0.9", min_ev: "", max_sigma: "" },
      ]).errors[0],
    ).toContain("twice");
  });

  it("compares decimals numerically", () => {
    expect(sameValue("1.0", "1.00")).toBe(true);
    expect(sameValue(0.25, 0.25)).toBe(true);
    expect(sameValue(["a"], ["a"])).toBe(true);
  });

  it("maps 422 items to the section's fields", () => {
    const items = [
      { loc: ["body", "bankroll", "kelly_fraction"], msg: "must be in (0, 1]" },
      { loc: ["body", "decision", "min_ev"], msg: "bad" },
    ];
    expect(serverFieldErrors(items, "bankroll")).toEqual({ kelly_fraction: "must be in (0, 1]" });
  });
});
