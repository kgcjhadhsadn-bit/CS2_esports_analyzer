/**
 * Link review queue: Betclic events linked with low confidence are never bet on until the owner
 * confirms, rejects or relinks them. Each candidate shows the suggested orientation
 * (`teams_swapped` = Betclic lists our team2 first), which is sent with the action.
 */
import { useState } from "react";
import { useReviewAction, useReviewLinks } from "../api/hooks";
import type { LinkStatus, ReviewActionIn, ReviewCandidate, ReviewItem } from "../api/types";
import { LinkBadge, MatchStatusBadge } from "../components/badges";
import { MatchTitle } from "../components/tables";
import { Badge, Card, ConfirmDialog, Empty, Pager, QueryView, Segmented } from "../components/ui";
import { formatDateTime, formatNumber, formatPct, formatRelative, teamName } from "../lib/format";

const PAGE_SIZE = 20;

interface PendingAction {
  eventId: number;
  body: ReviewActionIn;
  title: string;
  description: string;
  danger?: boolean;
}

/** The action that links the event to a candidate: confirm the current match or relink elsewhere. */
export function candidateAction(candidate: ReviewCandidate): ReviewActionIn {
  return candidate.is_current
    ? { action: "confirm", match_id: candidate.match.id, teams_swapped: candidate.teams_swapped }
    : { action: "relink", match_id: candidate.match.id, teams_swapped: candidate.teams_swapped };
}

export type ManualOrientation = "auto" | "same" | "swapped";

const ORIENTATION_TEXT: Record<ManualOrientation, string> = {
  auto: "team order detected from the names",
  same: "Betclic lists our team1 first",
  swapped: "Betclic lists our team2 first",
};

/** Body of a manual relink; "auto" leaves `teams_swapped` out so the API detects it. */
export function manualRelinkBody(matchId: number, orientation: ManualOrientation): ReviewActionIn {
  if (orientation === "auto") return { action: "relink", match_id: matchId };
  return { action: "relink", match_id: matchId, teams_swapped: orientation === "swapped" };
}

function orientationText(item: ReviewItem, c: ReviewCandidate): string {
  const t1 = teamName(c.match.team1, "team1");
  const t2 = teamName(c.match.team2, "team2");
  return c.teams_swapped
    ? `Betclic "${item.event.team1_raw}" = ${t2}, "${item.event.team2_raw}" = ${t1}`
    : `Betclic "${item.event.team1_raw}" = ${t1}, "${item.event.team2_raw}" = ${t2}`;
}

