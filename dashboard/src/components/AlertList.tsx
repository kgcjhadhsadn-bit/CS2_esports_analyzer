/** Health alerts with inline actions (Betclic login, link review). */
import { Link } from "react-router-dom";
import type { HealthAlert } from "../api/types";
import { AlertLevelBadge } from "./badges";
import { BetclicLoginButton } from "./TradingControls";
import { Empty } from "./ui";

export function AlertList({ alerts }: { alerts: HealthAlert[] }) {
  if (!alerts.length) return <Empty>No alerts - everything looks fine.</Empty>;
  return (
    <div>
      {alerts.map((a, i) => (
        <div key={`${a.code}-${i}`} className={`alert alert-${a.level}`} role={a.level === "critical" ? "alert" : undefined}>
          <AlertLevelBadge level={a.level} />
          <div className="alert-body">
            <div>{a.message}</div>
            <div className="alert-code">{a.code}</div>
          </div>
          {a.code === "betclic_login_required" && <BetclicLoginButton compact />}
          {a.code === "link_review_pending" && (
            <Link className="btn btn-sm" to="/review">
              Review
            </Link>
          )}
        </div>
      ))}
    </div>
  );
}
