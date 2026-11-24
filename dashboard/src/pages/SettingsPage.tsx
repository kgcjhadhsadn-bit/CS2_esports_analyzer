/**
 * Settings: decision thresholds and bankroll caps editor (PUT /api/control/config) with
 * client-side validation, effective values and defaults; kill switch, dry-run status and the
 * Betclic login button.
 */
import { useCallback, useState } from "react";
import { ApiError } from "../api/client";
import { useConfig, useHealth, useSaveConfig } from "../api/hooks";
import type { ConfigOut, MarketThresholds } from "../api/types";
import { BetclicLoginButton, TradingControls } from "../components/TradingControls";
import { Badge, Card, ConfirmDialog, QueryView } from "../components/ui";
import { formatDateTime, formatPct, humanize } from "../lib/format";
import {
  BETTABLE_MARKETS,
  buildSection,
  FIELDS,
  overridesToRows,
  serverFieldErrors,
  THRESHOLD_KEYS,
  toFormValue,
} from "../lib/settingsSchema";
import type { FieldSpec, FormValue, MarketOverrideRow, Section } from "../lib/settingsSchema";

function displayValue(spec: FieldSpec, value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (spec.kind === "bool") return value ? "yes" : "no";
  if (spec.percent && typeof value === "number") return `${value} (${formatPct(value, 1)})`;
  return String(value);
}

function FieldRow({
  spec,
  value,
  onChange,
  effective,
  defaultValue,
  overridden,
  error,
}: {
  spec: FieldSpec;
  value: FormValue;
  onChange: (v: FormValue) => void;
  effective: unknown;
  defaultValue: unknown;
  overridden: boolean;
  error?: string;
}) {
  const id = `setting-${spec.key}`;
  const pct = spec.percent && typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value));
  return (
    <tr>
      <td>
        <label htmlFor={id}>{spec.label}</label>
        <div className="field-hint">{spec.hint}</div>
      </td>
      <td>
        {spec.kind === "bool" ? (
          <input id={id} type="checkbox" checked={value === true} onChange={(e) => onChange(e.target.checked)} />
        ) : (
          <div className="stack" style={{ gap: "0.2rem" }}>
            <div className="row" style={{ flexWrap: "nowrap" }}>
              <input
                id={id}
                type={spec.kind === "currency" ? "text" : "number"}
                inputMode={spec.kind === "currency" ? "text" : "decimal"}
                step={spec.step}
                min={spec.min}
                max={spec.max}
                style={{ width: 130 }}
                value={typeof value === "string" ? value : ""}
                aria-invalid={error ? true : undefined}
                aria-describedby={error ? `${id}-error` : undefined}
                onChange={(e) => onChange(e.target.value)}
              />
              {pct && <span className="small muted nowrap">{formatPct(Number(value), 1)}</span>}
            </div>
            {error && (
              <span id={`${id}-error`} className="field-error">
                {error}
              </span>
            )}
          </div>
        )}
      </td>
      <td className="small">{displayValue(spec, effective)}</td>
      <td className="small muted">{displayValue(spec, defaultValue)}</td>
      <td className="right">
        {overridden ? <Badge tone="accent">override</Badge> : <Badge>default</Badge>}{" "}
        <button
          type="button"
          className="btn btn-sm btn-ghost"
          onClick={() => onChange(toFormValue(defaultValue))}
          disabled={value === toFormValue(defaultValue)}
          title="Put the default value in the field (save to apply)"
        >
          Reset
        </button>
      </td>
    </tr>
  );
}

