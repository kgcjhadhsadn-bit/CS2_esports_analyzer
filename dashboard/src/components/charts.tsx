/**
 * Recharts helpers. SVG presentation attributes do not reliably resolve CSS variables, so the
 * chart colours are read from the design tokens at runtime and refreshed when the OS colour
 * scheme changes.
 */
import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { Bar, BarChart, CartesianGrid, Cell, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Bar as BarDatum } from "../lib/analysis";
import { formatPct } from "../lib/format";

export interface ChartColors {
  grid: string;
  axis: string;
  text: string;
  surface: string;
  team1: string;
  team2: string;
  pos: string;
  neg: string;
  warn: string;
  series: string[];
}

const FALLBACK: ChartColors = {
  grid: "#e1e6ec",
  axis: "#5d6b7b",
  text: "#17202b",
  surface: "#ffffff",
  team1: "#1f5fd1",
  team2: "#c2410c",
  pos: "#12733a",
  neg: "#b42318",
  warn: "#8a5300",
  series: ["#1f5fd1", "#c2410c", "#12733a", "#7c3aed", "#b42318"],
};

function readColors(): ChartColors {
  if (typeof window === "undefined" || typeof getComputedStyle !== "function") return FALLBACK;
  const css = getComputedStyle(document.documentElement);
  const v = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
  return {
    grid: v("--chart-grid", FALLBACK.grid),
    axis: v("--chart-axis", FALLBACK.axis),
    text: v("--text", FALLBACK.text),
    surface: v("--surface", FALLBACK.surface),
    team1: v("--team1", FALLBACK.team1),
    team2: v("--team2", FALLBACK.team2),
    pos: v("--pos", FALLBACK.pos),
    neg: v("--neg", FALLBACK.neg),
    warn: v("--warn", FALLBACK.warn),
    series: [1, 2, 3, 4, 5].map((i, idx) => v(`--chart-${i}`, FALLBACK.series[idx] ?? "#888")),
  };
}

export function useChartColors(): ChartColors {
  const [colors, setColors] = useState<ChartColors>(readColors);
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const update = () => setColors(readColors());
    mq.addEventListener?.("change", update);
    return () => mq.removeEventListener?.("change", update);
  }, []);
  return colors;
}

export function axisProps(colors: ChartColors) {
  return {
    stroke: colors.axis,
    tick: { fill: colors.axis, fontSize: 12 },
    tickLine: false,
  } as const;
}

/** Generic tooltip box: `render` gets the hovered datum. */
export function ChartTooltip<T>({
  active,
  payload,
  render,
}: {
  active?: boolean;
  payload?: { payload: T }[];
  render: (datum: T) => ReactNode;
}) {
  if (!active || !payload || !payload.length || !payload[0]) return null;
  return <div className="chart-tooltip">{render(payload[0].payload)}</div>;
}

/**
 * Probability histogram (bars = P(x)), with an optional reference line (e.g. a market line or
 * the mean) and per-bar colouring (e.g. team1 vs team2 side of a margin).
 */
/** Integer axis ticks covering [min, max] with a "nice" step (at most ~12 ticks). */
export function integerTicks(min: number, max: number, maxTicks = 12): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max) || max < min) return [];
  const lo = Math.ceil(min);
  const hi = Math.floor(max);
  const span = Math.max(hi - lo, 1);
  const step = [1, 2, 3, 5, 10, 20, 25, 50, 100].find((s) => span / s <= maxTicks) ?? Math.ceil(span / maxTicks);
  const ticks: number[] = [];
  for (let t = Math.ceil(lo / step) * step; t <= hi; t += step) ticks.push(t);
  return ticks;
}

export function Histogram({
  bars,
  xLabel,
  refLine,
  refLabel,
  colorFor,
  height = 240,
}: {
  bars: BarDatum[];
  xLabel: string;
  refLine?: number | null;
  refLabel?: string;
  colorFor?: (x: number) => string;
  height?: number;
}) {
  const colors = useChartColors();
  const data = bars.map((b) => ({ ...b, fill: colorFor ? colorFor(b.x) : colors.series[0] }));
  const xs = bars.map((b) => b.x);
  const ticks = xs.length ? integerTicks(Math.min(...xs), Math.max(...xs)) : undefined;
  return (
    <div className="chart-box" style={{ height }} aria-label={`${xLabel} distribution`} role="img">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 18, right: 8, bottom: 16, left: 0 }} barCategoryGap={1}>
          <CartesianGrid stroke={colors.grid} vertical={false} />
          <XAxis
            dataKey="x"
            type="number"
            domain={["dataMin - 0.5", "dataMax + 0.5"]}
            ticks={ticks}
            interval={0}
            allowDecimals={false}
            {...axisProps(colors)}
            label={{ value: xLabel, position: "insideBottom", offset: -10, fill: colors.axis, fontSize: 12 }}
          />
          <YAxis tickFormatter={(v: number) => formatPct(v, 0)} width={44} {...axisProps(colors)} />
          <Tooltip
            cursor={{ fill: colors.grid, opacity: 0.5 }}
            content={
              <ChartTooltip<BarDatum & { fill: string }>
                render={(d) => (
                  <>
                    <strong>{d.label}</strong>: {formatPct(d.p, 1)}
                  </>
                )}
              />
            }
          />
          {refLine !== null && refLine !== undefined && (
            <ReferenceLine
              x={refLine}
              stroke={colors.text}
              strokeDasharray="4 3"
              label={refLabel ? { value: refLabel, fill: colors.text, fontSize: 11, position: "top" } : undefined}
            />
          )}
          <Bar dataKey="p" isAnimationActive={false} fill={colors.series[0]}>
            {data.map((d) => (
              <Cell key={d.x} fill={d.fill} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
