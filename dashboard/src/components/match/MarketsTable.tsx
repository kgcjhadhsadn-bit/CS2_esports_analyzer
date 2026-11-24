/**
 * Markets table of the match page: model probability vs Betclic odds, EV after tax, sigma and the
 * decision status, grouped by market type; a row expands to the decision reasons and details.
 */
import { Fragment, useMemo, useState } from "react";
import type { MarketRow } from "../../api/types";
import {
  formatAge,
  formatDateTime,
  formatMoney,
  formatNumber,
  formatOdds,
  formatPct,
  formatSignedPct,
  humanize,
  signClass,
} from "../../lib/format";
import { BetStatusBadge } from "../badges";
import { Badge, Empty, toggleRowProps } from "../ui";
import type { BadgeTone } from "../ui";

/** "p_win 0.45 < 0.75" or a structured reason -> display text. */
export function reasonText(reason: unknown): string {
  if (typeof reason === "string") return reason;
  if (reason === null || reason === undefined) return "";
  try {
    return JSON.stringify(reason);
  } catch {
    return String(reason);
  }
}

/** One-word decision status of an outcome (bet > decision > missing data). */
export function decisionStatus(row: MarketRow): { label: string; tone: BadgeTone; title?: string } {
  if (row.decision) {
    const reasons = row.decision.reasons.map(reasonText).filter((r) => r && r !== "ok");
    if (row.decision.accepted) return { label: "Accepted", tone: "pos", title: `decided ${formatDateTime(row.decision.decided_at)}` };
    return { label: "Skipped", tone: "neg", title: reasons.join("; ") || undefined };
  }
  if (row.p_win === null) return { label: "No model price", tone: "neutral" };
  if (row.odds === null) return { label: "No Betclic price", tone: "neutral" };
  if (row.is_available === false) return { label: "Suspended", tone: "warn" };
  return { label: "Not decided", tone: "neutral" };
}

type Filter = "all" | "priced" | "value" | "decided";

function groupLabel(row: MarketRow): string {
  const spec = row.spec ?? {};
  const map = typeof spec.map_number === "number" ? ` · map ${spec.map_number}` : "";
  return `${humanize(row.market_type)}${map}`;
}

