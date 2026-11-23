/** Domain-specific status badges. */
import type { AlertLevel, BetOut, BetBrief, JobStatus, LinkStatus, MatchStatus, Tier } from "../api/types";
import { formatAge, formatPct, formatSignedPct, humanize } from "../lib/format";
import { Badge } from "./ui";
import type { BadgeTone } from "./ui";

/**
 * Status of a bet as the owner should read it: the settlement `result` wins over the stored
 * `status` (paper bets keep status "dry_run" after settlement).
 */
export function betDisplayStatus(bet: Pick<BetOut | BetBrief, "status" | "result" | "dry_run">): {
  label: string;
  tone: BadgeTone;
} {
  const paper = bet.dry_run || bet.status === "dry_run";
  const prefix = paper ? "Paper " : "";
  if (bet.result === "won") return { label: `${prefix}won`, tone: "pos" };
  if (bet.result === "lost") return { label: `${prefix}lost`, tone: "neg" };
  if (bet.result === "void") return { label: `${prefix}void`, tone: "neutral" };
  switch (bet.status) {
    case "won":
      return { label: "Won", tone: "pos" };
    case "lost":
      return { label: "Lost", tone: "neg" };
    case "void":
      return { label: "Void", tone: "neutral" };
    case "placed":
      return { label: paper ? "Paper open" : "Placed", tone: "accent" };
    case "pending":
    case "placing":
      return { label: paper ? "Paper open" : humanize(bet.status), tone: "warn" };
    case "failed":
      return { label: "Failed", tone: "neg" };
    case "cancelled":
      return { label: "Cancelled", tone: "neutral" };
    case "dry_run":
      return { label: "Paper open", tone: "info" };
    default:
      return { label: humanize(bet.status), tone: "neutral" };
  }
}

export function BetStatusBadge({ bet }: { bet: Pick<BetOut | BetBrief, "status" | "result" | "dry_run"> }) {
  const { label, tone } = betDisplayStatus(bet);
  return (
    <Badge tone={tone} title={`stored status: ${bet.status}${bet.result ? `, result: ${bet.result}` : ""}`}>
      {label.charAt(0).toUpperCase() + label.slice(1)}
    </Badge>
  );
}

const MATCH_TONES: Record<MatchStatus, BadgeTone> = {
  scheduled: "accent",
  live: "warn",
  finished: "neutral",
  canceled: "neg",
  postponed: "warn",
  unknown: "neutral",
};

export function MatchStatusBadge({ status }: { status: MatchStatus }) {
  return <Badge tone={MATCH_TONES[status] ?? "neutral"}>{humanize(status)}</Badge>;
}

const LINK_TONES: Record<LinkStatus, BadgeTone> = {
  auto: "pos",
  confirmed: "pos",
  pending_review: "warn",
  rejected: "neg",
  unlinked: "neutral",
};

export function LinkBadge({ status, confidence }: { status: LinkStatus | null | undefined; confidence?: number | null }) {
  if (!status) return <Badge>Not linked</Badge>;
  const text = status === "pending_review" ? "Needs review" : humanize(status);
  return (
    <Badge tone={LINK_TONES[status] ?? "neutral"} title={confidence != null ? `link confidence ${formatPct(confidence)}` : undefined}>
      {text}
    </Badge>
  );
}

export function TierBadge({ tier }: { tier: Tier }) {
  const tone: BadgeTone = tier === "s" ? "accent" : tier === "a" ? "info" : "neutral";
  return <Badge tone={tone}>{tier === "unranked" ? "Unranked" : `Tier ${tier.toUpperCase()}`}</Badge>;
}

/** EV badge: green when positive. */
export function EvBadge({ ev }: { ev: number | null | undefined }) {
  if (ev === null || ev === undefined || !Number.isFinite(ev)) return <span className="faint">—</span>;
  return <Badge tone={ev > 0 ? "pos" : "neg"}>EV {formatSignedPct(ev)}</Badge>;
}

/** Data completeness vs the decision threshold (default 0.95). */
export function CompletenessBadge({ value, threshold = 0.95 }: { value: number | null | undefined; threshold?: number }) {
  if (value === null || value === undefined) return <Badge title="no data completeness yet">n/a</Badge>;
  const tone: BadgeTone = value >= threshold ? "pos" : value >= threshold - 0.15 ? "warn" : "neg";
  return (
    <Badge tone={tone} title={`data completeness ${formatPct(value)} (bets need ≥ ${formatPct(threshold, 0)})`}>
      {formatPct(value, 0)}
    </Badge>
  );
}

const JOB_TONES: Record<JobStatus, BadgeTone> = {
  running: "info",
  success: "pos",
  partial: "warn",
  failed: "neg",
};

export function JobStatusBadge({ status }: { status: JobStatus }) {
  return <Badge tone={JOB_TONES[status] ?? "neutral"}>{humanize(status)}</Badge>;
}

const ALERT_TONES: Record<AlertLevel, BadgeTone> = { critical: "neg", warning: "warn", info: "info" };

export function AlertLevelBadge({ level }: { level: AlertLevel }) {
  return <Badge tone={ALERT_TONES[level]}>{humanize(level)}</Badge>;
}

/** Age with a colour: green below `warnS`, amber below `badS`, red above. */
export function AgeBadge({ seconds, warnS, badS }: { seconds: number | null | undefined; warnS: number; badS: number }) {
  if (seconds === null || seconds === undefined) return <Badge>never</Badge>;
  const tone: BadgeTone = seconds < warnS ? "pos" : seconds < badS ? "warn" : "neg";
  return <Badge tone={tone}>{formatAge(seconds)}</Badge>;
}
