/**
 * Health: overall status, alerts, executor health, data freshness, job health (ages and status
 * colours, staleness from the expected interval), the built-in scheduler and recent job runs.
 */
import { Fragment, useState } from "react";
import { useHealth, useJobRuns } from "../api/hooks";
import type { Health, JobHealth, JobStatus } from "../api/types";
import { AlertList } from "../components/AlertList";
import { AgeBadge, JobStatusBadge } from "../components/badges";
import { AutonomyStatusCard, BetclicLoginButton, PauseReasonList } from "../components/TradingControls";
import { Badge, Card, Chips, Empty, Pager, QueryView, toggleRowProps } from "../components/ui";
import type { BadgeTone } from "../components/ui";
import { formatAge, formatDateTime, formatMoney, formatNumber, formatRelative, humanize } from "../lib/format";

const STATUS_TONE: Record<Health["status"], BadgeTone> = { ok: "pos", warning: "warn", critical: "neg" };

/** Seconds between `iso` and the API's `now` (null when missing). */
export function ageFrom(now: string, iso: string | null | undefined): number | null {
  if (!iso) return null;
  const a = new Date(now).getTime();
  const b = new Date(iso).getTime();
  return Number.isFinite(a) && Number.isFinite(b) ? Math.max(0, (a - b) / 1000) : null;
}

/** Colour of a job's last success relative to its expected interval. */
export function jobAgeThresholds(job: Pick<JobHealth, "expected_interval_s">): { warnS: number; badS: number } {
  const interval = job.expected_interval_s ?? 3600;
  return { warnS: interval * 1.5, badS: interval * 2 + 600 };
}

function ExecutorCard({ health }: { health: Health }) {
  const ex = health.executor;
  const selectors = Object.entries(ex.selectors_ok);
  const failing = selectors.filter(([, ok]) => !ok);
  return (
    <Card
      title="Executor (Betclic browser)"
      actions={
        ex.ok === null ? <Badge>unknown</Badge> : ex.ok ? <Badge tone="pos">healthy</Badge> : <Badge tone="neg">unhealthy</Badge>
      }
    >
      <dl className="kv small">
        <dt>Mode</dt>
        <dd>
          {ex.enabled ? "enabled" : "disabled"} · {ex.dry_run ? "dry-run" : "live"}
        </dd>
        <dt>Logged in</dt>
        <dd>{ex.logged_in === null ? "unknown" : ex.logged_in ? "yes" : <strong className="neg">no</strong>}</dd>
        <dt>Betclic balance</dt>
        <dd>{formatMoney(ex.balance)}</dd>
        <dt>Last check</dt>
        <dd>
          {formatDateTime(ex.checked_at)} {ex.age_s !== null && <span className="muted">({formatAge(ex.age_s)} ago)</span>}
        </dd>
        <dt>Source</dt>
        <dd>{humanize(ex.source)}</dd>
        {ex.message && (
          <>
            <dt>Message</dt>
            <dd>{ex.message}</dd>
          </>
        )}
      </dl>
      {selectors.length > 0 && (
        <div style={{ marginTop: "0.6rem" }}>
          <div className="small muted">
            Selectors: {selectors.length - failing.length}/{selectors.length} OK
          </div>
          <div className="chips" style={{ marginTop: "0.3rem" }}>
            {selectors.map(([name, ok]) => (
              <Badge key={name} tone={ok ? "pos" : "neg"}>
                {name}
              </Badge>
            ))}
          </div>
        </div>
      )}
      {ex.logged_in === false && (
        <div style={{ marginTop: "0.75rem" }}>
          <BetclicLoginButton compact />
        </div>
      )}
    </Card>
  );
}

