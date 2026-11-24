/**
 * Performance: equity curve, drawdown, P&L by period, ROI / hit rate / CLV by market type and the
 * calibration reliability diagram (with the diagonal and 95 % Wilson intervals) plus Brier by month.
 */
import { useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ErrorBar,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";
import { useBankroll, useCalibration, useHealth, useMarketPerformance } from "../api/hooks";
import type { Bankroll, BankrollMode, CalibrationSet, MarketPerf, PeriodPnl } from "../api/types";
import { axisProps, ChartTooltip, useChartColors } from "../components/charts";
import { Card, Empty, QueryView, Segmented, Stat } from "../components/ui";
import {
  formatDate,
  formatDateTime,
  formatMoney,
  formatNumber,
  formatOdds,
  formatPct,
  formatShortDate,
  formatSignedPct,
  humanize,
  signClass,
  toNumber,
} from "../lib/format";

type Period = "day" | "week" | "month";

/** Drawdown series from the cumulative realized P&L curve (0 at a peak, negative below it). */
export function drawdownSeries(points: { ts: string; cumulative_pnl: string }[]): { t: number; dd: number; pnl: number }[] {
  let peak = 0;
  const out: { t: number; dd: number; pnl: number }[] = [];
  for (const p of points) {
    const pnl = toNumber(p.cumulative_pnl);
    const t = new Date(p.ts).getTime();
    if (pnl === null || !Number.isFinite(t)) continue;
    peak = Math.max(peak, pnl);
    out.push({ t, pnl, dd: pnl - peak });
  }
  return out;
}

function StatsGrid({ data }: { data: Bankroll }) {
  const s = data.stats;
  return (
    <div className="grid grid-4">
      <Stat
        label="Realized P&L"
        value={formatMoney(s.realized_pnl, { signed: true })}
        className={signClass(s.realized_pnl)}
        hint={`turnover ${formatMoney(s.turnover)}`}
      />
      <Stat label="ROI" value={formatSignedPct(s.roi, 2)} className={signClass(s.roi)} hint={`on capital ${formatMoney(s.capital_base)}`} />
      <Stat label="Yield" value={formatSignedPct(s.yield_rate, 2)} className={signClass(s.yield_rate)} hint="P&L / turnover" />
      <Stat
        label="Hit rate"
        value={formatPct(s.hit_rate)}
        hint={`${s.won} won · ${s.lost} lost · ${s.void} void · avg odds ${formatOdds(s.avg_odds)}`}
      />
      <Stat label="Avg CLV" value={formatSignedPct(s.avg_clv, 2)} className={signClass(s.avg_clv)} hint={`${s.clv_bets} bet(s) with closing odds`} />
      <Stat
        label="Max drawdown"
        value={formatMoney(s.max_drawdown)}
        className={toNumber(s.max_drawdown) ? "neg" : ""}
        hint={s.max_drawdown_pct !== null ? `${formatPct(s.max_drawdown_pct)} of capital + peak` : undefined}
      />
      <Stat label="Balance" value={formatMoney(data.balance)} hint={`as of ${formatDateTime(data.as_of)}`} />
      <Stat label="Bets" value={s.bets} hint={`${s.settled_bets} settled · ${s.open_bets} open`} />
    </div>
  );
}

function EquityChart({ data }: { data: Bankroll }) {
  const colors = useChartColors();
  const rows = data.equity_curve
    .map((p) => ({ t: new Date(p.ts).getTime(), balance: toNumber(p.balance), kind: p.kind, amount: p.amount }))
    .filter((p) => Number.isFinite(p.t) && p.balance !== null);
  if (rows.length < 2) return <Empty>Not enough history for an equity curve yet.</Empty>;
  return (
    <div className="chart-box" role="img" aria-label="Equity curve">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
          <CartesianGrid stroke={colors.grid} />
          <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tickFormatter={(t: number) => formatShortDate(new Date(t))} {...axisProps(colors)} />
          <YAxis domain={["auto", "auto"]} tickFormatter={(v: number) => v.toFixed(0)} width={56} {...axisProps(colors)} />
          <Tooltip
            content={
              <ChartTooltip<(typeof rows)[number]>
                render={(d) => (
                  <>
                    <div>{formatDateTime(new Date(d.t))}</div>
                    <strong>{formatMoney(d.balance)}</strong>
                    {d.kind && (
                      <div className="muted">
                        {humanize(d.kind)} {formatMoney(d.amount, { signed: true })}
                      </div>
                    )}
                  </>
                )}
              />
            }
          />
          <Line type="stepAfter" dataKey="balance" stroke={colors.series[0]} strokeWidth={2} dot={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function DrawdownChart({ data }: { data: Bankroll }) {
  const colors = useChartColors();
  const rows = useMemo(() => drawdownSeries(data.pnl_curve), [data.pnl_curve]);
  if (rows.length < 2) return <Empty>Not enough settled bets for a drawdown chart yet.</Empty>;
  return (
    <div className="chart-box short" role="img" aria-label="Drawdown">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={rows} margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
          <CartesianGrid stroke={colors.grid} />
          <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tickFormatter={(t: number) => formatShortDate(new Date(t))} {...axisProps(colors)} />
          <YAxis tickFormatter={(v: number) => v.toFixed(0)} width={56} {...axisProps(colors)} />
          <Tooltip
            content={
              <ChartTooltip<(typeof rows)[number]>
                render={(d) => (
                  <>
                    <div>{formatDateTime(new Date(d.t))}</div>
                    <div>
                      Drawdown <strong className="neg">{formatMoney(d.dd)}</strong>
                    </div>
                    <div className="muted">cumulative P&amp;L {formatMoney(d.pnl, { signed: true })}</div>
                  </>
                )}
              />
            }
          />
          <Area type="stepAfter" dataKey="dd" stroke={colors.neg} fill={colors.neg} fillOpacity={0.25} isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

function PeriodChart({ rows }: { rows: PeriodPnl[] }) {
  const colors = useChartColors();
  const data = rows.map((r) => ({ ...r, value: toNumber(r.pnl) ?? 0 }));
  if (!data.length) return <Empty>No settled bets in any period yet.</Empty>;
  return (
    <div className="chart-box" role="img" aria-label="P&L by period">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
          <CartesianGrid stroke={colors.grid} vertical={false} />
          <XAxis dataKey="period" {...axisProps(colors)} />
          {/* Bars grow from 0, so 0 must always be inside the domain (all-negative weeks otherwise
              start the axis at the smallest loss and the bars look tiny). */}
          <YAxis
            domain={[(min: number) => Math.min(0, Math.floor(min)), (max: number) => Math.max(0, Math.ceil(max))]}
            tickFormatter={(v: number) => v.toFixed(0)}
            width={56}
            {...axisProps(colors)}
          />
          <ReferenceLine y={0} stroke={colors.axis} />
          <Tooltip
            cursor={{ fill: colors.grid, opacity: 0.4 }}
            content={
              <ChartTooltip<(typeof data)[number]>
                render={(d) => (
                  <>
                    <strong>{d.period}</strong> (from {formatDate(d.start)})
                    <div className={signClass(d.pnl)}>P&amp;L {formatMoney(d.pnl, { signed: true })}</div>
                    <div className="muted">
                      {d.bets} bets · {d.won}W {d.lost}L {d.void}V · staked {formatMoney(d.staked)} · yield {formatSignedPct(d.yield_rate)}
                    </div>
                  </>
                )}
              />
            }
          />
          <Bar dataKey="value" isAnimationActive={false}>
            {data.map((d) => (
              <Cell key={d.period} fill={d.value >= 0 ? colors.pos : colors.neg} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function MarketsPerfTable({ data }: { data: MarketPerf }) {
  if (!data.items.length) return <Empty>No bets in this mode yet.</Empty>;
  const rows = [...data.items, data.total];
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>Market</th>
            <th className="num">Bets</th>
            <th className="num">Open</th>
            <th className="num">W / L / V</th>
            <th className="num">Hit rate</th>
            <th className="num">Staked</th>
            <th className="num">P&amp;L</th>
            <th className="num">ROI</th>
            <th className="num">Avg odds</th>
            <th className="num">Avg EV</th>
            <th className="num">Avg CLV</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.market_type} style={i === rows.length - 1 ? { fontWeight: 700 } : undefined}>
              <td>{r.market_type === "all" ? "All markets" : humanize(r.market_type)}</td>
              <td className="num">{r.bets}</td>
              <td className="num">{r.open}</td>
              <td className="num">
                {r.won} / {r.lost} / {r.void}
              </td>
              <td className="num">{formatPct(r.hit_rate)}</td>
              <td className="num">{formatMoney(r.staked)}</td>
              <td className={`num ${signClass(r.pnl)}`}>{formatMoney(r.pnl, { signed: true })}</td>
              <td className={`num ${signClass(r.roi)}`}>{formatSignedPct(r.roi)}</td>
              <td className="num">{formatOdds(r.avg_odds)}</td>
              <td className="num">{formatSignedPct(r.avg_ev)}</td>
              <td className={`num ${signClass(r.avg_clv)}`}>{formatSignedPct(r.avg_clv, 2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Reliability diagram points: one per non-empty bin with its Wilson interval as y error. */
export function reliabilityPoints(set: CalibrationSet) {
  return set.bins
    .filter((b) => b.count > 0 && b.mean_predicted !== null && b.observed_rate !== null)
    .map((b) => {
      const y = b.observed_rate ?? 0;
      return {
        x: b.mean_predicted ?? 0,
        y,
        count: b.count,
        err: [Math.max(0, y - (b.ci_low ?? y)), Math.max(0, (b.ci_high ?? y) - y)] as [number, number],
        lower: b.lower,
        upper: b.upper,
      };
    });
}

function ReliabilityDiagram({ set }: { set: CalibrationSet }) {
  const colors = useChartColors();
  const points = reliabilityPoints(set);
  return (
    <div className="chart-box tall" role="img" aria-label="Calibration reliability diagram">
      <ResponsiveContainer width="100%" height="100%">
        <ScatterChart margin={{ top: 12, right: 20, bottom: 20, left: 8 }}>
          <CartesianGrid stroke={colors.grid} />
          <XAxis
            dataKey="x"
            type="number"
            domain={[0, 1]}
            ticks={[0, 0.2, 0.4, 0.6, 0.8, 1]}
            tickFormatter={(v: number) => formatPct(v, 0)}
            {...axisProps(colors)}
            label={{ value: "predicted probability", position: "insideBottom", offset: -12, fill: colors.axis, fontSize: 12 }}
          />
          <YAxis
            dataKey="y"
            type="number"
            domain={[0, 1]}
            ticks={[0, 0.2, 0.4, 0.6, 0.8, 1]}
            tickFormatter={(v: number) => formatPct(v, 0)}
            width={48}
            {...axisProps(colors)}
          />
          <ZAxis dataKey="count" range={[40, 400]} />
          <ReferenceLine
            segment={[
              { x: 0, y: 0 },
              { x: 1, y: 1 },
            ]}
            stroke={colors.axis}
            strokeDasharray="5 4"
            ifOverflow="extendDomain"
          />
          <Tooltip
            content={
              <ChartTooltip<(typeof points)[number]>
                render={(d) => (
                  <>
                    <strong>
                      bin {formatPct(d.lower, 0)}–{formatPct(d.upper, 0)}
                    </strong>
                    <div>predicted {formatPct(d.x)}</div>
                    <div>
                      observed {formatPct(d.y)} (95% {formatPct(d.y - d.err[0], 0)}–{formatPct(d.y + d.err[1], 0)})
                    </div>
                    <div className="muted">{d.count} sample(s)</div>
                  </>
                )}
              />
            }
          />
          <Scatter data={points} fill={colors.series[0]} isAnimationActive={false}>
            <ErrorBar dataKey="err" direction="y" stroke={colors.series[0]} width={4} />
          </Scatter>
        </ScatterChart>
      </ResponsiveContainer>
    </div>
  );
}

function BrierChart({ set }: { set: CalibrationSet }) {
  const colors = useChartColors();
  if (set.brier_by_month.length < 1) return <Empty>No monthly Brier scores yet.</Empty>;
  return (
    <div className="chart-box short" role="img" aria-label="Brier score by month">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={set.brier_by_month} margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
          <CartesianGrid stroke={colors.grid} />
          <XAxis dataKey="period" {...axisProps(colors)} />
          <YAxis domain={[0, "auto"]} tickFormatter={(v: number) => v.toFixed(2)} width={44} {...axisProps(colors)} />
          <ReferenceLine y={0.25} stroke={colors.axis} strokeDasharray="4 3" label={{ value: "coin flip", fill: colors.axis, fontSize: 11 }} />
          <Tooltip
            content={
              <ChartTooltip<{ period: string; n: number; brier: number }>
                render={(d) => (
                  <>
                    <strong>{d.period}</strong>: Brier {formatNumber(d.brier, 3)} ({d.n} samples)
                  </>
                )}
              />
            }
          />
          <Line dataKey="brier" stroke={colors.series[3]} strokeWidth={2} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function CalibrationSection({ mode }: { mode: BankrollMode }) {
  const query = useCalibration(mode, 10);
  const [source, setSource] = useState<string>("predictions");
  return (
    <Card
      title="Calibration"
      sub="Won / lost outcomes vs P(win | not refunded); points on the dashed diagonal are perfectly calibrated"
    >
      <QueryView query={query}>
        {(data) => {
          const options = [
            { value: "predictions", label: "All predictions" },
            { value: "bets", label: "Bets" },
            ...Object.keys(data.predictions_by_market).map((k) => ({ value: `market:${k}`, label: humanize(k) })),
          ];
          const set =
            source === "bets"
              ? data.bets
              : source.startsWith("market:")
                ? (data.predictions_by_market[source.slice(7)] ?? data.predictions)
                : data.predictions;
          return (
            <div className="stack">
              <div className="row-between">
                <select aria-label="Calibration sample" value={source} onChange={(e) => setSource(e.target.value)}>
                  {options.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
                <span className="small muted">
                  n {set.n} · Brier {formatNumber(set.brier, 3)} · log loss {formatNumber(set.log_loss, 3)} · ECE{" "}
                  {formatNumber(set.ece, 3)} · base rate {formatPct(set.base_rate)} · mean predicted {formatPct(set.mean_predicted)}
                </span>
              </div>
              {set.n === 0 ? (
                <Empty>No settled samples yet.</Empty>
              ) : (
                <div className="grid grid-2">
                  <ReliabilityDiagram set={set} />
                  <div>
                    <h3>Brier score by month</h3>
                    <BrierChart set={set} />
                  </div>
                </div>
              )}
            </div>
          );
        }}
      </QueryView>
    </Card>
  );
}

export default function PerformancePage() {
  const health = useHealth();
  // Like the overview: default to the mode the bot runs in (paper while dry-run is on).
  const [modeChoice, setModeChoice] = useState<BankrollMode | null>(null);
  const mode: BankrollMode = modeChoice ?? (health.data?.dry_run === false ? "real" : "dry_run");
  const [period, setPeriod] = useState<Period>("week");
  const bankroll = useBankroll(mode);
  const markets = useMarketPerformance(mode);

  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <h1>Performance</h1>
          <p>Results of settled bets; calibration of the model on finished matches.</p>
        </div>
        <Segmented<BankrollMode>
          label="Money"
          value={mode}
          onChange={setModeChoice}
          options={[
            { value: "real", label: "Real money" },
            { value: "dry_run", label: "Paper" },
          ]}
        />
      </div>
      <QueryView query={bankroll} loadingLines={5}>
        {(data) => (
          <>
            <Card>
              <StatsGrid data={data} />
            </Card>
            <div className="grid grid-2">
              <Card title="Equity curve" sub={data.mode === "real" ? "Ledger balance" : "Paper bankroll"}>
                <EquityChart data={data} />
              </Card>
              <Card title="Drawdown" sub="Cumulative realized P&L below its running peak">
                <DrawdownChart data={data} />
              </Card>
            </div>
            <Card
              title="P&L by period"
              sub="Settled bets by Warsaw settlement date"
              actions={
                <Segmented<Period>
                  label="Period"
                  value={period}
                  onChange={setPeriod}
                  options={[
                    { value: "day", label: "Day" },
                    { value: "week", label: "Week" },
                    { value: "month", label: "Month" },
                  ]}
                />
              }
            >
              <PeriodChart rows={period === "day" ? data.pnl_by_day : period === "week" ? data.pnl_by_week : data.pnl_by_month} />
            </Card>
          </>
        )}
      </QueryView>
      <Card title="By market type" sub="ROI = P&L / staked; CLV = closing-line value">
        <QueryView query={markets}>{(data) => <MarketsPerfTable data={data} />}</QueryView>
      </Card>
      <CalibrationSection mode={mode} />
    </div>
  );
}
