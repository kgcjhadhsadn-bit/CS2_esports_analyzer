/**
 * QueryClient factory: errors are surfaced as toasts (once per message while visible), 401s are
 * left to the auth provider (it shows the login page), and client errors (4xx) are not retried.
 */
import { MutationCache, QueryCache, QueryClient } from "@tanstack/react-query";
import { ApiError } from "./client";
import { toast } from "../components/toast";

declare module "@tanstack/react-query" {
  interface Register {
    mutationMeta: { silent422?: boolean };
    queryMeta: { silent?: boolean };
  }
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Unexpected error";
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    queryCache: new QueryCache({
      onError: (error, query) => {
        if (query.meta?.silent) return;
        if (error instanceof ApiError && error.status === 401) return;
        toast.error(describe(error), "Could not load data");
      },
    }),
    mutationCache: new MutationCache({
      onError: (error, _vars, _ctx, mutation) => {
        if (error instanceof ApiError && error.status === 401) return;
        if (error instanceof ApiError && error.status === 422 && mutation.meta?.silent422) return;
        toast.error(describe(error), "Action failed");
      },
    }),
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        refetchOnWindowFocus: true,
        retry: (failureCount, error) => {
          if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
          return failureCount < 1;
        },
      },
      mutations: { retry: false },
    },
  });
}
