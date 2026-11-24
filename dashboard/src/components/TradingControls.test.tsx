import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { TradingState } from "../api/types";
import { DecisionRuleNote } from "../pages/SettingsPage";
import { configOut, tradingState } from "../test/fixtures";
import { AutonomyStatusCard, PauseReasonList } from "./TradingControls";

const paused: TradingState = {
  ...tradingState,
  halted: true,
  reason: "paused: x",
  paused: { on: true, reason: "x", updated_at: null, updated_by: "autonomy" },
  pause_reasons: [
    {
      code: "loss_limit_daily",
      label: "daily loss limit reached (lifts at the next Warsaw midnight)",
      message: "daily loss limit reached: realized loss today 50.00 PLN",
      since: "2026-11-20T10:00:00Z",
      actor: "system",
      count: 1,
      auto_resume: true,
      eta: "2026-11-20T23:00:00Z",
      last_check: null,
    },
    {
      code: "2fa_required",
      label: "Betclic asks for an SMS / 2FA code - needs the owner",
      message: "Betclic asks for an SMS code",
      since: "2026-11-20T10:05:00Z",
      actor: "autonomy",
      count: 2,
      auto_resume: false,
      eta: null,
      last_check: null,
    },
  ],
  circuit_open: true,
  circuit_codes: ["login_required"],
  autonomy: {
    enabled: true,
    auto_login: true,
    last_check_at: "2026-11-20T10:10:00Z",
    last_login_status: "2fa_required",
    last_login_at: "2026-11-20T10:05:00Z",
    last_login_message: "SMS step",
    login_blocked: false,
  },
  telegram: {
    enabled: true,
    configured: true,
    last_scan_at: null,
    last_ok_at: "2026-11-20T10:06:00Z",
    last_error: "HTTP 403: bot was blocked",
    sent: 12,
    last_poll_at: null,
    commands_handled: 3,
  },
};

describe("pause reasons and autonomy status", () => {
  it("lists every structured reason with auto-resume ETA or 'needs you'", () => {
    render(<PauseReasonList state={paused} />);
    expect(screen.getByText("loss_limit_daily")).toBeInTheDocument();
    expect(screen.getByText(/^auto ·/)).toBeInTheDocument();
    expect(screen.getByText("needs you")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Circuit breaker open (login_required)");
    expect(screen.getByText(/2×/)).toBeInTheDocument();
  });

  it("shows autonomy and Telegram state", () => {
    render(<AutonomyStatusCard state={paused} />);
    expect(screen.getByText("auto-resume on")).toBeInTheDocument();
    expect(screen.getByText("auto-login on")).toBeInTheDocument();
    expect(screen.getByText("circuit open")).toBeInTheDocument();
    expect(screen.getByText("failing")).toBeInTheDocument();
    expect(screen.getByText(/Last auto-login: 2fa_required/)).toBeInTheDocument();
    expect(screen.getByText(/12 sent/)).toBeInTheDocument();
  });

  it("works with an older API without the new fields", () => {
    render(<AutonomyStatusCard state={tradingState} />);
    expect(screen.getAllByText("off")).toHaveLength(2);
  });
});

describe("DecisionRuleNote", () => {
  it("says odds are ignored when the EV check is off", () => {
    const cfg = { ...configOut, effective: { ...configOut.effective, decision: { ...configOut.effective.decision, min_ev: -1 } } };
    render(<DecisionRuleNote config={cfg} />);
    expect(screen.getByRole("note")).toHaveTextContent("odds ignored");
    expect(screen.getByRole("note")).toHaveTextContent("75%");
  });

  it("shows an EV guard when one is configured", () => {
    render(<DecisionRuleNote config={configOut} />);
    expect(screen.getByRole("note")).toHaveTextContent("EV at least 5.0%");
  });

  it("explains the profitable-win floor, the confidence guard and the staking tiers", () => {
    const cfg = {
      ...configOut,
      effective: {
        decision: { ...configOut.effective.decision, min_ev: -1, require_profitable_win: true, use_confidence_guard: true },
        bankroll: {
          ...configOut.effective.bankroll,
          staking_mode: "confidence_tiers" as const,
          confidence_tiers: [
            [0.85, 0.015],
            [0.75, 0.01],
            [0.92, 0.02],
          ] as [number, number][],
        },
      },
    };
    render(<DecisionRuleNote config={cfg} />);
    const note = screen.getByRole("note");
    expect(note).toHaveTextContent("1.137");
    expect(note).toHaveTextContent("confidence guard");
    expect(note).toHaveTextContent("75%-85% sure = 1.0% of the bankroll");
    expect(note).toHaveTextContent("85%-92% sure = 1.5% of the bankroll");
    expect(note).toHaveTextContent("92%+ sure = 2.0% of the bankroll");
  });

  it("describes Kelly staking when it is configured", () => {
    const cfg = {
      ...configOut,
      effective: { ...configOut.effective, bankroll: { ...configOut.effective.bankroll, staking_mode: "kelly" as const } },
    };
    render(<DecisionRuleNote config={cfg} />);
    expect(screen.getByRole("note")).toHaveTextContent("20% Kelly");
  });
});
