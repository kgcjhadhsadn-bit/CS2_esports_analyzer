import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { UnauthorizedWatcher } from "./auth";
import { AppRoutes } from "./App";
import { safeNext } from "./pages/LoginPage";
import { betPage, health, tradingState } from "./test/fixtures";
import { mockApi, renderWithProviders } from "./test/utils";

describe("auth flow", () => {
  it("redirects to the login page when auth is on and logs in", async () => {
    const user = userEvent.setup();
    let loggedIn = false;
    const { calls } = mockApi({
      "GET /api/auth/me": () => ({ auth_enabled: true, authenticated: loggedIn, username: loggedIn ? "admin" : null }),
      "POST /api/auth/login": (call: { body: unknown }) => {
        const body = call.body as { password: string };
        if (body.password !== "secret") return { status: 401, body: { detail: "invalid credentials" } };
        loggedIn = true;
        return { auth_enabled: true, authenticated: true, username: "admin" };
      },
      "GET /api/health": health,
      "GET /api/control/state": tradingState,
      "GET /api/bets": betPage,
    });
    renderWithProviders(
      <>
        <UnauthorizedWatcher />
        <AppRoutes />
      </>,
      { route: "/bets" },
    );
    const password = await screen.findByLabelText("Password");
    await user.type(password, "wrong");
    await user.click(screen.getByRole("button", { name: "Log in" }));
    expect(await screen.findByText("Wrong username or password.")).toBeInTheDocument();
    await user.clear(password);
    await user.type(password, "secret");
    await user.click(screen.getByRole("button", { name: "Log in" }));
    expect(await screen.findByRole("heading", { name: "Bets" }, { timeout: 3000 })).toBeInTheDocument();
    const login = calls.filter((c) => c.path === "/api/auth/login").at(-1);
    expect(login?.headers["X-Requested-With"]).toBe("cs2bot");
    expect(login?.credentials).toBe("include");
  });

  it("goes straight to the app when auth is disabled, and to login after a 401", async () => {
    mockApi({
      "GET /api/auth/me": { auth_enabled: false, authenticated: false, username: null },
      "GET /api/health": health,
      "GET /api/control/state": { status: 401, body: { detail: "not authenticated" } },
      "GET /api/bankroll": { status: 401, body: { detail: "not authenticated" } },
      "GET /api/matches": { status: 401, body: { detail: "not authenticated" } },
      "GET /api/bets": { status: 401, body: { detail: "not authenticated" } },
    });
    renderWithProviders(
      <>
        <UnauthorizedWatcher />
        <AppRoutes />
      </>,
      { route: "/" },
    );
    await waitFor(() => expect(screen.getByLabelText("Password")).toBeInTheDocument(), { timeout: 3000 });
  });

  it("shows the login page right after logging out", async () => {
    // Regression: logout used to clear the whole query cache from under the /auth/me observer, so
    // nothing re-rendered and the owner stayed on the (now unauthenticated) page.
    const user = userEvent.setup();
    let loggedIn = true;
    const denied = { status: 401, body: { detail: "not authenticated" } };
    const { calls } = mockApi({
      "GET /api/auth/me": () => ({ auth_enabled: true, authenticated: loggedIn, username: loggedIn ? "admin" : null }),
      "POST /api/auth/logout": () => {
        loggedIn = false;
        return { auth_enabled: true, authenticated: false, username: null };
      },
      "GET /api/health": () => (loggedIn ? health : denied),
      "GET /api/control/state": () => (loggedIn ? tradingState : denied),
      "GET /api/bets": () => (loggedIn ? betPage : denied),
    });
    renderWithProviders(
      <>
        <UnauthorizedWatcher />
        <AppRoutes />
      </>,
      { route: "/bets" },
    );
    await user.click(await screen.findByRole("button", { name: "Log out" }));
    expect(await screen.findByLabelText("Password", undefined, { timeout: 3000 })).toBeInTheDocument();
    const logout = calls.find((c) => c.path === "/api/auth/logout");
    expect(logout?.method).toBe("POST");
    expect(logout?.headers["X-Requested-With"]).toBe("cs2bot");
  });

  it("only follows same-app next paths", () => {
    expect(safeNext("/bets?x=1")).toBe("/bets?x=1");
    expect(safeNext("//evil.example")).toBe("/");
    expect(safeNext("https://evil.example")).toBe("/");
    expect(safeNext("/login")).toBe("/");
    expect(safeNext(null)).toBe("/");
    expect(safeNext("/\\evil.example")).toBe("/");
    expect(safeNext("/\tevil")).toBe("/");
    expect(safeNext("/matches/42")).toBe("/matches/42");
  });
});
