/**
 * Bets: history with filters and status badges, P&L, CLV; a row expands to the decision, the
 * ledger entries, the Claude rationale and the placement screenshot (/api/bets/{id}/screenshot).
 */
import { useState } from "react";
import { api } from "../api/client";
import { useBet, useBets } from "../api/hooks";
import type { BetQuery } from "../api/client";
import type { BetOut, BetStatus } from "../api/types";
import { reasonText } from "../components/match/MarketsTable";
import { BetsTable } from "../components/tables";
import { Card, Chips, ErrorState, Loading, Pager, QueryView, Segmented, Stat } from "../components/ui";
import {
  formatDateTime,
  formatMoney,
  formatNumber,
  formatOdds,
  formatPct,
  formatSignedPct,
  humanize,
  signClass,
  warsawDateToUtcIso,
} from "../lib/format";
import { SafeMarkdown } from "../lib/markdown";

type Mode = "all" | "real" | "paper";

const STATUS_OPTIONS: { value: BetStatus; label: string }[] = [
  { value: "placed", label: "Placed" },
  { value: "won", label: "Won" },
  { value: "lost", label: "Lost" },
  { value: "void", label: "Void" },
  { value: "pending", label: "Pending" },
  { value: "placing", label: "Placing" },
  { value: "failed", label: "Failed" },
  { value: "cancelled", label: "Cancelled" },
  { value: "dry_run", label: "Paper" },
];

const PAGE_SIZE = 50;

/** Query for the bets filters (exported for tests). `from` / `to` are Warsaw calendar dates. */
export function buildBetQuery(statuses: BetStatus[], mode: Mode, from: string, to: string, offset: number): BetQuery {
  return {
    status: statuses.length ? statuses : undefined,
    dry_run: mode === "all" ? undefined : mode === "paper",
    from: from ? (warsawDateToUtcIso(from) ?? undefined) : undefined,
    to: to ? (warsawDateToUtcIso(to, true) ?? undefined) : undefined,
    limit: PAGE_SIZE,
    offset,
  };
}

