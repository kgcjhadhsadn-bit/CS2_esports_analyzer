/** Kill switch, pause / resume and Betclic login controls (all behind confirmation dialogs). */
import { useCallback, useState } from "react";
import {
  useBetclicLoginStatus,
  useKillSwitch,
  usePause,
  useResume,
  useStartBetclicLogin,
  useTradingState,
} from "../api/hooks";
import type { TradingState } from "../api/types";
import { formatDateTime } from "../lib/format";
import { Badge, ConfirmDialog, ErrorState, Loading } from "./ui";

/**
 * The dialog's action is fixed when it opens (`target`): the state polls every 15 s, and a dialog
 * that flipped from "Engage" to "Release" under the owner's cursor (because the engine or another
 * tab changed the switch meanwhile) must never send the opposite of what was asked for.
 */
export function KillSwitch({ state }: { state: TradingState }) {
  const [target, setTarget] = useState<boolean | null>(null);
  const mutation = useKillSwitch();
  const on = state.kill_switch.on;
  const close = useCallback(() => setTarget(null), []);
  const engage = target ?? !on;
  return (
    <div className="stack" style={{ gap: "0.5rem" }}>
      <button
        type="button"
        className={`btn kill-switch ${on ? "on" : "btn-danger"}`}
        aria-pressed={on}
        onClick={() => setTarget(!on)}
      >
        {on ? "Kill switch ON - release" : "Kill switch - stop all betting"}
      </button>
      <div className="small muted">
        {on ? (
          <>
            Engaged{state.kill_switch.reason ? `: "${state.kill_switch.reason}"` : ""} ·{" "}
            {formatDateTime(state.kill_switch.updated_at)}
            {state.kill_switch.updated_by ? ` by ${state.kill_switch.updated_by}` : ""}
          </>
        ) : (
          "Off: the bot may place bets that pass every check."
        )}
      </div>
      <ConfirmDialog
        open={target !== null}
        title={engage ? "Engage the kill switch?" : "Release the kill switch?"}
        danger={engage}
        busy={mutation.isPending}
        confirmLabel={engage ? "Stop all betting" : "Release kill switch"}
        reasonLabel="Reason (saved in the audit log)"
        defaultReason={engage ? "manual stop from the dashboard" : ""}
        onCancel={close}
        onConfirm={(reason) => mutation.mutate({ on: engage, reason }, { onSuccess: close })}
      >
        {engage ? (
          <p>
            No new bet will be placed until you release it. Bets already placed at Betclic stay open and are settled
            normally.
            {on ? " (The kill switch is already on; this keeps it on.)" : ""}
          </p>
        ) : (
          <p>
            Betting becomes possible again{state.paused.on ? " once the pause is also cleared" : ""}. Make sure the
            reason for stopping is resolved.
            {!on ? " (The kill switch is already off.)" : ""}
          </p>
        )}
      </ConfirmDialog>
    </div>
  );
}

export function PauseResume({ state }: { state: TradingState }) {
  const [dialog, setDialog] = useState<"pause" | "resume" | null>(null);
  const pause = usePause();
  const resume = useResume();
  const paused = state.paused.on;
  const close = useCallback(() => setDialog(null), []);
  return (
    <div className="stack" style={{ gap: "0.5rem" }}>
      <div className="row-between">
        <div className="row">
          <strong>Trading</strong>
          {paused ? <Badge tone="warn">Paused</Badge> : <Badge tone="pos">Running</Badge>}
        </div>
        {paused ? (
          <button className="btn btn-primary" onClick={() => setDialog("resume")}>
            Resume
          </button>
        ) : (
          <button className="btn" onClick={() => setDialog("pause")}>
            Pause
          </button>
        )}
      </div>
      {paused && (
        <div className="small muted">
          {state.paused.reason ?? "paused"} · {formatDateTime(state.paused.updated_at)}
          {state.paused.updated_by ? ` by ${state.paused.updated_by}` : ""}
        </div>
      )}
      <ConfirmDialog
        open={dialog === "pause"}
        title="Pause trading?"
        busy={pause.isPending}
        confirmLabel="Pause"
        reasonLabel="Reason"
        defaultReason="paused from the dashboard"
        onCancel={close}
        onConfirm={(reason) => pause.mutate(reason, { onSuccess: close })}
      >
        <p>The bot keeps collecting data and pricing matches but places no bet until you resume.</p>
      </ConfirmDialog>
      <ConfirmDialog
        open={dialog === "resume"}
        title="Resume trading?"
        busy={resume.isPending}
        confirmLabel="Resume"
        reasonLabel="Reason (optional)"
        onCancel={close}
        onConfirm={(reason) => resume.mutate(reason, { onSuccess: close })}
      >
        <p>
          This clears the pause flag only.
          {state.kill_switch.on ? " The kill switch is still ON and keeps blocking every bet." : ""}
          {state.paused.reason?.toLowerCase().includes("loss") ? " The pause was caused by a loss limit." : ""}
        </p>
      </ConfirmDialog>
    </div>
  );
}

/** Current trading state with kill switch + pause controls. */
export function TradingControls() {
  const state = useTradingState();
  if (state.error && !state.data) return <ErrorState error={state.error} onRetry={() => void state.refetch()} />;
  if (!state.data) return <Loading lines={2} />;
  return (
    <div className="stack">
      <KillSwitch state={state.data} />
      <PauseResume state={state.data} />
    </div>
  );
}

/** Opens the executor's visible Betclic login window on the PC and polls its status. */
export function BetclicLoginButton({ compact = false }: { compact?: boolean }) {
  const [open, setOpen] = useState(false);
  const status = useBetclicLoginStatus();
  const start = useStartBetclicLogin();
  const s = status.data;
  const running = s?.running ?? false;
  const close = useCallback(() => setOpen(false), []);
  return (
    <div className="stack" style={{ gap: "0.4rem" }}>
      <div className="row">
        <button
          className="btn btn-primary"
          disabled={running || start.isPending || s?.configured === false}
          onClick={() => setOpen(true)}
          title={s?.configured === false ? "No login launcher configured in this API process" : undefined}
        >
          {running ? "Login window open…" : "Open Betclic login"}
        </button>
        {!compact && s && !s.configured && <Badge tone="warn">Not configured</Badge>}
        {s?.last_result === true && !running && <Badge tone="pos">Last login OK</Badge>}
        {s?.last_result === false && !running && <Badge tone="neg">Last login not completed</Badge>}
      </div>
      {!compact && s?.last_error && <div className="small neg">Last error: {s.last_error}</div>}
      {!compact && s?.last_started_at && (
        <div className="small muted">
          Last started {formatDateTime(s.last_started_at)}
          {s.last_finished_at ? `, finished ${formatDateTime(s.last_finished_at)}` : ""}
        </div>
      )}
      <ConfirmDialog
        open={open}
        title="Open the Betclic login window?"
        confirmLabel="Open window"
        busy={start.isPending}
        onCancel={close}
        onConfirm={() => start.mutate(undefined, { onSuccess: close })}
      >
        <p>
          A browser window with the bot's Betclic profile opens on this PC. Log in by hand (including SMS / 2FA) and
          close it when done; the bot reuses the session.
        </p>
      </ConfirmDialog>
    </div>
  );
}