function FreshnessCard({ health }: { health: Health }) {
  const f = health.freshness;
  if (!f) {
    return (
      <Card title="Data freshness">
        <Empty>Not available (database unreachable?).</Empty>
      </Card>
    );
  }
  const rows: { label: string; at: string | null; age: number | null; warn: number; bad: number }[] = [
    { label: "Last Betclic odds snapshot", at: f.last_odds_snapshot_at, age: f.last_odds_snapshot_age_s, warn: 900, bad: 3600 },
    { label: "Last Betclic event seen", at: f.last_event_seen_at, age: f.last_event_seen_age_s, warn: 900, bad: 3600 },
    { label: "Last match sync", at: f.last_match_sync_at, age: f.last_match_sync_age_s, warn: 3 * 3600, bad: 12 * 3600 },
    { label: "Last prediction", at: f.last_prediction_at, age: ageFrom(health.now, f.last_prediction_at), warn: 3 * 3600, bad: 24 * 3600 },
    { label: "Last analysis", at: f.last_analysis_at, age: ageFrom(health.now, f.last_analysis_at), warn: 6 * 3600, bad: 48 * 3600 },
    { label: "Last decision", at: f.last_decision_at, age: ageFrom(health.now, f.last_decision_at), warn: 6 * 3600, bad: 48 * 3600 },
  ];
  return (
    <Card title="Data freshness">
      <table className="table table-compact">
        <tbody>
          {rows.map((r) => (
            <tr key={r.label}>
              <td>{r.label}</td>
              <td className="small muted nowrap">{formatDateTime(r.at)}</td>
              <td className="num">
                <AgeBadge seconds={r.age} warnS={r.warn} badS={r.bad} />
              </td>
            </tr>
          ))}
          {f.sync_jobs.map((j) => (
            <tr key={j.name}>
              <td>
                Sync job <span className="mono small">{j.name}</span> <span className="faint small">({j.kind})</span>
              </td>
              <td className="small muted nowrap">{formatDateTime(j.last_success_at)}</td>
              <td className="num">
                <AgeBadge seconds={j.age_s} warnS={3 * 3600} badS={12 * 3600} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function JobsTable({ jobs }: { jobs: JobHealth[] }) {
  if (!jobs.length) return <Empty>No job has run yet.</Empty>;
  return (
    <div className="table-wrap">
      <table className="table table-compact">
        <thead>
          <tr>
            <th>Job</th>
            <th>Status</th>
            <th>Last run</th>
            <th className="num">Duration</th>
            <th className="num">Items</th>
            <th className="num">Last success</th>
            <th className="num">Interval</th>
            <th className="num">Failures</th>
            <th>Next due</th>
            <th>Error</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((j) => {
            const t = jobAgeThresholds(j);
            return (
              <tr key={j.name}>
                <td>
                  <span className="mono">{j.name}</span>
                  {j.kind && <span className="faint small"> · {j.kind}</span>}
                  {j.scheduled === false && (
                    <>
                      {" "}
                      <Badge title="the scheduler of this process does not run it">not scheduled</Badge>
                    </>
                  )}
                </td>
                <td>
                  <span className="row" style={{ gap: "0.3rem" }}>
                    <JobStatusBadge status={j.status} />
                    {j.stale && <Badge tone="neg">stale</Badge>}
                  </span>
                </td>
                <td className="nowrap small">
                  <span title={formatDateTime(j.last_started_at)}>{formatAge(j.age_s)} ago</span>
                </td>
                <td className="num">{j.duration_s !== null ? `${formatNumber(j.duration_s, 1)} s` : "—"}</td>
                <td className="num">{j.items}</td>
                <td className="num">
                  {j.stale ? <Badge tone="neg">{formatAge(j.last_success_age_s)}</Badge> : <AgeBadge seconds={j.last_success_age_s} {...t} />}
                </td>
                <td className="num small">{j.expected_interval_s !== null ? formatAge(j.expected_interval_s) : "—"}</td>
                <td className={`num ${j.consecutive_failures > 0 ? "neg" : ""}`}>{j.consecutive_failures}</td>
                <td className="small nowrap">{j.next_due_at ? formatRelative(j.next_due_at) : "—"}</td>
                <td className="small" style={{ maxWidth: 280 }}>
                  {j.error ? (
                    <span className="neg" title={j.error}>
                      {j.error.length > 80 ? `${j.error.slice(0, 80)}…` : j.error}
                    </span>
                  ) : (
                    ""
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function SchedulerCard({ health }: { health: Health }) {
  const s = health.scheduler;
  return (
    <Card
      title="Scheduler"
      actions={!s ? <Badge>not running in this process</Badge> : s.ok ? <Badge tone="pos">ok</Badge> : <Badge tone="neg">status error</Badge>}
    >
      {!s ? (
        <p className="small muted" style={{ margin: 0 }}>
          This API process runs without the built-in scheduler. Start everything with{" "}
          <code>cs2bot up</code>.
        </p>
      ) : !s.jobs.length ? (
        <Empty>No scheduled jobs.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table table-compact">
            <thead>
              <tr>
                <th>Job</th>
                <th>Lane</th>
                <th>State</th>
                <th>Last run</th>
                <th>Next due</th>
                <th className="num">Failures</th>
              </tr>
            </thead>
            <tbody>
              {s.jobs.map((j) => (
                <tr key={j.name}>
                  <td className="mono">{j.name}</td>
                  <td>{j.lane ?? "—"}</td>
                  <td>
                    {!j.enabled ? (
                      <Badge>disabled</Badge>
                    ) : j.running ? (
                      <Badge tone="info">running</Badge>
                    ) : j.overdue ? (
                      <Badge tone="neg">overdue</Badge>
                    ) : (
                      <Badge tone="pos">waiting</Badge>
                    )}
                  </td>
                  <td className="small nowrap">{formatRelative(j.last_run_at)}</td>
                  <td className="small nowrap">
                    {j.due_in_s !== null ? (j.due_in_s >= 0 ? `in ${formatAge(j.due_in_s)}` : `${formatAge(-j.due_in_s)} overdue`) : "—"}
                  </td>
                  <td className={`num ${j.failures > 0 ? "neg" : ""}`}>{j.failures}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

const JOB_STATUS_OPTIONS: { value: JobStatus; label: string }[] = [
  { value: "running", label: "Running" },
  { value: "success", label: "Success" },
  { value: "partial", label: "Partial" },
  { value: "failed", label: "Failed" },
];

function JobRunsCard() {
  const [statuses, setStatuses] = useState<JobStatus[]>([]);
  const [name, setName] = useState("");
  const [offset, setOffset] = useState(0);
  const [expanded, setExpanded] = useState<number | null>(null);
  const runs = useJobRuns(statuses, name.trim(), offset, 25);
  return (
    <Card title="Recent job runs">
      <div className="filters">
        <div className="field">
          <span className="small muted">Status</span>
          <Chips<JobStatus>
            label="Job status"
            values={statuses}
            options={JOB_STATUS_OPTIONS}
            onChange={(v) => {
              setStatuses(v);
              setOffset(0);
            }}
          />
        </div>
        <div className="field">
          <label htmlFor="job-name" className="small muted">
            Job name (exact)
          </label>
          <input
            id="job-name"
            type="search"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              setOffset(0);
            }}
          />
        </div>
      </div>
      <QueryView query={runs}>
        {(page) =>
          page.items.length ? (
            <>
              <div className="table-wrap">
                <table className="table table-compact">
                  <thead>
                    <tr>
                      <th>Started (Warsaw)</th>
                      <th>Job</th>
                      <th>Status</th>
                      <th className="num">Duration</th>
                      <th className="num">Items</th>
                      <th>Error</th>
                    </tr>
                  </thead>
                  <tbody>
                    {page.items.map((r) => {
                      const open = expanded === r.id;
                      const hasDetails = Object.keys(r.details).length > 0 || !!r.error;
                      return (
                        <Fragment key={r.id}>
                          <tr
                            className={hasDetails ? `clickable ${open ? "expanded" : ""}` : undefined}
                            {...toggleRowProps(hasDetails ? () => setExpanded(open ? null : r.id) : undefined, open)}
                          >
                            <td className="nowrap">{formatDateTime(r.started_at)}</td>
                            <td className="mono">{r.job_name}</td>
                            <td>
                              <JobStatusBadge status={r.status} />
                            </td>
                            <td className="num">{r.duration_s !== null ? `${formatNumber(r.duration_s, 1)} s` : "—"}</td>
                            <td className="num">{r.items}</td>
                            <td className="small neg" style={{ maxWidth: 360 }}>
                              {r.error ? (r.error.length > 90 ? `${r.error.slice(0, 90)}…` : r.error) : ""}
                            </td>
                          </tr>
                          {open && (
                            <tr className="detail-row">
                              <td colSpan={6}>
                                {r.error && <pre className="json">{r.error}</pre>}
                                {Object.keys(r.details).length > 0 && <pre className="json">{JSON.stringify(r.details, null, 2)}</pre>}
                              </td>
                            </tr>
                          )}
                        </Fragment>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <Pager total={page.total} limit={page.limit} offset={page.offset} onChange={setOffset} />
            </>
          ) : (
            <Empty>No job runs match.</Empty>
          )
        }
      </QueryView>
    </Card>
  );
}

export default function HealthPage() {
  const health = useHealth();
  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <h1>Health</h1>
          <p>Pipelines, data freshness and the Betclic executor. Refreshes every 15 seconds.</p>
        </div>
        {health.data && (
          <div className="row">
            <Badge tone={STATUS_TONE[health.data.status]}>{health.data.status.toUpperCase()}</Badge>
            <Badge tone={health.data.db.ok ? "pos" : "neg"}>
              DB {health.data.db.ok ? `ok ${formatNumber(health.data.db.latency_ms, 1)} ms` : "down"}
            </Badge>
            <span className="small muted">
              v{health.data.version} · {formatDateTime(health.data.now)}
            </span>
          </div>
        )}
      </div>
      <QueryView query={health} loadingLines={8}>
        {(data) => (
          <>
            {!data.db.ok && (
              <div className="error-box" role="alert">
                Database problem: {data.db.error ?? "unreachable"}
              </div>
            )}
            <Card title="Alerts">
              <AlertList alerts={data.alerts} />
            </Card>
            {data.trading && (
              <Card title="Trading state & autonomy">
                <div className="stack">
                  {data.trading.paused.on && (data.trading.pause_reasons?.length ?? 0) > 0 ? (
                    <PauseReasonList state={data.trading} />
                  ) : (
                    <span className="small muted">{data.trading.halted ? data.trading.reason : "Not paused."}</span>
                  )}
                  <AutonomyStatusCard state={data.trading} />
                </div>
              </Card>
            )}
            <div className="grid grid-2">
              <ExecutorCard health={data} />
              <FreshnessCard health={data} />
            </div>
            <Card title="Jobs" sub="Last run of every job; a job is stale when its last success is older than 2 × its interval + 10 min">
              <JobsTable jobs={data.jobs} />
            </Card>
            <SchedulerCard health={data} />
          </>
        )}
      </QueryView>
      <JobRunsCard />
    </div>
  );
}
