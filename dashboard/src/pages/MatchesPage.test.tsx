import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { matchPage } from "../test/fixtures";
import { mockApi, renderWithProviders } from "../test/utils";
import MatchesPage, { buildMatchQuery } from "./MatchesPage";

describe("MatchesPage", () => {
  it("queries upcoming Tier 1/2 matches and filters tiers on the page", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi({ "GET /api/matches": matchPage });
    renderWithProviders(<MatchesPage />);
    expect(await screen.findByText("Synth Team 3")).toBeInTheDocument();
    const first = calls[0];
    expect(first?.search.getAll("status")).toEqual(["scheduled", "live"]);
    expect(first?.search.get("in_scope")).toBe("true");
    expect(first?.search.get("order")).toBe("asc");
    expect(screen.getByText("EV +37.3%")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "S" }));
    await waitFor(() => expect(screen.queryByText("Synth Team 3")).toBeNull());
    expect(screen.getByText(/Showing 1 of 2 matches/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Not linked" }));
    await waitFor(() => expect(calls.at(-1)?.search.get("linked")).toBe("false"));
  });

  it("builds past / all queries", () => {
    const now = new Date("2026-11-20T12:00:00Z");
    expect(buildMatchQuery("past", ["finished"], "any", "any", now, 50)).toMatchObject({
      status: ["finished"],
      to: "2026-11-20T12:00:00.000Z",
      order: "desc",
      offset: 50,
      linked: undefined,
    });
    expect(buildMatchQuery("all", [], "yes", "no", now, 0)).toMatchObject({ order: "desc", linked: true, in_scope: false });
  });
});
