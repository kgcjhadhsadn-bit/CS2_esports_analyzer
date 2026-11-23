/**
 * Match detail: teams and lineups, veto, win probabilities with uncertainty, round distributions,
 * player kill projections, the markets table (model vs Betclic), odds history, context flags and
 * the Claude rationale (rendered with the safe Markdown renderer, never as HTML).
 */
import { useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMatch, useOddsHistory } from "../api/hooks";
import type { MarketRow, MatchDetail } from "../api/types";
import { CompletenessBadge, EvBadge, LinkBadge, MatchStatusBadge, TierBadge } from "../components/badges";
import {
  ContextFlagsCard,
  LineupsCard,
  MapsPlayedCard,
  PlayersCard,
  RoundDistributionsCard,
  VetoCard,
  WinProbabilityCard,
} from "../components/match/AnalysisSections";
import { MarketsTable } from "../components/match/MarketsTable";
import { groupOddsSeries, OddsHistoryChart } from "../components/match/OddsHistoryChart";
import { Badge, Card, Empty, ErrorState, Loading, QueryView, Tabs } from "../components/ui";
import { parseAnalysis } from "../lib/analysis";
import { formatDateTime, formatRelative, teamName } from "../lib/format";
import { safeHref, SafeMarkdown } from "../lib/markdown";

type Tab = "markets" | "model" | "context";

