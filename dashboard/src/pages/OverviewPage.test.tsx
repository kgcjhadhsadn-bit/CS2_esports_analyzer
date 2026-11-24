import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { bankroll, betPage, health, matchPage, tradingState } from "../test/fixtures";
import { mockApi, renderWithProviders } from "../test/utils";
import OverviewPage from "./OverviewPage";

const plain = (s: string | null | undefined) => (s ?? "").replace(/[\u00a0\u202f]/g, " ");

function routes(extra: Record<string, unknown> = {}) {
  return {
    "GET /api/health": health,
    "GET /api/control/state": tradingState,
    "GET /api/bankroll": bankroll,
    "GET /api/matches": matchPage,
    "GET /api/bets": betPage,
    "GET /api/control/betclic-login": { configured: true, running: false, started: false, last_started_at: null, last_finished_at: null, last_result: null, last_error: null },
    ...extra,
  };
}

describe("OverviewPage", () => {
  it("shows bankroll, loss-limit meters, alerts, matches and bets", async () => {
    const { calls } = mockApi(routes());
    renderWithProviders(<OverviewPage />);
    expect(await screen.findByText("Balance")).toBeInTheDocument();
    const balance = screen.getByText("Balance").parentElement;
    expect(plain(balance?.textContent)).toContain("968,44 zł");
    expect(screen.getByRole("meter", { name: /Daily loss limit/ })).toHaveAttribute("aria-valuenow", "59");
    expect(await screen.findByText(/Betclic login required/)).toBeInTheDocument();
    expect(await screen.findAllByRole("link", { name: /Synth Team 1/ })).not.toHaveLength(0);
    expect(screen.getByText("Paper won")).toBeInTheDocument();
    // dry-run is on, so the paper bankroll is requested by default
    expect(calls.find((c) => c.path === "/api/bankroll")?.search.get("dry_run")).toBe("true");
    const matchesCall = calls.find((c) => c.path === "/api/matches");
    expect(matchesCall?.search.getAll("status")).toEqual(["scheduled", "live"]);
    expect(matchesCall?.search.get("order")).toBe("asc");
  });

  it("engages the kill switch only after confirmation, with the CSRF header", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi(
      routes({ "POST /api/control/kill-switch": { on: true, reason: "manual stop from the dashboard", updated_at: null, updated_by: "dashboard" } }),
    );
    renderWithProviders(<OverviewPage />);
    await user.click(await screen.findByRole("button", { name: /Kill switch - stop all betting/ }));
    const dialog = screen.getByRole("dialog", { name: "Engage the kill switch?" });
    expect(calls.some((c) => c.method === "POST")).toBe(false);
    const reason = within(dialog).getByLabelText(/Reason/);
    await user.clear(reason);
    await user.type(reason, "suspicious odds");
    await user.click(within(dialog).getByRole("button", { name: "Stop all betting" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/control/kill-switch")).toBe(true));
    const post = calls.find((c) => c.path === "/api/control/kill-switch");
    expect(post?.method).toBe("POST");
    expect(post?.body).toEqual({ on: true, reason: "suspicious odds" });
    expect(post?.headers["X-Requested-With"]).toBe("cs2bot");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(await screen.findByText(/Kill switch is ON/)).toBeInTheDocument();
  });

  it("cancelling the dialog sends nothing", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi(routes());
    renderWithProviders(<OverviewPage />);
    await user.click(await screen.findByRole("button", { name: /Kill switch - stop all betting/ }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(calls.some((c) => c.method !== "GET")).toBe(false);
  });

  it("opens the Betclic login window from the alert", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi(
      routes({
        "POST /api/control/betclic-login": {
          status: 202,
          body: { configured: true, running: true, started: true, last_started_at: "2026-11-20T12:00:07Z", last_finished_at: null, last_result: null, last_error: null },
        },
      }),
    );
    renderWithProviders(<OverviewPage />);
    await user.click(await screen.findByRole("button", { name: "Open Betclic login" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Open window" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/api/control/betclic-login")).toBe(true));
    expect(await screen.findByRole("button", { name: /Login window open/ })).toBeDisabled();
  });

  it("surfaces API errors as toasts", async () => {
    mockApi(routes({ "GET /api/bankroll": { status: 503, body: { detail: "database unavailable" } } }));
    renderWithProviders(<OverviewPage />);
    // one toast and one inline error box in the bankroll card
    await waitFor(() => {
      const messages = screen.getAllByText("database unavailable");
      expect(messages.some((el) => el.closest(".toast") !== null)).toBe(true);
      expect(messages.some((el) => el.closest(".error-box") !== null)).toBe(true);
    });
  });
});