export function MarketsTable({
  rows,
  taxRate,
  onShowHistory,
}: {
  rows: MarketRow[];
  taxRate: string;
  onShowHistory?: (row: MarketRow) => void;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("priced");
  const [marketType, setMarketType] = useState<string>("");

  const marketTypes = useMemo(() => [...new Set(rows.map((r) => r.market_type))], [rows]);
  const visible = rows.filter((r) => {
    if (marketType && r.market_type !== marketType) return false;
    if (filter === "priced") return r.p_win !== null && r.odds !== null;
    if (filter === "value") return r.ev !== null && r.ev > 0;
    if (filter === "decided") return r.decision !== null || r.bet !== null;
    return true;
  });

  if (!rows.length) return <Empty>No markets yet: no model prices and no mapped Betclic selections.</Empty>;

  let lastGroup = "";
  return (
    <div className="stack" style={{ gap: "0.75rem" }}>
      <div className="filters" style={{ marginBottom: 0 }}>
        <div className="field">
          <label htmlFor="market-filter" className="small muted">
            Show
          </label>
          <select id="market-filter" value={filter} onChange={(e) => setFilter(e.target.value as Filter)}>
            <option value="priced">Model + Betclic price</option>
            <option value="value">Positive EV only</option>
            <option value="decided">Decided / bet</option>
            <option value="all">Everything</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="market-type" className="small muted">
            Market
          </label>
          <select id="market-type" value={marketType} onChange={(e) => setMarketType(e.target.value)}>
            <option value="">All markets</option>
            {marketTypes.map((mt) => (
              <option key={mt} value={mt}>
                {humanize(mt)}
              </option>
            ))}
          </select>
        </div>
        <span className="small muted">
          EV is per 1 PLN staked after the {formatPct(Number(taxRate), 0)} stake tax; model p = P(win | not refunded).
        </span>
      </div>
      {!visible.length ? (
        <Empty>No outcome matches this filter.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Outcome</th>
                <th className="num">Model p</th>
                <th className="num">σ</th>
                <th className="num">Fair</th>
                <th className="num">Break-even</th>
                <th className="num">Betclic</th>
                <th className="num">EV</th>
                <th>Decision</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {visible.map((row) => {
                const key = `${row.spec_key}::${row.selection_key}::${row.selection_id ?? "-"}`;
                const group = groupLabel(row);
                const showGroup = group !== lastGroup;
                lastGroup = group;
                const isOpen = expanded === key;
                const status = decisionStatus(row);
                const p = row.p_win_settled ?? row.p_win;
                return (
                  <Fragment key={key}>
                    {showGroup && (
                      <tr className="group-row">
                        <td colSpan={9}>{group}</td>
                      </tr>
                    )}
                    <tr
                      className={`clickable ${isOpen ? "expanded" : ""}`}
                      {...toggleRowProps(() => setExpanded(isOpen ? null : key), isOpen)}
                    >
                      <td>{row.label}</td>
                      <td className="num" title={row.p_win !== null ? `unconditional p_win ${formatPct(row.p_win)}` : undefined}>
                        <strong>{formatPct(p)}</strong>
                      </td>
                      <td className="num">{formatNumber(row.sigma, 3)}</td>
                      <td className="num">{formatOdds(row.fair_odds)}</td>
                      <td className="num">{formatOdds(row.breakeven_odds)}</td>
                      <td className="num">
                        <span className="row" style={{ justifyContent: "flex-end", gap: "0.3rem" }}>
                          {row.odds_stale && <Badge tone="warn" title={`price age ${formatAge(row.odds_age_s)}`}>stale</Badge>}
                          {row.is_available === false && <Badge tone="warn">susp.</Badge>}
                          <strong>{formatOdds(row.odds)}</strong>
                        </span>
                      </td>
                      <td className={`num ${signClass(row.ev)}`}>
                        <strong>{formatSignedPct(row.ev)}</strong>
                      </td>
                      <td>
                        <span className="row" style={{ gap: "0.3rem" }}>
                          <Badge tone={status.tone} title={status.title}>
                            {status.label}
                          </Badge>
                          {row.bet && <BetStatusBadge bet={row.bet} />}
                        </span>
                      </td>
                      <td className="num faint" aria-hidden="true">
                        {isOpen ? "▾" : "▸"}
                      </td>
                    </tr>
                    {isOpen && (
                      <tr className="detail-row">
                        <td colSpan={9}>
                          <MarketRowDetail row={row} onShowHistory={onShowHistory} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {visible.length !== rows.length && (
        <div className="small muted">
          Showing {visible.length} of {rows.length} outcomes (filter &quot;Show&quot; / &quot;Market&quot;).
        </div>
      )}
    </div>
  );
}

function MarketRowDetail({ row, onShowHistory }: { row: MarketRow; onShowHistory?: (row: MarketRow) => void }) {
  const reasons = row.decision?.reasons.map(reasonText).filter(Boolean) ?? [];
  return (
    <div className="grid grid-3">
      <div>
        <h3>Decision</h3>
        {row.decision ? (
          <>
            <p className={row.decision.accepted ? "pos" : "neg"} style={{ margin: "0 0 0.4rem" }}>
              <strong>{row.decision.accepted ? "Accepted" : "Skipped"}</strong> · {formatDateTime(row.decision.decided_at)}
            </p>
            {reasons.length > 0 && (
              <ul style={{ margin: "0 0 0.5rem", paddingLeft: "1.1rem" }}>
                {reasons.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            )}
            <dl className="kv small">
              <dt>Odds at decision</dt>
              <dd>{formatOdds(row.decision.odds)}</dd>
              <dt>p / σ</dt>
              <dd>
                {formatPct(row.decision.p_win)} / {formatNumber(row.decision.sigma, 3)}
              </dd>
              <dt>EV</dt>
              <dd className={signClass(row.decision.ev)}>{formatSignedPct(row.decision.ev)}</dd>
              <dt>Kelly fraction</dt>
              <dd>{formatPct(row.decision.kelly_fraction, 2)}</dd>
              <dt>Proposed stake</dt>
              <dd>{formatMoney(row.decision.proposed_stake)}</dd>
            </dl>
          </>
        ) : (
          <p className="muted small">No decision recorded for this outcome yet.</p>
        )}
        {row.bet && (
          <div className="small" style={{ marginTop: "0.5rem" }}>
            Bet #{row.bet.id}: <BetStatusBadge bet={row.bet} /> {formatMoney(row.bet.stake)} @{" "}
            {formatOdds(row.bet.placed_odds ?? row.bet.requested_odds)}
            {row.bet.pnl !== null && (
              <span className={signClass(row.bet.pnl)}> · P&amp;L {formatMoney(row.bet.pnl, { signed: true })}</span>
            )}
          </div>
        )}
      </div>
      <div>
        <h3>Model</h3>
        {row.p_win !== null ? (
          <dl className="kv small">
            <dt>P(win | settled)</dt>
            <dd>{formatPct(row.p_win_settled)}</dd>
            <dt>P(win)</dt>
            <dd>{formatPct(row.p_win)}</dd>
            <dt>P(push) / P(void)</dt>
            <dd>
              {formatPct(row.p_push)} / {formatPct(row.p_void)}
            </dd>
            <dt>P(lose)</dt>
            <dd>{formatPct(row.p_lose)}</dd>
            <dt>σ</dt>
            <dd>{formatNumber(row.sigma, 3)}</dd>
            <dt>Model</dt>
            <dd>
              {row.model_version ?? "—"} · {formatDateTime(row.predicted_at)}
            </dd>
          </dl>
        ) : (
          <p className="muted small">The model has no price for this outcome.</p>
        )}
      </div>
      <div>
        <h3>Betclic</h3>
        {row.selection_id !== null ? (
          <>
            <dl className="kv small">
              <dt>Market</dt>
              <dd>{row.market_raw ?? "—"}</dd>
              <dt>Selection</dt>
              <dd>{row.selection_raw ?? "—"}</dd>
              <dt>Odds / after tax</dt>
              <dd>
                {formatOdds(row.odds)} / {formatOdds(row.effective_odds)}
              </dd>
              <dt>Captured</dt>
              <dd>
                {formatDateTime(row.odds_captured_at)} ({formatAge(row.odds_age_s)} ago)
                {row.odds_stale ? " - stale" : ""}
              </dd>
              <dt>Available</dt>
              <dd>
                {row.is_available === false ? "suspended" : "yes"}
                {row.event_is_live ? " (event live)" : ""}
              </dd>
            </dl>
            {onShowHistory && (
              <button className="btn btn-sm" style={{ marginTop: "0.5rem" }} onClick={() => onShowHistory(row)}>
                Odds history
              </button>
            )}
          </>
        ) : (
          <p className="muted small">Betclic does not offer this outcome (or it is not mapped).</p>
        )}
      </div>
    </div>
  );
}
