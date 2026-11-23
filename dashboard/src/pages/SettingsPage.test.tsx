import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { configOut, health, tradingState } from "../test/fixtures";
import { mockApi, renderWithProviders } from "../test/utils";
import SettingsPage from "./SettingsPage";

function routes(put: unknown = configOut) {
  return {
    "GET /api/control/config": configOut,
    "GET /api/health": health,
    "GET /api/control/state": tradingState,
    "GET /api/control/betclic-login": { configured: false, running: false, started: false, last_started_at: null, last_finished_at: null, last_result: null, last_error: null },
    "PUT /api/control/config": put,
  };
}

describe("SettingsPage", () => {
  it("shows effective values, defaults and overrides", async () => {
    mockApi(routes());
    renderWithProviders(<SettingsPage />);
    const kelly = await screen.findByLabelText("Kelly fraction");
    expect(kelly).toHaveValue(0.2);
    expect(screen.getAllByText("override")).toHaveLength(2);
    expect(screen.getByText("Not configured")).toBeInTheDocument();
  });

  it("blocks invalid input on the client", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi(routes());
    renderWithProviders(<SettingsPage />);
    const kelly = await screen.findByLabelText("Kelly fraction");
    await user.clear(kelly);
    await user.type(kelly, "1.5");
    await user.click(screen.getByRole("button", { name: "Save bankroll settings" }));
    expect(await screen.findByText("must be in (0, 1]")).toBeInTheDocument();
    expect(kelly).toHaveAttribute("aria-invalid", "true");
    expect(calls.some((c) => c.method === "PUT")).toBe(false);
  });

  it("PUTs only the edited section with every non-default value", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi(routes());
    renderWithProviders(<SettingsPage />);
    const maxBets = await screen.findByLabelText("Max bets per day");
    await user.clear(maxBets);
    await user.type(maxBets, "5");
    await user.click(screen.getByRole("button", { name: "Save bankroll settings" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));
    const put = calls.find((c) => c.method === "PUT");
    expect(put?.body).toEqual({ bankroll: { kelly_fraction: 0.2, max_bets_per_day: 5 } });
    expect(put?.headers["X-Requested-With"]).toBe("cs2bot");
    expect(await screen.findByText("Settings saved.")).toBeInTheDocument();
  });

  it("shows server-side 422 errors next to the field", async () => {
    const user = userEvent.setup();
    mockApi(
      routes({
        status: 422,
        body: { detail: [{ loc: ["body", "decision", "min_ev"], msg: "must be in [0, 1] (server)", type: "value_error" }] },
      }),
    );
    renderWithProviders(<SettingsPage />);
    await screen.findByLabelText("Min EV");
    await user.click(screen.getByRole("button", { name: "Save decision settings" }));
    expect(await screen.findByText("must be in [0, 1] (server)")).toBeInTheDocument();
  });
});
