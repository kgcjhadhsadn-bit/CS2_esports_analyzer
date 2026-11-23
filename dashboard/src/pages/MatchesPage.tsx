/**
 * Matches: upcoming / past table with filters (status, Betclic link, Tier 1/2 scope, tier) and
 * data-completeness / best-EV badges.
 *
 * Assumption: `GET /api/matches` has no `tier` parameter, so the tier filter is applied to the
 * loaded page in the browser (the pager still counts every match matching the server filters).
 */
import { useState } from "react";
import { useMatches } from "../api/hooks";
import type { MatchQuery } from "../api/client";
import type { MatchStatus, Tier } from "../api/types";
import { MatchesTable } from "../components/tables";
import { Card, Chips, Pager, QueryView, Segmented } from "../components/ui";
import { useNow } from "../lib/useNow";

type View = "upcoming" | "past" | "all";
type TriState = "any" | "yes" | "no";

const STATUS_OPTIONS: { value: MatchStatus; label: string }[] = [
  { value: "scheduled", label: "Scheduled" },
  { value: "live", label: "Live" },
  { value: "finished", label: "Finished" },
  { value: "postponed", label: "Postponed" },
  { value: "canceled", label: "Canceled" },
  { value: "unknown", label: "Unknown" },
];

const TIER_OPTIONS: { value: Tier; label: string }[] = [
  { value: "s", label: "S" },
  { value: "a", label: "A" },
  { value: "b", label: "B" },
  { value: "c", label: "C" },
  { value: "d", label: "D" },
  { value: "unranked", label: "Unranked" },
];

const PAGE_SIZE = 50;

const triToBool = (v: TriState): boolean | undefined => (v === "any" ? undefined : v === "yes");

/** Query for the chosen view and filters (exported for tests). */
export function buildMatchQuery(
  view: View,
  statuses: MatchStatus[],
  linked: TriState,
  inScope: TriState,
  now: Date,
  offset: number,
): MatchQuery {
  const q: MatchQuery = { limit: PAGE_SIZE, offset, linked: triToBool(linked), in_scope: triToBool(inScope) };
  if (statuses.length) q.status = statuses;
  if (view === "upcoming") {
    q.from = new Date(now.getTime() - 6 * 3600_000).toISOString();
    q.order = "asc";
  } else if (view === "past") {
    q.to = now.toISOString();
    q.order = "desc";
  } else {
    q.order = "desc";
  }
  return q;
}

export default function MatchesPage() {
  const [view, setView] = useState<View>("upcoming");
  const [statuses, setStatuses] = useState<MatchStatus[]>(["scheduled", "live"]);
  const [linked, setLinked] = useState<TriState>("any");
  const [inScope, setInScope] = useState<TriState>("yes");
  const [tiers, setTiers] = useState<Tier[]>([]);
  const [offset, setOffset] = useState(0);
  const now = useNow(5 * 60_000);
  const query = useMatches(buildMatchQuery(view, statuses, linked, inScope, now, offset));

  const changeView = (v: View) => {
    setView(v);
    setOffset(0);
    setStatuses(v === "upcoming" ? ["scheduled", "live"] : v === "past" ? ["finished"] : []);
  };
  const resetting =
    <T,>(set: (v: T) => void) =>
    (v: T) => {
      set(v);
      setOffset(0);
    };

  return (
    <div className="stack">
      <div className="page-header">
        <div>
          <h1>Matches</h1>
          <p>Model coverage, Betclic links and the best available value per match.</p>
        </div>
        <Segmented<View>
          label="Time range"
          value={view}
          onChange={changeView}
          options={[
            { value: "upcoming", label: "Upcoming" },
            { value: "past", label: "Past" },
            { value: "all", label: "All" },
          ]}
        />
      </div>
      <Card>
        <div className="filters">
          <div className="field">
            <span className="small muted">Status</span>
            <Chips<MatchStatus> label="Status" values={statuses} options={STATUS_OPTIONS} onChange={resetting(setStatuses)} />
          </div>
          <div className="field">
            <span className="small muted">Betclic link</span>
            <Segmented<TriState>
              label="Betclic link"
              value={linked}
              onChange={resetting(setLinked)}
              options={[
                { value: "any", label: "Any" },
                { value: "yes", label: "Linked" },
                { value: "no", label: "Not linked" },
              ]}
            />
          </div>
          <div className="field">
            <span className="small muted">Scope</span>
            <Segmented<TriState>
              label="Scope"
              value={inScope}
              onChange={resetting(setInScope)}
              options={[
                { value: "yes", label: "Tier 1/2" },
                { value: "any", label: "Any" },
                { value: "no", label: "Out of scope" },
              ]}
            />
          </div>
          <div className="field">
            <span className="small muted">Tier (this page)</span>
            <Chips<Tier> label="Tier" values={tiers} options={TIER_OPTIONS} onChange={setTiers} />
          </div>
        </div>
        <QueryView query={query} loadingLines={6}>
          {(page) => {
            const items = tiers.length ? page.items.filter((m) => tiers.includes(m.tier)) : page.items;
            return (
              <>
                <MatchesTable matches={items} />
                {tiers.length > 0 && items.length !== page.items.length && (
                  <div className="small muted" style={{ paddingTop: "0.5rem" }}>
                    Showing {items.length} of {page.items.length} matches on this page (tier filter).
                  </div>
                )}
                <Pager total={page.total} limit={page.limit} offset={page.offset} onChange={setOffset} />
              </>
            );
          }}
        </QueryView>
      </Card>
    </div>
  );
}
