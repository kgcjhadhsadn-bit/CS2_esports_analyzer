import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { reviewPage } from "../test/fixtures";
import { mockApi, renderWithProviders } from "../test/utils";
import ReviewPage, { manualRelinkBody } from "./ReviewPage";

describe("ReviewPage", () => {
  it("confirms the current candidate with its suggested orientation", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi({
      "GET /api/review/links": reviewPage,
      "POST /api/review/links/2": {
        event: { ...reviewPage.items[0]!.event, link_status: "confirmed", teams_swapped: true },
        action: "confirm",
        previous_match_id: 103,
        previous_status: "pending_review",
        previous_teams_swapped: false,
        markets_reset: 1,
        remapped: null,
        open_bets: 0,
      },
    });
    renderWithProviders(<ReviewPage />);
    await user.click(await screen.findByRole("button", { name: "Confirm" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).toContain('Betclic "Synth Team 6" = Synth Team 6');
    await user.click(within(dialog).getByRole("button", { name: "Confirm link" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ action: "confirm", match_id: 103, teams_swapped: true });
    expect(await screen.findByText(/1 market\(s\) cleared/)).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/review/links")?.search.getAll("status")).toEqual(["pending_review"]);
  });

  it("relinks and rejects", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi({
      "GET /api/review/links": reviewPage,
      "POST /api/review/links/2": (call: { body: unknown }) => ({
        event: reviewPage.items[0]!.event,
        action: (call.body as { action: string }).action,
        previous_match_id: 103,
        previous_status: "pending_review",
        previous_teams_swapped: false,
        markets_reset: 0,
        remapped: null,
        open_bets: 2,
      }),
    });
    renderWithProviders(<ReviewPage />);
    await user.click(await screen.findByRole("button", { name: "Link here" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Relink" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "POST")).toHaveLength(1));
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ action: "relink", match_id: 101, teams_swapped: false });
    expect(await screen.findByText(/2 open bet\(s\)/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Reject" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "POST")).toHaveLength(2));
    expect(calls.filter((c) => c.method === "POST")[1]?.body).toEqual({ action: "reject" });
  });

  it("lets the API detect the team order of a manual relink unless the owner picks one", async () => {
    // Regression: the manual relink always sent teams_swapped=false, overriding the API's
    // detection; a match listed the other way round would then be priced for the wrong team.
    const user = userEvent.setup();
    const { calls } = mockApi({
      "GET /api/review/links": reviewPage,
      "POST /api/review/links/2": {
        event: reviewPage.items[0]!.event,
        action: "relink",
        previous_match_id: 103,
        previous_status: "pending_review",
        previous_teams_swapped: false,
        markets_reset: 0,
        remapped: null,
        open_bets: 0,
      },
    });
    renderWithProviders(<ReviewPage />);
    await user.type(await screen.findByLabelText("Relink to match id"), "555");
    await user.click(screen.getByRole("button", { name: "Relink" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Relink" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "POST")).toHaveLength(1));
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ action: "relink", match_id: 555 });

    await user.selectOptions(screen.getByRole("combobox", { name: "Team order" }), "swapped");
    await user.click(screen.getByRole("button", { name: "Relink" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).toContain("Betclic lists our team2 first");
    await user.click(within(dialog).getByRole("button", { name: "Relink" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "POST")).toHaveLength(2));
    expect(calls.filter((c) => c.method === "POST")[1]?.body).toEqual({ action: "relink", match_id: 555, teams_swapped: true });
  });

  it("builds manual relink bodies", () => {
    expect(manualRelinkBody(7, "auto")).toEqual({ action: "relink", match_id: 7 });
    expect(manualRelinkBody(7, "same")).toEqual({ action: "relink", match_id: 7, teams_swapped: false });
    expect(manualRelinkBody(7, "swapped")).toEqual({ action: "relink", match_id: 7, teams_swapped: true });
  });
});
