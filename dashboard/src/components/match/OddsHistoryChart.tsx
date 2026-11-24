/**
 * Betclic odds history of a match. Snapshots are written only when the price changes, so each
 * series is drawn as a step line. Series are grouped by market (spec key) so both sides of a market
 * can be compared; the market of the selection picked in the markets table is preselected.
 */
import { useMemo } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { OddsSeries } from "../../api/types";
import { formatDateTime, formatOdds, formatShortDate, formatTime, specKeyLabel, toNumber } from "../../lib/format";
import { axisProps, useChartColors } from "../charts";
import { Empty } from "../ui";

export interface OddsGroup {
  key: string;
  label: string;
  series: OddsSeries[];
}

/** Group series by spec key (unmapped ones by their raw market name). */
export function groupOddsSeries(series: OddsSeries[]): OddsGroup[] {
  const groups = new Map<string, OddsGroup>();
  for (const s of series) {
    const key = s.spec_key ?? `raw:${s.bookmaker_event_id}:${s.market_raw}`;
    const label = s.spec_key ? specKeyLabel(s.spec_key) : `${s.market_raw} (unmapped)`;
    const g = groups.get(key) ?? { key, label, series: [] };
    g.series.push(s);
    groups.set(key, g);
  }
  return [...groups.values()];
}

interface Row {
  t: number;
  [seriesKey: string]: number | null;
}

/**
 * Merge the step series on one time axis (each value carried forward until it changes).
 *
 * Snapshots are stored only when a price changes, so the last price is still valid after its
 * snapshot: with `until` (ms, e.g. now or the match start) a final row carries every series
 * forward to that instant. Without it a price that never changed (one snapshot) would draw nothing.
 */
export function mergeSeries(series: OddsSeries[], until?: number | null): { rows: Row[]; keys: string[] } {
  const keys = series.map((s) => `s${s.selection_id}`);
  const times = new Set<number>();
  const points = series.map((s) =>
    s.points
      .map((p) => ({ t: new Date(p.captured_at).getTime(), v: p.is_available ? toNumber(p.odds) : null }))
      .filter((p) => Number.isFinite(p.t))
      .sort((a, b) => a.t - b.t),
  );
  points.forEach((ps) => ps.forEach((p) => times.add(p.t)));
  const sorted = [...times].sort((a, b) => a - b);
  const lastT = sorted.at(-1);
  if (until !== null && until !== undefined && Number.isFinite(until) && lastT !== undefined && until > lastT) {
    sorted.push(until);
  }
  const idx = points.map(() => 0);
  const last: (number | null)[] = points.map(() => null);
  const rows: Row[] = sorted.map((t) => {
    const row: Row = { t };
    points.forEach((ps, i) => {
      while ((idx[i] ?? 0) < ps.length && (ps[idx[i] ?? 0]?.t ?? Infinity) <= t) {
        last[i] = ps[idx[i] ?? 0]?.v ?? null;
        idx[i] = (idx[i] ?? 0) + 1;
      }
      row[keys[i] ?? `s${i}`] = last[i] ?? null;
    });
    return row;
  });
  return { rows, keys };
}

export function OddsHistoryChart({
  groups,
  selectedKey,
  onSelect,
  until,
}: {
  groups: OddsGroup[];
  selectedKey: string | null;
  onSelect: (key: string) => void;
  /** Carry the last prices forward to this instant (ms): now, or the match start once it began. */
  until?: number | null;
}) {
  const colors = useChartColors();
  const group = groups.find((g) => g.key === selectedKey) ?? groups[0];
  const merged = useMemo(() => (group ? mergeSeries(group.series, until) : { rows: [], keys: [] }), [group, until]);
  if (!groups.length || !group) return <Empty>No Betclic odds recorded for this match yet.</Empty>;
  const spanMs = merged.rows.length > 1 ? (merged.rows.at(-1)?.t ?? 0) - (merged.rows[0]?.t ?? 0) : 0;
  const tickFormat = (t: number) => (spanMs > 36 * 3600_000 ? formatShortDate(new Date(t)) : formatTime(new Date(t)));
  return (
    <div className="stack" style={{ gap: "0.5rem" }}>
      <div className="field" style={{ maxWidth: 420 }}>
        <label htmlFor="odds-market" className="small muted">
          Odds history of
        </label>
        <select id="odds-market" value={group.key} onChange={(e) => onSelect(e.target.value)}>
          {groups.map((g) => (
            <option key={g.key} value={g.key}>
              {g.label}
            </option>
          ))}
        </select>
      </div>
      <div className="chart-box tall" role="img" aria-label={`Odds history: ${group.label}`}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={merged.rows} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
            <CartesianGrid stroke={colors.grid} />
            <XAxis
              dataKey="t"
              type="number"
              scale="time"
              domain={["dataMin", "dataMax"]}
              tickFormatter={tickFormat}
              {...axisProps(colors)}
            />
            <YAxis domain={["auto", "auto"]} tickFormatter={(v: number) => v.toFixed(2)} width={48} {...axisProps(colors)} />
            <Tooltip
              labelFormatter={(t: number) => formatDateTime(new Date(t))}
              formatter={(value: number | string) => formatOdds(value as number)}
              contentStyle={{ background: colors.surface, borderColor: colors.grid }}
            />
            <Legend />
            {group.series.map((s, i) => (
              <Line
                key={s.selection_id}
                type="stepAfter"
                dataKey={merged.keys[i]}
                name={s.label ?? s.selection_raw}
                stroke={colors.series[i % colors.series.length]}
                strokeWidth={2}
                dot={false}
                connectNulls={false}
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <div className="small muted">
        Current:{" "}
        {group.series.map((s) => `${s.label ?? s.selection_raw} ${formatOdds(s.current_odds)}`).join(" · ")}. Gaps =
        selection suspended.
      </div>
    </div>
  );
}