function BetDetailPanel({ bet }: { bet: BetOut }) {
  const detail = useBet(bet.id);
  const [imgError, setImgError] = useState(false);
  if (detail.error && !detail.data) return <ErrorState error={detail.error} onRetry={() => void detail.refetch()} />;
  if (!detail.data) return <Loading lines={3} />;
  const { decision, ledger, rationale } = detail.data;
  const b = detail.data.bet;
  return (
    <div className="grid grid-2">
      <div className="stack" style={{ gap: "0.75rem" }}>
        <div>
          <h3>Bet #{b.id}</h3>
          <dl className="kv small">
            <dt>Betclic</dt>
            <dd>
              {b.market_raw} · {b.selection_raw}
            </dd>
            <dt>Odds requested / min / placed</dt>
            <dd>
              {formatOdds(b.requested_odds)} / {formatOdds(b.min_acceptable_odds)} / {formatOdds(b.placed_odds)}
            </dd>
            <dt>Stake / tax</dt>
            <dd>
              {formatMoney(b.stake)} / {formatMoney(b.tax)}
            </dd>
            <dt>Payout / P&amp;L</dt>
            <dd>
              {formatMoney(b.payout)} / <span className={signClass(b.pnl)}>{formatMoney(b.pnl, { signed: true })}</span>
            </dd>
            <dt>Model p / EV</dt>
            <dd>
              {formatPct(b.p_win)} / {formatSignedPct(b.ev)}
            </dd>
            <dt>Closing odds / CLV</dt>
            <dd>
              {formatOdds(b.closing_odds)} / <span className={signClass(b.clv)}>{formatSignedPct(b.clv)}</span>
            </dd>
            <dt>Created / placed / settled</dt>
            <dd>
              {formatDateTime(b.created_at)} / {formatDateTime(b.placed_at)} / {formatDateTime(b.settled_at)}
            </dd>
            {b.external_bet_id && (
              <>
                <dt>Betclic bet id</dt>
                <dd className="mono">{b.external_bet_id}</dd>
              </>
            )}
            {b.error && (
              <>
                <dt>Error</dt>
                <dd className="neg">{b.error}</dd>
              </>
            )}
          </dl>
        </div>
        {decision && (
          <div>
            <h3>Decision #{decision.id}</h3>
            <p className="small" style={{ margin: "0 0 0.3rem" }}>
              {decision.accepted ? "Accepted" : "Rejected"} {formatDateTime(decision.decided_at)} at {formatOdds(decision.odds)} · p{" "}
              {formatPct(decision.p_win)} · σ {formatNumber(decision.sigma, 3)} · EV {formatSignedPct(decision.ev)} · Kelly{" "}
              {formatPct(decision.kelly_fraction, 2)}
            </p>
            {decision.reasons.length > 0 && (
              <ul className="small" style={{ margin: 0, paddingLeft: "1.1rem" }}>
                {decision.reasons.map((r, i) => (
                  <li key={i}>{reasonText(r)}</li>
                ))}
              </ul>
            )}
            {Object.keys(decision.settings_snapshot).length > 0 && (
              <details className="small" style={{ marginTop: "0.4rem" }}>
                <summary>Settings at decision time</summary>
                <pre className="json">{JSON.stringify(decision.settings_snapshot, null, 2)}</pre>
              </details>
            )}
          </div>
        )}
        {ledger.length > 0 && (
          <div>
            <h3>Ledger</h3>
            <table className="table table-compact">
              <tbody>
                {ledger.map((l) => (
                  <tr key={l.id}>
                    <td className="nowrap">{formatDateTime(l.ts)}</td>
                    <td>{humanize(l.kind)}</td>
                    <td className={`num ${signClass(l.amount)}`}>{formatMoney(l.amount, { signed: true })}</td>
                    <td className="num muted">{formatMoney(l.balance_after)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {rationale && (
          <div>
            <h3>Claude rationale</h3>
            <SafeMarkdown text={rationale} />
          </div>
        )}
      </div>
      <div>
        <h3>Placement screenshot</h3>
        {b.has_screenshot && !imgError ? (
          <a href={api.screenshotUrl(b.id)} target="_blank" rel="noopener noreferrer">
            <img
              className="screenshot"
              src={api.screenshotUrl(b.id)}
              alt={`Betclic bet slip of bet ${b.id}`}
              loading="lazy"
              onError={() => setImgError(true)}
            />
          </a>
        ) : (
          <p className="muted small">{imgError ? "The screenshot file could not be loaded." : "No screenshot for this bet."}</p>
        )}
      </div>
    </div>
  );
}

export default function BetsPage() {
  const [statuses, setStatuses] = useState<BetStatus[]>([]);
  const [mode, setMode] = useState<Mode>("all");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<number | null>(null);
  const query = useBets(buildBetQuery(statuses, mode, from, to, offset), { poll: true });
  const reset = () => setOffset(0);

  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <h1>Bets</h1>
          <p>Every bet the engine placed (real and paper). Click a row for details and the screenshot.</p>
        </div>
        <Segmented<Mode>
          label="Money"
          value={mode}
          onChange={(v) => {
            setMode(v);
            reset();
          }}
          options={[
            { value: "all", label: "All" },
            { value: "real", label: "Real money" },
            { value: "paper", label: "Paper" },
          ]}
        />
      </div>
      <Card>
        <div className="filters">
          <div className="field">
            <span className="small muted">Status / result</span>
            <Chips<BetStatus>
              label="Status"
              values={statuses}
              options={STATUS_OPTIONS}
              onChange={(v) => {
                setStatuses(v);
                reset();
              }}
            />
          </div>
          <div className="field">
            <label htmlFor="bets-from" className="small muted">
              From (Warsaw)
            </label>
            <input
              id="bets-from"
              type="date"
              value={from}
              onChange={(e) => {
                setFrom(e.target.value);
                reset();
              }}
            />
          </div>
          <div className="field">
            <label htmlFor="bets-to" className="small muted">
              To (Warsaw)
            </label>
            <input
              id="bets-to"
              type="date"
              value={to}
              onChange={(e) => {
                setTo(e.target.value);
                reset();
              }}
            />
          </div>
        </div>
        <QueryView query={query} loadingLines={6}>
          {(page) => (
            <div className="stack">
              <div className="grid grid-4">
                <Stat label="Bets" value={page.summary.count} hint={`${page.summary.open} open`} />
                <Stat label="Staked" value={formatMoney(page.summary.stake)} />
                <Stat
                  label="P&L"
                  value={formatMoney(page.summary.pnl, { signed: true })}
                  className={signClass(page.summary.pnl)}
                />
                <Stat
                  label="Won / lost / void"
                  value={`${page.summary.won} / ${page.summary.lost} / ${page.summary.void}`}
                  hint={
                    page.summary.won + page.summary.lost > 0
                      ? `hit rate ${formatPct(page.summary.won / (page.summary.won + page.summary.lost))}`
                      : undefined
                  }
                />
              </div>
              <BetsTable
                bets={page.items}
                selectedId={selected}
                onSelect={setSelected}
                renderDetail={(bet) => <BetDetailPanel bet={bet} />}
              />
              <Pager total={page.total} limit={page.limit} offset={page.offset} onChange={setOffset} />
            </div>
          )}
        </QueryView>
      </Card>
    </div>
  );
}
