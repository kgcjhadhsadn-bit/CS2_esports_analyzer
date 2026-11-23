import { describe, expect, it, vi } from "vitest";
import { mockApi } from "../test/utils";
import { api, ApiError, apiRequest, buildQuery, errorMessage, onUnauthorized } from "./client";

describe("buildQuery", () => {
  it("repeats list values and skips empty ones", () => {
    expect(buildQuery({ status: ["won", "lost"], dry_run: false, from: "", match_id: undefined, limit: 50 })).toBe(
      "?status=won&status=lost&dry_run=false&limit=50",
    );
    expect(buildQuery({})).toBe("");
    expect(buildQuery({ status: [] })).toBe("");
  });
});

describe("apiRequest", () => {
  it("sends credentials, JSON and the CSRF header on mutations", async () => {
    const { calls } = mockApi({ "POST /api/control/kill-switch": { on: true, reason: "x", updated_at: null, updated_by: null } });
    const res = await api.setKillSwitch(true, "manual stop");
    expect(res.on).toBe(true);
    expect(calls[0]?.credentials).toBe("include");
    expect(calls[0]?.headers["X-Requested-With"]).toBe("cs2bot");
    expect(calls[0]?.headers["Content-Type"]).toBe("application/json");
    expect(calls[0]?.body).toEqual({ on: true, reason: "manual stop" });
  });

  it("puts list filters in the query string", async () => {
    const { calls } = mockApi({ "GET /api/bets": { total: 0, limit: 50, offset: 0, items: [], summary: {} } });
    await api.bets({ status: ["won", "lost"], dry_run: true, limit: 50 });
    expect(calls[0]?.search.getAll("status")).toEqual(["won", "lost"]);
    expect(calls[0]?.search.get("dry_run")).toBe("true");
    expect(calls[0]?.headers["Content-Type"]).toBeUndefined();
  });

  it("turns error bodies into ApiError messages", async () => {
    mockApi({
      "PUT /api/control/config": {
        status: 422,
        body: { detail: [{ loc: ["body", "bankroll", "kelly_fraction"], msg: "must be in (0, 1]", type: "value_error" }] },
      },
      "POST /api/control/betclic-login": { status: 409, body: { detail: "a login window is already open" } },
    });
    const err = await api.putConfig({ bankroll: { kelly_fraction: 2 } }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(422);
    expect((err as ApiError).message).toBe("bankroll.kelly_fraction: must be in (0, 1]");
    expect((err as ApiError).validationErrors).toHaveLength(1);
    await expect(api.startBetclicLogin()).rejects.toThrow("a login window is already open");
  });

  it("notifies listeners on 401", async () => {
    mockApi({ "GET /api/health": { status: 401, body: { detail: "not authenticated" } } });
    const listener = vi.fn();
    const off = onUnauthorized(listener);
    await expect(api.health()).rejects.toMatchObject({ status: 401 });
    expect(listener).toHaveBeenCalledTimes(1);
    off();
  });

  it("reports an unreachable API", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));
    await expect(apiRequest("GET", "/ping")).rejects.toMatchObject({ status: 0 });
  });

  it("has readable fallbacks", () => {
    expect(errorMessage(503, null, "x")).toBe("Database unavailable");
    expect(errorMessage(418, null, "HTTP 418")).toBe("HTTP 418");
  });
});