function Header({ detail }: { detail: MatchDetail }) {
  const m = detail.match;
  const finished = m.team1_score !== null && m.team2_score !== null;
  const betclicUrl = m.betclic?.url ? safeHref(m.betclic.url) : null;
  return (
    <Card>
      <div className="teams-header">
        <div className="team-block">
          <span className="team-name team1">{teamName(m.team1, "team1")}</span>
          {m.team1?.hltv_rank && <span className="small muted">HLTV #{m.team1.hltv_rank}</span>}
        </div>
        <div className="stack" style={{ gap: "0.2rem", alignItems: "center" }}>
          <span className="score">{finished ? `${m.team1_score} : ${m.team2_score}` : "vs"}</span>
          <span className="small muted nowrap">{m.format?.toUpperCase() ?? ""}</span>
        </div>
        <div className="team-block right">
          <span className="team-name team2">{teamName(m.team2, "team2")}</span>
          {m.team2?.hltv_rank && <span className="small muted">HLTV #{m.team2.hltv_rank}</span>}
        </div>
      </div>
      <div className="row" style={{ marginTop: "0.75rem", justifyContent: "center" }}>
        <MatchStatusBadge status={m.status} />
        <TierBadge tier={m.tier} />
        {m.is_lan && <Badge>LAN</Badge>}
        {!m.in_scope && <Badge tone="warn">out of scope</Badge>}
        <span className="small">
          {formatDateTime(m.scheduled_at)} <span className="muted">({formatRelative(m.scheduled_at)})</span>
        </span>
        {m.tournament && (
          <span className="small muted">
            {m.tournament.name}
            {m.stage ? ` · ${m.stage}` : ""}
          </span>
        )}
      </div>
      <div className="row" style={{ marginTop: "0.5rem", justifyContent: "center" }}>
        <span className="small muted">Data</span>
        <CompletenessBadge value={m.data_completeness} />
        <span className="small muted">Betclic</span>
        <LinkBadge status={m.betclic?.link_status} confidence={m.betclic?.link_confidence} />
        {m.betclic?.link_status === "pending_review" && (
          <Link to="/review" className="small">
            review link ›
          </Link>
        )}
        {betclicUrl && (
          <a className="small" href={betclicUrl} target="_blank" rel="noopener noreferrer">
            open on Betclic ↗
          </a>
        )}
        <span className="small muted">Best EV</span>
        <EvBadge ev={m.best_ev} />
        {detail.unmapped_selections > 0 && (
          <Badge tone="warn" title="Betclic selections not mapped to our market taxonomy (never bet)">
            {detail.unmapped_selections} unmapped
          </Badge>
        )}
      </div>
    </Card>
  );
}

function OddsHistorySection({ matchId, selectedKey, onSelect }: { matchId: number; selectedKey: string | null; onSelect: (k: string) => void }) {
  const history = useOddsHistory(matchId);
  const groups = useMemo(() => groupOddsSeries(history.data?.series ?? []), [history.data]);
  return (
    <QueryView query={history}>
      {() => <OddsHistoryChart groups={groups} selectedKey={selectedKey} onSelect={onSelect} />}
    </QueryView>
  );
}

export default function MatchDetailPage() {
  const params = useParams();
  const id = Number(params.id);
  const query = useMatch(id);
  const [tab, setTab] = useState<Tab>("markets");
  const [oddsKey, setOddsKey] = useState<string | null>(null);
  const historyRef = useRef<HTMLDivElement>(null);
  const view = useMemo(() => (query.data?.analysis ? parseAnalysis(query.data.analysis.payload) : null), [query.data]);

  if (!Number.isInteger(id) || id <= 0) return <ErrorState error={new Error("Invalid match id")} />;
  if (query.error && !query.data) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  if (!query.data) return <Loading lines={8} />;
  const detail = query.data;
  const match = detail.match;

  const showHistory = (row: MarketRow) => {
    setOddsKey(row.spec_key);
    historyRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <div className="stack">
      <div className="row">
        <Link to="/matches" className="small">
          ‹ Matches
        </Link>
      </div>
      <Header detail={detail} />
      <Tabs<Tab>
        label="Match sections"
        value={tab}
        onChange={setTab}
        tabs={[
          { value: "markets", label: `Markets (${detail.markets.length})` },
          { value: "model", label: "Model" },
          { value: "context", label: `Context${detail.context_flags.length ? ` (${detail.context_flags.length})` : ""}` },
        ]}
      />

      {tab === "markets" && (
        <>
          <Card title="Markets" sub="Model price vs Betclic, EV after tax and the decision for every outcome">
            <MarketsTable rows={detail.markets} taxRate={detail.tax_rate} onShowHistory={showHistory} />
          </Card>
          <div ref={historyRef}>
            <Card title="Odds history" sub="Betclic prices over time (step line)">
              <OddsHistorySection matchId={match.id} selectedKey={oddsKey} onSelect={setOddsKey} />
            </Card>
          </div>
        </>
      )}

      {tab === "model" &&
        (view ? (
          <>
            <div className="small muted">
              Analysis {detail.analysis?.model_version} · {formatDateTime(detail.analysis?.created_at)}
              {view.nSimulations ? ` · ${view.nSimulations.toLocaleString("en-GB")} simulations` : ""}
            </div>
            <div className="grid grid-2">
              <WinProbabilityCard view={view} match={match} />
              <div className="stack">
                <VetoCard vetoes={detail.vetoes} view={view} match={match} />
                <LineupsCard lineups={detail.lineups} match={match} />
              </div>
            </div>
            <MapsPlayedCard maps={detail.maps} match={match} />
            <RoundDistributionsCard view={view} match={match} />
            <PlayersCard view={view} match={match} />
          </>
        ) : (
          <>
            <Card title="Model">
              <Empty>No analysis for this match yet (the prediction job has not priced it).</Empty>
            </Card>
            <div className="grid grid-2">
              <VetoCard vetoes={detail.vetoes} view={null} match={match} />
              <LineupsCard lineups={detail.lineups} match={match} />
            </div>
            <MapsPlayedCard maps={detail.maps} match={match} />
          </>
        ))}

      {tab === "context" && (
        <>
          <Card
            title="Claude rationale"
            sub={detail.analysis?.rationale_model ? `by ${detail.analysis.rationale_model}` : undefined}
          >
            {detail.analysis?.rationale ? (
              <SafeMarkdown text={detail.analysis.rationale} />
            ) : (
              <Empty>No rationale written for this match.</Empty>
            )}
          </Card>
          <ContextFlagsCard flags={detail.context_flags} match={match} />
        </>
      )}
    </div>
  );
}
