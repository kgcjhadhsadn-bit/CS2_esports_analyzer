/** Tables reused on several pages. */
import { Fragment } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { BetOut, MatchBrief, MatchSummary } from "../api/types";
import { formatDateTime, formatMoney, formatOdds, formatSignedPct, signClass, teamName } from "../lib/format";
import { BetStatusBadge, CompletenessBadge, EvBadge, LinkBadge, MatchStatusBadge, TierBadge } from "./badges";
import { Badge, Empty } from "./ui";

export function MatchTitle({ match }: { match: Pick<MatchBrief, "id" | "team1" | "team2"> }) {
  return (
    <Link to={`/matches/${match.id}`}>
      <span className="team1">{teamName(match.team1, "team1")}</span>
      <span className="faint"> vs </span>
      <span className="team2">{teamName(match.team2, "team2")}</span>
    </Link>
  );
}

export function MatchesTable({ matches, compact = false }: { matches: MatchSummary[]; compact?: boolean }) {
  if (!matches.length) return <Empty>No matches.</Empty>;
  return (
    <div className="table-wrap">
      <table className={`table ${compact ? "table-compact" : ""}`}>
        <thead>
          <tr>
            <th>Start (Warsaw)</th>
            <th>Match</th>
            {!compact && <th>Tournament</th>}
            <th>Status</th>
            {!compact && <th>Tier</th>}
            <th>Betclic</th>
            <th className="num">Data</th>
            <th className="num">Best EV</th>
            <th className="num">Accepted</th>
          </tr>
        </thead>
        <tbody>
          {matches.map((m) => (
            <tr key={m.id}>
              <td className="nowrap">{formatDateTime(m.scheduled_at)}</td>
              <td>
                <MatchTitle match={m} />
                {m.format && <span className="faint small"> · {m.format.toUpperCase()}</span>}
                {m.status === "finished" && m.team1_score !== null && m.team2_score !== null && (
                  <span className="small"> · {m.team1_score}-{m.team2_score}</span>
                )}
              </td>
              {!compact && (
                <td className="small">
                  {m.tournament?.name ?? <span className="faint">—</span>}
                  {m.stage && <span className="faint"> · {m.stage}</span>}
                </td>
              )}
              <td>
                <MatchStatusBadge status={m.status} />
              </td>
              {!compact && (
                <td>
                  <TierBadge tier={m.tier} />
                </td>
              )}
              <td>
                <LinkBadge status={m.betclic?.link_status} confidence={m.betclic?.link_confidence} />
              </td>
              <td className="num">
                <CompletenessBadge value={m.data_completeness} />
              </td>
              <td className="num">
                <EvBadge ev={m.best_ev} />
              </td>
              <td className="num">
                {m.accepted_decisions > 0 ? <Badge tone="pos">{m.accepted_decisions}</Badge> : <span className="faint">0</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function BetsTable({
  bets,
  onSelect,
  selectedId,
  renderDetail,
  compact = false,
}: {
  bets: BetOut[];
  onSelect?: (id: number | null) => void;
  selectedId?: number | null;
  renderDetail?: (bet: BetOut) => ReactNode;
  compact?: boolean;
}) {
  if (!bets.length) return <Empty>No bets.</Empty>;
  const cols = compact ? 7 : 11;
  return (
    <div className="table-wrap">
      <table className={`table ${compact ? "table-compact" : ""}`}>
        <thead>
          <tr>
            <th>Placed (Warsaw)</th>
            <th>Match</th>
            <th>Selection</th>
            <th>Status</th>
            <th className="num">Odds</th>
            <th className="num">Stake</th>
            {!compact && <th className="num">EV</th>}
            {!compact && <th className="num">Closing</th>}
            {!compact && <th className="num">CLV</th>}
            <th className="num">P&amp;L</th>
            {!compact && <th />}
          </tr>
        </thead>
        <tbody>
          {bets.map((b) => {
            const selected = selectedId === b.id;
            return (
              <Fragment key={b.id}>
                <tr
                  className={`${onSelect ? "clickable" : ""} ${selected ? "expanded" : ""}`}
                  onClick={onSelect ? () => onSelect(selected ? null : b.id) : undefined}
                  aria-expanded={onSelect ? selected : undefined}
                >
                  <td className="nowrap">{formatDateTime(b.placed_at ?? b.created_at)}</td>
                  <td className="small">
                    {b.match ? <MatchTitle match={b.match} /> : <span className="faint">{b.external_event_id}</span>}
                  </td>
                  <td>
                    {b.label}
                    {b.dry_run && (
                      <>
                        {" "}
                        <Badge tone="info">paper</Badge>
                      </>
                    )}
                  </td>
                  <td>
                    <BetStatusBadge bet={b} />
                  </td>
                  <td className="num">{formatOdds(b.placed_odds ?? b.requested_odds)}</td>
                  <td className="num">{formatMoney(b.stake)}</td>
                  {!compact && <td className="num">{formatSignedPct(b.ev)}</td>}
                  {!compact && <td className="num">{formatOdds(b.closing_odds)}</td>}
                  {!compact && <td className={`num ${signClass(b.clv)}`}>{formatSignedPct(b.clv)}</td>}
                  <td className={`num ${signClass(b.pnl)}`}>{formatMoney(b.pnl, { signed: true })}</td>
                  {!compact && (
                    <td className="num faint" aria-hidden="true">
                      {b.has_screenshot ? "📷" : ""} {onSelect ? (selected ? "▾" : "▸") : ""}
                    </td>
                  )}
                </tr>
                {selected && renderDetail && (
                  <tr className="detail-row">
                    <td colSpan={cols}>{renderDetail(b)}</td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