function ReviewCard({ item, onAction }: { item: ReviewItem; onAction: (a: PendingAction) => void }) {
  const ev = item.event;
  const [manualId, setManualId] = useState("");
  // "auto" omits teams_swapped so the API detects the orientation from the team names; sending
  // `false` by default would silently store "same order" for a match Betclic lists the other way.
  const [manualOrientation, setManualOrientation] = useState<ManualOrientation>("auto");
  const manualMatchId = Number(manualId);
  const manualValid = Number.isInteger(manualMatchId) && manualMatchId > 0 && manualMatchId < 2 ** 31;
  return (
    <Card
      title={
        <span>
          {ev.name} <span className="faint small mono">{ev.external_id}</span>
        </span>
      }
      sub={
        <>
          Betclic order: <strong>{ev.team1_raw}</strong> vs <strong>{ev.team2_raw}</strong>
          {ev.competition_raw ? ` · ${ev.competition_raw}` : ""} · starts {formatDateTime(ev.starts_at)} ({formatRelative(ev.starts_at)}) ·{" "}
          {item.n_markets} market(s)
        </>
      }
      actions={
        <>
          <LinkBadge status={ev.link_status} confidence={ev.link_confidence} />
          {ev.link_confidence !== null && <span className="small muted">confidence {formatPct(ev.link_confidence, 0)}</span>}
        </>
      }
    >
      {item.current_match && (
        <p className="small" style={{ marginTop: 0 }}>
          Currently linked to <MatchTitle match={item.current_match} /> ({formatDateTime(item.current_match.scheduled_at)}
          {ev.teams_swapped ? ", teams swapped" : ""}).
        </p>
      )}
      {item.candidates.length ? (
        <div className="table-wrap">
          <table className="table table-compact">
            <thead>
              <tr>
                <th>Candidate match</th>
                <th>Start (Warsaw)</th>
                <th className="num">Score</th>
                <th className="num">Names</th>
                <th className="num">Δ time</th>
                <th>Orientation</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {item.candidates.map((c) => (
                <tr key={c.match.id}>
                  <td>
                    <MatchTitle match={c.match} /> {c.is_current && <Badge tone="accent">current</Badge>}{" "}
                    <MatchStatusBadge status={c.match.status} />
                    {c.match.tournament && <div className="small muted">{c.match.tournament}</div>}
                  </td>
                  <td className="nowrap">{formatDateTime(c.match.scheduled_at)}</td>
                  <td className="num">
                    <strong>{formatPct(c.score, 0)}</strong>
                  </td>
                  <td className="num">{formatNumber(c.name_score, 0)}</td>
                  <td className="num">{c.time_diff_minutes !== null ? `${formatNumber(c.time_diff_minutes, 0)} min` : "—"}</td>
                  <td className="small">
                    {c.teams_swapped ? <Badge tone="warn">swapped</Badge> : <Badge>same order</Badge>}
                  </td>
                  <td className="right">
                    <button
                      className={`btn btn-sm ${c.is_current ? "btn-primary" : ""}`}
                      onClick={() =>
                        onAction({
                          eventId: ev.id,
                          body: candidateAction(c),
                          title: c.is_current ? "Confirm this link?" : "Relink to this match?",
                          description: `${ev.name} → match #${c.match.id}. ${orientationText(item, c)}.`,
                        })
                      }
                    >
                      {c.is_current ? "Confirm" : "Link here"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty>No candidate match within the time window.</Empty>
      )}
      <div className="row-between" style={{ marginTop: "0.75rem" }}>
        <form
          className="row"
          onSubmit={(e) => {
            e.preventDefault();
            if (!manualValid) return;
            onAction({
              eventId: ev.id,
              body: manualRelinkBody(manualMatchId, manualOrientation),
              title: "Relink to another match?",
              description: `${ev.name} → match #${manualMatchId} (${ORIENTATION_TEXT[manualOrientation]}).`,
            });
          }}
        >
          <label htmlFor={`relink-${ev.id}`} className="small">
            Relink to match id
          </label>
          <input
            id={`relink-${ev.id}`}
            type="number"
            min={1}
            style={{ width: 110 }}
            value={manualId}
            onChange={(e) => setManualId(e.target.value)}
          />
          <select
            aria-label="Team order"
            value={manualOrientation}
            onChange={(e) => setManualOrientation(e.target.value as ManualOrientation)}
          >
            <option value="auto">order: detect from names</option>
            <option value="same">order: same as our match</option>
            <option value="swapped">order: swapped</option>
          </select>
          <button type="submit" className="btn btn-sm" disabled={!manualValid}>
            Relink
          </button>
        </form>
        {ev.link_status !== "rejected" && (
          <button
            className="btn btn-sm btn-danger"
            onClick={() =>
              onAction({
                eventId: ev.id,
                body: { action: "reject" },
                title: "Reject this event?",
                description: `${ev.name} will be unlinked and never bet on.`,
                danger: true,
              })
            }
          >
            Reject
          </button>
        )}
      </div>
    </Card>
  );
}

export default function ReviewPage() {
  const [status, setStatus] = useState<LinkStatus>("pending_review");
  const [offset, setOffset] = useState(0);
  const [pending, setPending] = useState<PendingAction | null>(null);
  const query = useReviewLinks([status], offset, PAGE_SIZE);
  const mutation = useReviewAction();

  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <h1>Link review</h1>
          <p>Betclic events the matcher was unsure about. They are never bet on until you decide.</p>
        </div>
        <Segmented<LinkStatus>
          label="Link status"
          value={status}
          onChange={(v) => {
            setStatus(v);
            setOffset(0);
          }}
          options={[
            { value: "pending_review", label: "Needs review" },
            { value: "rejected", label: "Rejected" },
            { value: "confirmed", label: "Confirmed" },
            { value: "auto", label: "Auto" },
          ]}
        />
      </div>
      <QueryView query={query} loadingLines={6}>
        {(page) =>
          page.items.length ? (
            <>
              {page.items.map((item) => (
                <ReviewCard key={item.event.id} item={item} onAction={setPending} />
              ))}
              <Pager total={page.total} limit={page.limit} offset={page.offset} onChange={setOffset} />
            </>
          ) : (
            <Card>
              <Empty>{status === "pending_review" ? "Nothing to review - every Betclic event is linked." : "No events."}</Empty>
            </Card>
          )
        }
      </QueryView>
      <ConfirmDialog
        open={pending !== null}
        title={pending?.title ?? ""}
        danger={pending?.danger}
        busy={mutation.isPending}
        confirmLabel={pending?.body.action === "reject" ? "Reject" : pending?.body.action === "confirm" ? "Confirm link" : "Relink"}
        onCancel={() => setPending(null)}
        onConfirm={() => {
          if (!pending) return;
          mutation.mutate({ eventId: pending.eventId, body: pending.body }, { onSuccess: () => setPending(null) });
        }}
      >
        <p>{pending?.description}</p>
        {pending?.body.action !== "reject" && (
          <p className="small muted">
            If the match or the team order changes, the event's market mappings are cleared and re-mapped by the odds
            collector before anything can be bet.
          </p>
        )}
      </ConfirmDialog>
    </div>
  );
}
