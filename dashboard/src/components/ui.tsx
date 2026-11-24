/** Small presentational building blocks of the design system. */
import { useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from "react";
import { ApiError } from "../api/client";
import { formatPct } from "../lib/format";

/**
 * Props that make a table row act as a toggle button: click, Enter / Space and keyboard focus.
 * Returns `{}` when there is nothing to toggle.
 */
export function toggleRowProps(onToggle: (() => void) | undefined, expanded: boolean) {
  if (!onToggle) return {};
  return {
    onClick: onToggle,
    onKeyDown: (e: ReactKeyboardEvent<HTMLTableRowElement>) => {
      if (e.target !== e.currentTarget) return;
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onToggle();
      }
    },
    tabIndex: 0,
    "aria-expanded": expanded,
  };
}

export function Card({
  title,
  sub,
  actions,
  children,
  className,
}: {
  title?: ReactNode;
  sub?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card ${className ?? ""}`}>
      {(title || actions) && (
        <div className="card-header">
          <div>
            {title && <h2>{title}</h2>}
            {sub && <div className="card-sub">{sub}</div>}
          </div>
          {actions && <div className="row">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

export function Stat({
  label,
  value,
  hint,
  className,
}: {
  label: ReactNode;
  value: ReactNode;
  hint?: ReactNode;
  className?: string;
}) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className={`stat-value ${className ?? ""}`}>{value}</span>
      {hint && <span className="stat-hint">{hint}</span>}
    </div>
  );
}

export type BadgeTone = "neutral" | "pos" | "neg" | "warn" | "info" | "accent";

export function Badge({ tone = "neutral", children, title }: { tone?: BadgeTone; children: ReactNode; title?: string }) {
  return (
    <span className={`badge ${tone === "neutral" ? "" : `badge-${tone}`}`} title={title}>
      {children}
    </span>
  );
}

export function Loading({ label = "Loading…", lines = 3 }: { label?: string; lines?: number }) {
  return (
    <div role="status" aria-live="polite" className="stack" style={{ gap: "0.5rem" }}>
      <span className="visually-hidden">{label}</span>
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className="skeleton" style={{ height: 18, width: `${90 - i * 12}%` }} />
      ))}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message =
    error instanceof ApiError ? error.message : error instanceof Error ? error.message : "Something went wrong";
  return (
    <div className="error-box row-between" role="alert">
      <span>{message}</span>
      {onRetry && (
        <button className="btn btn-sm" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

/** Renders loading / error / content for a TanStack query result. */
export function QueryView<T>({
  query,
  children,
  loadingLines,
}: {
  query: { data: T | undefined; error: unknown; isLoading: boolean; refetch: () => unknown };
  children: (data: T) => ReactNode;
  loadingLines?: number;
}) {
  if (query.data !== undefined) return <>{children(query.data)}</>;
  if (query.error) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  return <Loading lines={loadingLines} />;
}

/** Usage meter (0..1+); colour turns amber at 60 % and red at 90 % / when `reached`. */
export function Meter({
  label,
  value,
  detail,
  reached,
}: {
  label: ReactNode;
  value: number | null | undefined;
  detail?: ReactNode;
  reached?: boolean;
}) {
  const v = value !== null && value !== undefined && Number.isFinite(value) ? Math.max(0, value) : 0;
  const tone = reached || v >= 0.9 ? "neg" : v >= 0.6 ? "warn" : "";
  return (
    <div className="meter">
      <div className="row-between small">
        <span className="muted">{label}</span>
        <strong className={tone === "neg" ? "neg" : undefined}>{reached ? "LIMIT REACHED" : formatPct(v, 0)}</strong>
      </div>
      <div
        className="meter-track"
        role="meter"
        aria-label={typeof label === "string" ? label : undefined}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(Math.min(v, 1) * 100)}
      >
        <div className={`meter-fill ${tone}`} style={{ width: `${Math.min(v, 1) * 100}%` }} />
      </div>
      {detail && <div className="small muted">{detail}</div>}
    </div>
  );
}

/**
 * Two-sided bar: team1 share (left, team1 colour) vs team2. `low` / `high` draw the uncertainty
 * range (e.g. the -1 / +1 sigma scenarios) as a bracket.
 */
export function ProbBar({
  p,
  low,
  high,
  label1,
  label2,
}: {
  p: number | null;
  low?: number | null;
  high?: number | null;
  label1?: string;
  label2?: string;
}) {
  if (p === null || !Number.isFinite(p)) return <span className="faint">—</span>;
  const clamp = (x: number) => Math.min(1, Math.max(0, x));
  const lo = low !== null && low !== undefined ? clamp(Math.min(low, high ?? low)) : null;
  const hi = high !== null && high !== undefined ? clamp(Math.max(high, low ?? high)) : null;
  const title = `${label1 ?? "Team 1"} ${formatPct(p)}${lo !== null && hi !== null ? ` (range ${formatPct(lo)} – ${formatPct(hi)})` : ""}`;
  return (
    <div className="probbar" title={title} role="img" aria-label={title}>
      <div className="probbar-fill" style={{ width: `${clamp(p) * 100}%` }} />
      {lo !== null && hi !== null && hi > lo && (
        <div className="probbar-range" style={{ left: `${lo * 100}%`, width: `${(hi - lo) * 100}%` }} />
      )}
      <span className="probbar-label left">{formatPct(p, 0)}</span>
      <span className="probbar-label right">{formatPct(1 - p, 0)}</span>
      {label2 && <span className="visually-hidden">{`${label2} ${formatPct(1 - p)}`}</span>}
    </div>
  );
}

/** Horizontal labelled bar for distributions (0..1). */
export function HBar({ label, value, max = 1, color }: { label: ReactNode; value: number; max?: number; color?: string }) {
  const width = max > 0 ? Math.min(1, Math.max(0, value / max)) * 100 : 0;
  return (
    <div className="hbar">
      <span className="nowrap" style={{ overflow: "hidden", textOverflow: "ellipsis", textTransform: "capitalize" }}>
        {label}
      </span>
      <div className="hbar-track">
        <div className="hbar-fill" style={{ width: `${width}%`, background: color }} />
      </div>
      <span className="right">{formatPct(value, 0)}</span>
    </div>
  );
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: readonly { value: T; label: ReactNode }[];
  onChange: (value: T) => void;
  label: string;
}) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.value} type="button" aria-pressed={o.value === value} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Chips<T extends string>({
  values,
  options,
  onChange,
  label,
}: {
  values: readonly T[];
  options: readonly { value: T; label: ReactNode }[];
  onChange: (values: T[]) => void;
  label: string;
}) {
  const toggle = (v: T) =>
    onChange(values.includes(v) ? values.filter((x) => x !== v) : [...values, v]);
  return (
    <div className="chips" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.value} type="button" className="chip" aria-pressed={values.includes(o.value)} onClick={() => toggle(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Pager({
  total,
  limit,
  offset,
  onChange,
}: {
  total: number;
  limit: number;
  offset: number;
  onChange: (offset: number) => void;
}) {
  if (total <= limit && offset === 0) return <div className="pager">{total} item(s)</div>;
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(total, offset + limit);
  return (
    <nav className="pager" aria-label="Pagination">
      <span>
        {from}–{to} of {total}
      </span>
      <button className="btn btn-sm" disabled={offset <= 0} onClick={() => onChange(Math.max(0, offset - limit))}>
        ‹ Prev
      </button>
      <button className="btn btn-sm" disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}>
        Next ›
      </button>
    </nav>
  );
}

export function Tabs<T extends string>({
  value,
  tabs,
  onChange,
  label,
}: {
  value: T;
  tabs: readonly { value: T; label: ReactNode }[];
  onChange: (value: T) => void;
  label: string;
}) {
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {tabs.map((t) => (
        <button key={t.value} role="tab" className="tab" aria-selected={t.value === value} onClick={() => onChange(t.value)}>
          {t.label}
        </button>
      ))}
    </div>
  );
}

/**
 * Modal confirmation dialog. `reasonLabel` adds a free-text reason field (sent with kill switch /
 * pause actions for the audit log). Escape and the backdrop cancel; focus starts on the input or
 * the cancel button (a destructive action is never the default).
 */
export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel = "Confirm",
  danger = false,
  busy = false,
  reasonLabel,
  defaultReason = "",
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  children?: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  busy?: boolean;
  reasonLabel?: string;
  defaultReason?: string;
  onConfirm: (reason: string) => void;
  onCancel: () => void;
}) {
  const [reason, setReason] = useState(defaultReason);
  const titleId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  // Callers often pass inline callbacks and the page re-renders on every poll: keep the latest
  // values in refs so the effect below runs only when the dialog opens (and keeps the typed reason).
  const onCancelRef = useRef(onCancel);
  const defaultReasonRef = useRef(defaultReason);
  useEffect(() => {
    onCancelRef.current = onCancel;
    defaultReasonRef.current = defaultReason;
  });

  useEffect(() => {
    if (!open) return;
    setReason(defaultReasonRef.current);
    const t = setTimeout(() => (inputRef.current ?? cancelRef.current)?.focus(), 0);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancelRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      clearTimeout(t);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (!open) return null;
  return (
    <div className="dialog-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onCancel()}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <h2 id={titleId}>{title}</h2>
        {children && <div>{children}</div>}
        <form
          className="stack"
          style={{ gap: "0.85rem" }}
          onSubmit={(e) => {
            e.preventDefault();
            if (!busy) onConfirm(reason.trim());
          }}
        >
          {reasonLabel && (
            <div className="field">
              <label htmlFor={`${titleId}-reason`}>{reasonLabel}</label>
              <input
                id={`${titleId}-reason`}
                ref={inputRef}
                type="text"
                maxLength={500}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
            </div>
          )}
          <div className="dialog-actions">
            <button type="button" className="btn" ref={cancelRef} onClick={onCancel}>
              Cancel
            </button>
            <button type="submit" className={`btn ${danger ? "btn-danger" : "btn-primary"}`} disabled={busy}>
              {busy ? "Working…" : confirmLabel}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
