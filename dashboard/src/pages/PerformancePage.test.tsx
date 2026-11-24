import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { Calibration, CalibrationSet, MarketPerf } from "../api/types";
import { bankroll, health } from "../test/fixtures";
import { mockApi, renderWithProviders } from "../test/utils";
import PerformancePage, { drawdownSeries, reliabilityPoints } from "./PerformancePage";

const set: CalibrationSet = {
  n: 2,
  base_rate: 0.5,
  mean_predicted: 0.77,
  brier: 0.313,
  log_loss: 0.837789,
  ece: 0.27,
  bins: [
    { lower: 0.0, upper: 0.1, count: 0, mean_predicted: null, observed_rate: null, ci_low: null, ci_high: null },
    { lower: 0.7, upper: 0.8, count: 2, mean_predicted: 0.77, observed_rate: 0.5, ci_low: 0.094529, ci_high: 0.905471 },
  ],
  brier_by_month: [{ period: "2026-11", n: 2, brier: 0.313 }],
};

const calibration: Calibration = { n_bins: 10, bets: set, predictions: set, predictions_by_market: { match_winner: set } };

const marketPerf: MarketPerf = {
  mode: "dry_run",
  items: [
    { market_type: "match_winner", bets: 2, open: 1, settled: 1, won: 1, lost: 0, void: 0, hit_rate: 1, staked: "20.00", pnl: "13.44", roi: 0.672, avg_odds: "1.90", avg_clv: 0.03, avg_ev: 0.3364 },
  ],
  total: { market_type: "all", bets: 2, open: 1, settled: 1, won: 1, lost: 0, void: 0, hit_rate: 1, staked: "20.00", pnl: "13.44", roi: 0.672, avg_odds: "1.90", avg_clv: 0.03, avg_ev: 0.3364 },
};

describe("PerformancePage", () => {
  it("defaults to the paper book while dry-run is on and switches to real money", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi({
      "GET /api/health": health,
      "GET /api/bankroll": bankroll,
      "GET /api/performance/markets": marketPerf,
      "GET /api/performance/calibration": calibration,
    });
    renderWithProviders(<PerformancePage />);
    expect(await screen.findByText("All markets")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Calibration reliability diagram" })).toBeInTheDocument();
    await waitFor(() => expect(calls.some((c) => c.path === "/api/bankroll" && c.search.get("dry_run") === "true")).toBe(true));
    expect(calls.filter((c) => c.path === "/api/performance/markets").at(-1)?.search.get("mode")).toBe("dry_run");
    await user.click(screen.getByRole("button", { name: "Real money" }));
    await waitFor(() => expect(calls.filter((c) => c.path === "/api/bankroll").at(-1)?.search.get("dry_run")).toBe("false"));
  });
});

describe("performance helpers", () => {
  it("measures the drawdown below the running peak (peak starts at 0)", () => {
    const dd = drawdownSeries([
      { ts: "2026-11-01T10:00:00Z", cumulative_pnl: "-5.00" },
      { ts: "2026-11-02T10:00:00Z", cumulative_pnl: "10.00" },
      { ts: "2026-11-03T10:00:00Z", cumulative_pnl: "4.00" },
      { ts: "bad", cumulative_pnl: "1.00" },
      { ts: "2026-11-04T10:00:00Z", cumulative_pnl: "12.50" },
    ]);
    expect(dd.map((p) => p.dd)).toEqual([-5, 0, -6, 0]);
  });

  it("plots only non-empty bins with their Wilson interval as error bars", () => {
    const points = reliabilityPoints(set);
    expect(points).toHaveLength(1);
    expect(points[0]?.x).toBe(0.77);
    expect(points[0]?.err[0]).toBeCloseTo(0.405471, 6);
    expect(points[0]?.err[1]).toBeCloseTo(0.405471, 6);
  });
});