function MarketOverridesEditor({
  rows,
  setRows,
  disabled,
  setDisabled,
  error,
}: {
  rows: MarketOverrideRow[];
  setRows: (rows: MarketOverrideRow[]) => void;
  disabled: string[];
  setDisabled: (d: string[]) => void;
  error?: string;
}) {
  const update = (i: number, patch: Partial<MarketOverrideRow>) => setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const used = new Set(rows.map((r) => r.market));
  return (
    <div className="grid grid-2" style={{ marginTop: "1rem" }}>
      <div>
        <h3>Per-market thresholds</h3>
        <p className="small muted" style={{ marginTop: 0 }}>
          Override min probability / min EV / max sigma for one market type. Empty cells use the global value.
        </p>
        <table className="table table-compact">
          <thead>
            <tr>
              <th>Market</th>
              <th>Min p</th>
              <th>Min EV</th>
              <th>Max σ</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i}>
                <td>
                  <select aria-label="Market type" value={row.market} onChange={(e) => update(i, { market: e.target.value })}>
                    <option value="">choose…</option>
                    {BETTABLE_MARKETS.map((m) => (
                      <option key={m} value={m} disabled={used.has(m) && m !== row.market}>
                        {humanize(m)}
                      </option>
                    ))}
                  </select>
                </td>
                {THRESHOLD_KEYS.map((k) => (
                  <td key={k}>
                    <input
                      aria-label={`${row.market || "market"} ${k}`}
                      type="number"
                      step="0.01"
                      style={{ width: 80 }}
                      value={row[k]}
                      onChange={(e) => update(i, { [k]: e.target.value } as Partial<MarketOverrideRow>)}
                    />
                  </td>
                ))}
                <td>
                  <button type="button" className="btn btn-sm btn-ghost" onClick={() => setRows(rows.filter((_, j) => j !== i))} aria-label="Remove override">
                    ×
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <button
          type="button"
          className="btn btn-sm"
          style={{ marginTop: "0.5rem" }}
          onClick={() => setRows([...rows, { market: "", min_probability: "", min_ev: "", max_sigma: "" }])}
        >
          Add market override
        </button>
        {error && <div className="field-error" style={{ marginTop: "0.4rem" }}>{error}</div>}
      </div>
      <div>
        <h3>Disabled markets</h3>
        <p className="small muted" style={{ marginTop: 0 }}>
          The engine never bets on a disabled market type.
        </p>
        <div className="grid grid-2" style={{ gap: "0.25rem 1rem" }}>
          {BETTABLE_MARKETS.map((m) => (
            <label key={m} className="row small" style={{ fontWeight: 400 }}>
              <input
                type="checkbox"
                checked={disabled.includes(m)}
                onChange={(e) => setDisabled(e.target.checked ? [...disabled, m] : disabled.filter((x) => x !== m))}
              />
              {humanize(m)}
            </label>
          ))}
        </div>
      </div>
    </div>
  );
}

function SectionForm({ section, config }: { section: Section; config: ConfigOut }) {
  const effective = config.effective[section] as Record<string, unknown>;
  const defaults = config.defaults[section] as Record<string, unknown>;
  const overrides = config.overrides[section] as Record<string, unknown>;
  const specs = FIELDS[section];
  const [form, setForm] = useState<Record<string, FormValue>>(() =>
    Object.fromEntries(specs.map((s) => [s.key, toFormValue(effective[s.key])])),
  );
  const [rows, setRows] = useState<MarketOverrideRow[]>(() =>
    overridesToRows(effective.market_overrides as Record<string, MarketThresholds> | undefined),
  );
  const [disabled, setDisabled] = useState<string[]>(() =>
    Array.isArray(effective.disabled_markets) ? (effective.disabled_markets as string[]) : [],
  );
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [confirmReset, setConfirmReset] = useState(false);
  const save = useSaveConfig();

  const onError = (err: unknown) => {
    if (err instanceof ApiError && err.status === 422) {
      const fieldErrors = serverFieldErrors(err.validationErrors, section);
      setErrors(Object.keys(fieldErrors).length ? fieldErrors : { _section: err.message });
    }
  };

  const submit = () => {
    const result = buildSection(
      section,
      form,
      defaults,
      section === "decision" ? { marketRows: rows, disabledMarkets: disabled } : {},
    );
    setErrors(result.errors);
    if (Object.keys(result.errors).length) return;
    save.mutate({ [section]: result.body }, { onError });
  };
  const closeReset = useCallback(() => setConfirmReset(false), []);
  const errorCount = Object.keys(errors).length;

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
      noValidate
    >
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>Setting</th>
              <th>New value</th>
              <th>Effective now</th>
              <th>Default</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {specs.map((spec) => (
              <FieldRow
                key={spec.key}
                spec={spec}
                value={form[spec.key] ?? ""}
                onChange={(v) => setForm((f) => ({ ...f, [spec.key]: v }))}
                effective={effective[spec.key]}
                defaultValue={defaults[spec.key]}
                overridden={spec.key in overrides}
                error={errors[spec.key]}
              />
            ))}
          </tbody>
        </table>
      </div>
      {section === "decision" && (
        <MarketOverridesEditor
          rows={rows}
          setRows={setRows}
          disabled={disabled}
          setDisabled={setDisabled}
          error={errors.market_overrides ?? errors.disabled_markets}
        />
      )}
      {errors._section && (
        <div className="field-error" role="alert">
          {errors._section}
        </div>
      )}
      <div className="row-between" style={{ marginTop: "1rem" }}>
        <span className="small muted">
          {errorCount > 0 ? <span className="neg">Fix {errorCount} problem(s) before saving.</span> : "Values equal to the default are not stored as overrides."}
        </span>
        <div className="row">
          <button type="button" className="btn" onClick={() => setConfirmReset(true)} disabled={!Object.keys(overrides).length}>
            Reset all to defaults
          </button>
          <button type="submit" className="btn btn-primary" disabled={save.isPending}>
            {save.isPending ? "Saving…" : `Save ${section} settings`}
          </button>
        </div>
      </div>
      <ConfirmDialog
        open={confirmReset}
        title={`Clear all ${section} overrides?`}
        confirmLabel="Use defaults"
        busy={save.isPending}
        onCancel={closeReset}
        onConfirm={() => save.mutate({ [section]: {} }, { onSuccess: closeReset, onError })}
      >
        <p>The engine goes back to the values from .env / the built-in defaults.</p>
      </ConfirmDialog>
    </form>
  );
}

