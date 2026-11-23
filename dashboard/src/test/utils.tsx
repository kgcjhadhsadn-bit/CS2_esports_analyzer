/** Test helpers: a routed fetch mock for /api and a render wrapper with QueryClient + router. */
import { QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { createQueryClient } from "../api/queryClient";
import { ROUTER_FUTURE } from "../App";
import { Toaster } from "../components/toast";

export interface RecordedCall {
  method: string;
  path: string;
  search: URLSearchParams;
  headers: Record<string, string>;
  credentials: RequestCredentials | undefined;
  body: unknown;
}

export type RouteHandler = (call: RecordedCall) => { status?: number; body: unknown } | unknown;

/**
 * Replace `fetch` with a router: keys are "METHOD /api/path" (query ignored). A handler returns
 * either a plain body (status 200) or `{status, body}`. Unknown routes answer 404.
 */
export function mockApi(routes: Record<string, RouteHandler | unknown>) {
  const calls: RecordedCall[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://127.0.0.1:8000");
    const method = (init?.method ?? "GET").toUpperCase();
    const headers = Object.fromEntries(Object.entries((init?.headers ?? {}) as Record<string, string>));
    const call: RecordedCall = {
      method,
      path: url.pathname,
      search: url.searchParams,
      headers,
      credentials: init?.credentials,
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
    };
    calls.push(call);
    const route = routes[`${method} ${url.pathname}`];
    if (route === undefined) {
      return new Response(JSON.stringify({ detail: "not found" }), { status: 404 });
    }
    const result = typeof route === "function" ? (route as RouteHandler)(call) : route;
    const isEnvelope = typeof result === "object" && result !== null && "body" in result && ("status" in result || Object.keys(result).length === 1);
    const status = isEnvelope ? ((result as { status?: number }).status ?? 200) : 200;
    const body = isEnvelope ? (result as { body: unknown }).body : result;
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

export function renderWithProviders(ui: ReactElement, { route = "/" }: { route?: string } = {}) {
  const queryClient = createQueryClient();
  queryClient.setDefaultOptions({
    queries: { ...queryClient.getDefaultOptions().queries, retry: false, refetchInterval: false, refetchOnWindowFocus: false },
  });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]} future={ROUTER_FUTURE}>
        {ui}
        <Toaster />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...result, queryClient };
}
