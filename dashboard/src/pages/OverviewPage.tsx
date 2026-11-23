/**
 * Overview: bankroll and loss-limit meters, trading controls (kill switch, pause / resume),
 * health alerts (with the Betclic login action), upcoming matches and recent bets.
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import { useBankroll, useBets, useHealth, useMatches } from "../api/hooks";
import type { Bankroll, BankrollMode, LossLimit } from "../api/types";
import { AlertList } from "../components/AlertList";
import { BetsTable, MatchesTable } from "../components/tables";
import { TradingControls } from "../components/TradingControls";
import { Badge, Card, Meter, QueryView, Segmented, Stat } from "../components/ui";
import { formatDateTime, formatMoney, signClass } from "../lib/format";
import { useNow } from "../lib/useNow";

function LimitMeter({ limit, label }: { limit: LossLimit; label: string }) {
  return (
    <Meter
      label={label}
      value={limit.used_fraction}
      reached={limit.reached}
      detail={
        <>
          Realized <span className={signClass(limit.realized_pnl)}>{formatMoney(limit.realized_pnl, { signed: true })}</span> of a{" "}
          {formatMoney(limit.limit_amount)} limit · open {formatMoney(limit.open_stakes)} · can still stake{" "}
          {formatMoney(limit.stake_allowance)}
        </>
      }
    />
  );
}

function BankrollCard({ data }: { data: Bankroll }) {
  return (
    <div className="stack">
      <div className="grid grid-4">
        <Stat
          label="Balance"
          value={formatMoney(data.balance)}
          hint={data.balance_source === "paper" ? "paper bankroll" : `available ${formatMoney(data.available_balance)}`}
        />
        <Stat label="Open exposure" value={formatMoney(data.open_exposure)} hint={`${data.open_bets} open bet(s)`} />
        <Stat
          label="Today P&L"
          value={formatMoney(data.daily.realized_pnl, { signed: true })}
          className={signClass(data.daily.realized_pnl)}
          hint={`since ${formatDateTime(data.daily.period_start)}`}
        />
        <Stat
          label="This week P&L"
          value={formatMoney(data.weekly.realized_pnl, { signed: true })}
          className={signClass(data.weekly.realized_pnl)}
          hint={`since ${formatDateTime(data.weekly.period_start)}`}
        />
      </div>
      <div className="grid grid-2">
        <LimitMeter limit={data.daily} label={`Daily loss limit (${(data.daily.limit_fraction * 100).toFixed(0)}%)`} />
        <LimitMeter limit={data.weekly} label={`Weekly loss limit (${(data.weekly.limit_fraction * 100).toFixed(0)}%)`} />
      </div>
      {data.mode === "dry_run" && (
        <div className="small muted">Paper mode: loss limits are shown for information; the engine applies them to real money.</div>
      )}
    </div>
  );
}

export default function OverviewPage() {
  const health = useHealth();
  const [modeChoice, setModeChoice] = useState<BankrollMode | null>(null);
  const mode: BankrollMode = modeChoice ?? (health.data?.dry_run === false ? "real" : "dry_run");
  const bankroll = useBankroll(mode, { poll: true });
  const now = useNow(5 * 60_000);
  const from = new Date(now.getTime() - 6 * 3600_000).toISOString();
  const upcoming = useMatches({ status: ["scheduled", "live"], from, order: "asc", limit: 12 }, { poll: true });
  const recentBets = useBets({ limit: 10 }, { poll: true });
  const [onlyAccepted, setOnlyAccepted] = useState(false);

  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <h1>Overview</h1>
          <p>Everything that matters right now. Times are Europe/Warsaw.</p>
        </div>
        <div className="row">
          {health.data &&
            (health.data.dry_run ? (
              <Badge tone="info">Dry-run: bets are simulated</Badge>
            ) : (
              <Badge tone="neg">LIVE: real money</Badge>
            ))}
          <Segmented<BankrollMode>
            label="Bankroll mode"
            value={mode}
            onChange={setModeChoice}
            options={[
              { value: "real", label: "Real money" },
              { value: "dry_run", label: "Paper" },
            ]}
          />
        </div>
      </div>

      <div className="grid grid-3">
        <Card title="Bankroll" sub={mode === "real" ? "Real-money ledger" : "Paper (dry-run) bets"} className="span-2">
          <QueryView query={bankroll}>{(data) => <BankrollCard data={data} />}</QueryView>
        </Card>
        <Card title="Controls">
          <TradingControls />
        </Card>
      </div>

      <Card
        title="Alerts"
        actions={
          health.data && (
            <Link to="/health" className="small">
              System health ›
            </Link>
          )
        }
      >
        <QueryView query={health}>{(data) => <AlertList alerts={data.alerts} />}</QueryView>
      </Card>

      <Card
        title="Next matches"
        sub="Scheduled and live matches; accepted = outcomes the engine decided to bet"
        actions={
          <>
            <label className="row small">
              <input type="checkbox" checked={onlyAccepted} onChange={(e) => setOnlyAccepted(e.target.checked)} /> Only with
              accepted bets
            </label>
            <Link to="/matches" className="small">
              All matches ›
            </Link>
          </>
        }
      >
        <QueryView query={upcoming}>
          {(page) => (
            <MatchesTable compact matches={onlyAccepted ? page.items.filter((m) => m.accepted_decisions > 0) : page.items} />
          )}
        </QueryView>
      </Card>

      <Card
        title="Recent bets"
        actions={
          <Link to="/bets" className="small">
            All bets ›
          </Link>
        }
      >
        <QueryView query={recentBets}>{(page) => <BetsTable compact bets={page.items} />}</QueryView>
      </Card>
    </div>
  );
}