/** The bet rule in one sentence: confidence only (odds ignored) unless an EV guard is configured. */
export function DecisionRuleNote({ config }: { config: ConfigOut }) {
  const decision = config.effective.decision;
  const minEv = Number(decision.min_ev);
  const minP = Number(decision.min_probability);
  const oddsIgnored = Number.isFinite(minEv) && minEv <= -1;
  const profitableWin = decision.require_profitable_win !== false;
  return (
    <div className="alert alert-info" role="note" aria-label="Bet rule">
      <strong>Bet rule:</strong>{" "}
      {oddsIgnored ? (
        <>
          confidence only, <strong>odds ignored</strong> (EV check off). A pick is bet when the calibrated probability is at
          least {formatPct(minP, 0)}, whatever the odds - even 1.28
          {profitableWin ? ", as long as a win pays back more than the stake after the 12% tax (odds 1.137 or more)" : ""}.
        </>
      ) : (
        <>
          calibrated probability at least {formatPct(minP, 0)} and EV at least {formatPct(minEv, 1)} after the 12% stake tax
          {minEv === 0 ? " (break-even guard)" : ""}.
        </>
      )}
      {decision.use_confidence_guard ? (
        <> Probabilities above 75% are first shrunk by the confidence guard where the history shows over-confidence.</>
      ) : null}
      <StakingNote config={config} />
    </div>
  );
}

/** How stakes are sized: confidence tiers (the owner's rule) or fractional Kelly. */
export function StakingNote({ config }: { config: ConfigOut }) {
  const bank = config.effective.bankroll;
  if (bank.staking_mode === "kelly") {
    return (
      <div className="small" style={{ marginTop: "0.35rem" }}>
        <strong>Staking:</strong> {formatPct(Number(bank.kelly_fraction), 0)} Kelly, capped at{" "}
        {formatPct(Number(bank.max_stake_fraction), 1)} of the bankroll per bet.
      </div>
    );
  }
  const tiers = [...(bank.confidence_tiers ?? [])].sort((a, b) => a[0] - b[0]);
  if (!tiers.length) return null;
  return (
    <div className="small" style={{ marginTop: "0.35rem" }} aria-label="Staking tiers">
      <strong>Staking by confidence:</strong>{" "}
      {tiers
        .map(([p, f], i) => {
          const next = tiers[i + 1];
          const range = next ? `${formatPct(p, 0)}-${formatPct(next[0], 0)}` : `${formatPct(p, 0)}+`;
          return `${range} sure = ${formatPct(f, 1)} of the bankroll`;
        })
        .join(", ")}{" "}
        (per-match, daily and weekly caps still apply; tiers are set with <code>CS2BOT_BANKROLL__CONFIDENCE_TIERS</code>).
    </div>
  );
}

export default function SettingsPage() {
  const config = useConfig();
  const health = useHealth();
  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <h1>Settings</h1>
          <p>Decision thresholds and bankroll caps. Changes apply to the next decision the engine makes.</p>
        </div>
      </div>
      <div className="grid grid-3">
        <Card title="Kill switch & pause">
          <TradingControls />
        </Card>
        <Card title="Execution mode">
          {health.data ? (
            <div className="stack" style={{ gap: "0.5rem" }}>
              <div className="row">
                {health.data.dry_run ? <Badge tone="info">Dry-run ON</Badge> : <Badge tone="neg">LIVE: real money</Badge>}
                {health.data.executor.enabled ? <Badge tone="pos">Executor enabled</Badge> : <Badge>Executor disabled</Badge>}
              </div>
              <p className="small muted" style={{ margin: 0 }}>
                Dry-run is set on the PC with <code>CS2BOT_EXECUTOR__DRY_RUN</code> in <code>.env</code> (restart required); it
                cannot be changed from the dashboard on purpose.
              </p>
            </div>
          ) : (
            <span className="muted small">Loading…</span>
          )}
        </Card>
        <Card title="Betclic session">
          <BetclicLoginButton />
        </Card>
      </div>
      <QueryView query={config} loadingLines={8}>
        {(data) => (
          <>
            {data.warnings.map((w) => (
              <div key={w} className="alert alert-warning" role="alert">
                {w}
              </div>
            ))}
            <DecisionRuleNote config={data} />
            <Card
              title="Decision thresholds"
              sub={data.updated_at ? `Last changed ${formatDateTime(data.updated_at)}${data.updated_by ? ` by ${data.updated_by}` : ""}` : "No dashboard overrides yet"}
            >
              <SectionForm key={`decision-${JSON.stringify(data.overrides.decision)}`} section="decision" config={data} />
            </Card>
            <Card title="Bankroll and staking caps">
              <SectionForm key={`bankroll-${JSON.stringify(data.overrides.bankroll)}`} section="bankroll" config={data} />
            </Card>
          </>
        )}
      </QueryView>
    </div>
  );
}
