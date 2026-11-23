import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { FormEvent } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { ApiError, api } from "../api/client";
import { qk } from "../api/hooks";
import { useAuth } from "../auth";
import { Loading } from "../components/ui";

/** Only same-app paths are accepted as the post-login target (no open redirect). */
export function safeNext(next: string | null): string {
  return next && next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/login") ? next : "/";
}

export function LoginPage() {
  const me = useAuth();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const next = safeNext(params.get("next"));

  if (me.data && (!me.data.auth_enabled || me.data.authenticated)) return <Navigate to={next} replace />;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await api.login({ username, password });
      qc.setQueryData(qk.me, res);
      setPassword("");
      navigate(next, { replace: true });
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 401
          ? "Wrong username or password."
          : err instanceof Error
            ? err.message
            : "Login failed",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-page">
      <form className="card login-card stack" onSubmit={(e) => void submit(e)} aria-label="Log in">
        <div className="brand" style={{ padding: 0 }}>
          <img src="/favicon.svg" alt="" className="brand-logo" />
          <span>cs2bot dashboard</span>
        </div>
        {me.isLoading ? (
          <Loading lines={2} />
        ) : (
          <>
            <div className="field">
              <label htmlFor="username">Username</label>
              <input id="username" type="text" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required />
            </div>
            <div className="field">
              <label htmlFor="password">Password</label>
              <input
                id="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                autoFocus
              />
            </div>
            {error && (
              <div className="field-error" role="alert">
                {error}
              </div>
            )}
            <button type="submit" className="btn btn-primary" disabled={busy}>
              {busy ? "Logging in…" : "Log in"}
            </button>
          </>
        )}
      </form>
    </div>
  );
}
