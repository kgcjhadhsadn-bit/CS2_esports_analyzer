/** App shell: sidebar navigation, global trading banners and the routed page. */
import { useQueryClient } from "@tanstack/react-query";
import { NavLink, Outlet } from "react-router-dom";
import { api } from "../api/client";
import { qk, useHealth, useTradingState } from "../api/hooks";
import type { AuthMe } from "../api/types";
import { useAuth } from "../auth";
import { toast } from "./toast";
import { Badge } from "./ui";

const NAV: { to: string; label: string; icon: string }[] = [
  { to: "/", label: "Overview", icon: "◎" },
  { to: "/matches", label: "Matches", icon: "⚔" },
  { to: "/bets", label: "Bets", icon: "₿" },
  { to: "/performance", label: "Performance", icon: "↗" },
  { to: "/review", label: "Review", icon: "⇄" },
  { to: "/settings", label: "Settings", icon: "⚙" },
  { to: "/health", label: "Health", icon: "♥" },
];

function StatusDot({ status }: { status: "ok" | "warning" | "critical" | undefined }) {
  const cls = status === "ok" ? "dot-pos" : status === "warning" ? "dot-warn" : status === "critical" ? "dot-neg" : "";
  return <span className={`dot ${cls}`} aria-label={`system status ${status ?? "unknown"}`} />;
}

export function Layout() {
  const health = useHealth();
  const trading = useTradingState();
  const me = useAuth();
  const qc = useQueryClient();
  const pending = health.data?.pending_link_reviews ?? 0;
  const t = trading.data ?? health.data?.trading ?? null;

  // The /auth/me observer must see the change: `qc.clear()` would drop that query from under the
  // mounted observer (nothing would re-render), so set the logged-out state on it directly and
  // only drop the other cached data.
  const logout = async () => {
    let next: AuthMe;
    try {
      next = await api.logout();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Logout failed", "Action failed");
      return;
    }
    qc.removeQueries({ predicate: (q) => q.queryKey[0] !== qk.me[0] });
    qc.setQueryData(qk.me, { ...next, authenticated: false, username: null });
  };

  return (
    <div className="app">
      <aside className="sidebar" aria-label="Main navigation">
        <div className="brand">
          <img src="/favicon.svg" alt="" className="brand-logo" />
          <span className="brand-name">cs2bot</span>
        </div>
        <nav className="stack" style={{ gap: "0.15rem" }}>
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}
              title={item.label}
            >
              <span className="row" style={{ gap: "0.55rem" }}>
                <span aria-hidden="true">{item.icon}</span>
                <span className="nav-label">{item.label}</span>
              </span>
              {item.to === "/review" && pending > 0 && <Badge tone="warn">{pending}</Badge>}
              {item.to === "/health" && <StatusDot status={health.data?.status} />}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          {health.data && (
            <div className="row">
              {health.data.dry_run ? <Badge tone="info">DRY RUN</Badge> : <Badge tone="neg">LIVE MONEY</Badge>}
            </div>
          )}
          {me.data?.auth_enabled && me.data.authenticated && (
            <div className="row-between hide-narrow">
              <span>{me.data.username}</span>
              <button className="btn btn-sm btn-ghost" onClick={() => void logout()}>
                Log out
              </button>
            </div>
          )}
          {health.data && <span className="faint hide-narrow small">v{health.data.version}</span>}
        </div>
      </aside>
      <main className="main" id="main">
        {t?.halted && (
          <div className="banner banner-halted" role="alert">
            <span aria-hidden="true">■</span>
            Trading halted{t.reason ? `: ${t.reason}` : ""}
          </div>
        )}
        <Outlet />
      </main>
    </div>
  );
}
