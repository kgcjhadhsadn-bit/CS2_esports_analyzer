import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { matchDetail, oddsHistory } from "../test/fixtures";
import { mockApi, renderWithProviders } from "../test/utils";
import MatchDetailPage from "./MatchDetailPage";

function renderPage() {
  mockApi({ "GET /api/matches/101": matchDetail, "GET /api/matches/101/odds-history": oddsHistory });
  return renderWithProviders(
    <Routes>
      <Route path="/matches/:id" element={<MatchDetailPage />} />
    </Routes>,
    { route: "/matches/101" },
  );
}

describe("MatchDetailPage", () => {
  it("renders the markets table with EV and decisions", async () => {
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByText("Match winner: Synth Team 1")).toBeInTheDocument();
    expect(screen.getByText("+37.3%")).toBeInTheDocument();
    expect(screen.getByText("Accepted")).toBeInTheDocument();
    expect(screen.getByText("stale")).toBeInTheDocument();
    await user.click(screen.getByText("Map handicap: Synth Team 1 -1.5"));
    expect(await screen.findByText("p_win 0.45 < 0.75")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Market" })).toBeInTheDocument();
  });

  it("shows the model tab without crashing", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("tab", { name: "Model" }));
    expect(await screen.findByText("Win probabilities")).toBeInTheDocument();
    expect(screen.getByText("Round distributions")).toBeInTheDocument();
    expect(screen.getByText("Player kill projections")).toBeInTheDocument();
    expect(screen.getByText(/Predicted map per slot/)).toBeInTheDocument();
    expect(screen.getAllByText("s1_awper").length).toBeGreaterThan(0);
  });

  it("renders the rationale safely and only links https evidence", async () => {
    const user = userEvent.setup();
    const { container } = renderPage();
    await user.click(await screen.findByRole("tab", { name: /Context/ }));
    const rationale = screen.getByText("Claude rationale").closest("section");
    expect(rationale).not.toBeNull();
    expect(within(rationale as HTMLElement).getByText("Team 1").tagName).toBe("STRONG");
    expect(container.querySelector("img[src='x']")).toBeNull();
    expect(rationale?.textContent).toContain("<img src=x onerror=alert(1)>");
    const links = screen.getAllByRole("link").map((a) => a.getAttribute("href"));
    expect(links).toContain("https://example.invalid/news/1");
    expect(links.some((h) => h?.startsWith("javascript"))).toBe(false);
  });
});
