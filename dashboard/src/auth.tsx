/**
 * Auth gate. `GET /api/auth/me` tells whether auth is enabled (only when `api.password` is set);
 * when it is and there is no session - or any request answers 401 - the login page is shown.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { api, onUnauthorized } from "./api/client";
import { qk } from "./api/hooks";
import { ErrorState, Loading } from "./components/ui";

export function useAuth() {
  return useQuery({
    queryKey: qk.me,
    queryFn: ({ signal }) => api.me(signal),
    staleTime: 60_000,
    meta: { silent: true },
  });
}

/** Re-checks /auth/me whenever any request answers 401 (session expired / logged out). */
export function UnauthorizedWatcher() {
  const qc = useQueryClient();
  useEffect(
    () =>
      onUnauthorized(() => {
        // A 401 proves that auth is enabled now (e.g. the API restarted with a password set).
        qc.setQueryData(qk.me, { auth_enabled: true, authenticated: false, username: null });
      }),
    [qc],
  );
  return null;
}

export function RequireAuth({ children }: { children: ReactNode }) {
  const me = useAuth();
  const location = useLocation();
  if (me.data) {
    if (me.data.auth_enabled && !me.data.authenticated) {
      const next = `${location.pathname}${location.search}`;
      return <Navigate to={`/login?next=${encodeURIComponent(next)}`} replace />;
    }
    return <>{children}</>;
  }
  if (me.error) {
    return (
      <div className="login-page">
        <div className="card login-card stack">
          <h1>cs2bot</h1>
          <ErrorState error={me.error} onRetry={() => void me.refetch()} />
          <p className="muted small">
            The dashboard needs the local API on 127.0.0.1:8000 (<code>cs2bot up</code>).
          </p>
        </div>
      </div>
    );
  }
  return (
    <div className="login-page">
      <div className="login-card">
        <Loading label="Connecting to the cs2bot API…" />
      </div>
    </div>
  );
}
