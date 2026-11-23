/** Cards of the match page that visualise the analysis payload and the match context. */
import { Fragment, useState } from "react";
import type { ContextFlagOut, Lineups, MapOut, MatchSummary, TeamSlot, VetoStepOut } from "../../api/types";
import {
  pmfStats,
  probOver,
  roundMarginBars,
  roundTotalBars,
  scopeLabel,
  slotMapDistribution,
  sortScopes,
} from "../../lib/analysis";
import type { AnalysisView, PlayerView } from "../../lib/analysis";
import { formatDateTime, formatNumber, formatPct, formatSigned, humanize, teamName } from "../../lib/format";
import { safeHref } from "../../lib/markdown";
import { Histogram, useChartColors } from "../charts";
import { Badge, Card, Empty, HBar, ProbBar, Segmented } from "../ui";

function slotTeam(slot: TeamSlot | null | undefined, match: MatchSummary): string {
  if (slot === "team1") return teamName(match.team1, "team1");
  if (slot === "team2") return teamName(match.team2, "team2");
  return "decider";
}

// ---------------------------------------------------------------------------------------- veto

export function VetoCard({ vetoes, view, match }: { vetoes: VetoStepOut[]; view: AnalysisView | null; match: MatchSummary }) {
  const slots = view ? slotMapDistribution(view) : [];
  return (
    <Card
      title="Veto"
      sub={vetoes.length ? "Published veto" : view?.vetoKnown ? "Veto known to the model" : "Predicted map per slot (veto not published yet)"}
    >
      {vetoes.length > 0 ? (
        <div className="veto-steps">
          {[...vetoes]
            .sort((a, b) => a.step - b.step)
            .map((v) => (
              <div key={v.step} className={`veto-step ${v.action}`}>
                <span className="faint">#{v.step}</span>
                <span className="map">{v.map_name}</span>
                <Badge tone={v.action === "pick" ? "pos" : v.action === "ban" ? "neg" : "neutral"}>{v.action}</Badge>
                <span className={`small ${v.team ?? ""}`}>{v.team ? slotTeam(v.team, match) : "leftover"}</span>
              </div>
            ))}
        </div>
      ) : slots.length ? (
        <div className="grid grid-3">
          {slots.map((slot) => (
            <div key={slot.mapNumber} className="stack" style={{ gap: "0.35rem" }}>
              <div className="row-between">
                <strong>Map {slot.mapNumber}</strong>
                {slot.pPlayed !== null && <span className="small muted">played {formatPct(slot.pPlayed, 0)}</span>}
              </div>
              {slot.maps.slice(0, 7).map((m) => (
                <HBar key={m.map} label={m.map} value={m.p} max={Math.max(...slot.maps.map((x) => x.p), 0.01)} />
              ))}
              {!slot.maps.length && <span className="faint small">no distribution</span>}
            </div>
          ))}
        </div>
      ) : (
        <Empty>No veto information.</Empty>
      )}
      {!vetoes.length && view && view.vetoSequences.length > 0 && (
        <details style={{ marginTop: "0.75rem" }}>
          <summary className="small">Most likely map sequences</summary>
          <table className="table table-compact" style={{ marginTop: "0.5rem" }}>
            <tbody>
              {view.vetoSequences.slice(0, 8).map((seq, i) => (
                <tr key={i}>
                  <td style={{ textTransform: "capitalize" }}>
                    {seq.maps.map((m, j) => (
                      <Fragment key={j}>
                        {j > 0 && <span className="faint"> → </span>}
                        <span className={seq.pickers[j] ?? undefined} title={`picked by ${slotTeam(seq.pickers[j], match)}`}>
                          {m}
                        </span>
                      </Fragment>
                    ))}
                  </td>
                  <td className="num">{formatPct(seq.probability)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------------------- win probabilities

export function WinProbabilityCard({ view, match }: { view: AnalysisView; match: MatchSummary }) {
  const t1 = teamName(match.team1, "team1");
  const t2 = teamName(match.team2, "team2");
  return (
    <Card
      title="Win probabilities"
      sub={
        <span className="legend">
          <span>
            <i className="swatch team1" /> {t1}
          </span>
          <span>
            <i className="swatch team2" /> {t2}
          </span>
          <span>bracket = −1σ / +1σ strength scenarios</span>
        </span>
      }
    >
      <div className="stack">
        <div>
          <div className="row-between small">
            <strong>Match</strong>
            <span className="muted">
              σ {formatNumber(view.sigmaTeam1Win, 3)}
              {view.pDraw ? ` · draw ${formatPct(view.pDraw)}` : ""}
            </span>
          </div>
          <ProbBar p={view.pTeam1Win} low={view.pTeam1WinLow} high={view.pTeam1WinHigh} label1={t1} label2={t2} />
        </div>
        {view.slots.map((slot) => (
          <div key={slot.mapNumber}>
            <div className="row-between small">
              <span>
                <strong>Map {slot.mapNumber}</strong>{" "}
                <span className="muted" style={{ textTransform: "capitalize" }}>
                  {slot.mapDistribution[0] ? `${slot.mapDistribution[0].map} ${formatPct(slot.mapDistribution[0].p, 0)}` : ""}
                </span>
              </span>
              <span className="muted">played {formatPct(slot.pPlayed, 0)}</span>
            </div>
            <ProbBar p={slot.pTeam1Win} low={slot.pTeam1WinLow} high={slot.pTeam1WinHigh} label1={t1} label2={t2} />
          </div>
        ))}
        {view.mapScore.length > 0 && (
          <div>
            <h3>Map score</h3>
            <div className="stack" style={{ gap: "0.3rem" }}>
              {view.mapScore.map((s) => (
                <HBar key={s.score} label={s.score} value={s.p} max={Math.max(...view.mapScore.map((x) => x.p))} />
              ))}
            </div>
          </div>
        )}
        {view.mapEstimates.length > 0 && (
          <div>
            <h3>Per-map estimates ({t1} win, ± σ)</h3>
            <table className="table table-compact">
              <thead>
                <tr>
                  <th>Map</th>
                  <th style={{ width: "50%" }}>{t1} win</th>
                  <th className="num">p</th>
                  <th className="num">σ</th>
                  <th>Picked by</th>
                </tr>
              </thead>
              <tbody>
                {view.mapEstimates.map((e) => (
                  <tr key={e.key}>
                    <td style={{ textTransform: "capitalize" }}>{e.map}</td>
                    <td>
                      <ProbBar
                        p={e.pTeam1Win}
                        low={e.sigma !== null ? e.pTeam1Win - e.sigma : null}
                        high={e.sigma !== null ? e.pTeam1Win + e.sigma : null}
                        label1={t1}
                        label2={t2}
                      />
                    </td>
                    <td className="num">{formatPct(e.pTeam1Win)}</td>
                    <td className="num">{formatNumber(e.sigma, 3)}</td>
                    <td className="small">{e.pickedBy ? slotTeam(e.pickedBy, match) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------------------- rounds

export function RoundDistributionsCard({ view, match }: { view: AnalysisView; match: MatchSummary }) {
  const colors = useChartColors();
  const slotsWithScores = view.slots.filter((s) => Object.keys(s.scorePmf).length > 0);
  const [selected, setSelected] = useState<string>(String(slotsWithScores[0]?.mapNumber ?? 1));
  const slot = slotsWithScores.find((s) => String(s.mapNumber) === selected) ?? slotsWithScores[0];
  if (!slot) {
    return (
      <Card title="Round distributions">
        <Empty>The analysis has no simulated map scores.</Empty>
      </Card>
    );
  }
  const totals = roundTotalBars(slot.scorePmf);
  const margins = roundMarginBars(slot.scorePmf);
  const totalStats = pmfStats(totals);
  const marginStats = pmfStats(margins);
  const t1 = teamName(match.team1, "team1");
  return (
    <Card
      title="Round distributions"
      sub="Simulated final scores, conditional on the map being played"
      actions={
        <Segmented<string>
          label="Map"
          value={String(slot.mapNumber)}
          onChange={setSelected}
          options={slotsWithScores.map((s) => ({ value: String(s.mapNumber), label: `Map ${s.mapNumber}` }))}
        />
      }
    >
      <div className="grid grid-2">
        <div>
          <h3>Total rounds</h3>
          <div className="small muted">
            mean {formatNumber(totalStats.mean, 1)} · sd {formatNumber(totalStats.sd, 1)} · over 21.5{" "}
            {formatPct(probOver(totals, 21.5))} · over 24.5 {formatPct(probOver(totals, 24.5))}
          </div>
          <Histogram bars={totals} xLabel="total rounds" refLine={totalStats.mean !== null ? Math.round(totalStats.mean * 2) / 2 : null} refLabel="mean" />
        </div>
        <div>
          <h3>Round margin ({t1} − opponent)</h3>
          <div className="small muted">
            mean {formatNumber(marginStats.mean, 1)} · {t1} −4.5 covers {formatPct(probOver(margins, 4.5))} · +4.5 covers{" "}
            {formatPct(probOver(margins, -4.5))}
          </div>
          <Histogram bars={margins} xLabel="round margin" refLine={0} colorFor={(x) => (x > 0 ? colors.team1 : colors.team2)} />
        </div>
      </div>
      <div className="grid grid-3" style={{ marginTop: "0.5rem" }}>
        <div className="stat">
          <span className="stat-label">Overtime</span>
          <span className="stat-value">{formatPct(slot.pOvertime)}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Pistol rounds ({t1})</span>
          <span className="stat-value" style={{ fontSize: "1.1rem" }}>
            {slot.pistol.length
              ? slot.pistol.map((p) => `R${p.round} ${formatPct(p.pTeam1, 0)}`).join(" · ")
              : "—"}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Halves ({t1})</span>
          <span className="stat-value" style={{ fontSize: "1.1rem" }}>
            {slot.halves.length
              ? slot.halves
                  .map((h) => `H${h.half} ${formatPct(h.pTeam1, 0)}${h.pDraw !== null ? ` (6-6 ${formatPct(h.pDraw, 0)})` : ""}`)
                  .join(" · ")
              : "—"}
          </span>
        </div>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------------------- players

export function PlayersCard({ view, match }: { view: AnalysisView; match: MatchSummary }) {
  const scopes = sortScopes(Object.keys(view.playerScopes));
  const [scope, setScope] = useState<string>(scopes[0] ?? "match");
  const [expanded, setExpanded] = useState<number | null>(null);
  const current = scopes.includes(scope) ? scope : scopes[0];
  const players: PlayerView[] = current ? (view.playerScopes[current] ?? []) : [];
  return (
    <Card
      title="Player kill projections"
      sub={current === "match" ? "Kills over the maps actually played" : "Conditional on the map(s) being played"}
      actions={
        scopes.length > 1 && (
          <select aria-label="Kill projection scope" value={current} onChange={(e) => setScope(e.target.value)}>
            {scopes.map((s) => (
              <option key={s} value={s}>
                {scopeLabel(s)}
              </option>
            ))}
          </select>
        )
      }
    >
      {!players.length ? (
        <Empty>No player projections in this analysis.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table table-compact">
            <thead>
              <tr>
                <th>Player</th>
                <th>Team</th>
                <th className="num">Kills (mean)</th>
                <th className="num">sd</th>
                <th className="num">80% range</th>
                <th className="num">Deaths</th>
              </tr>
            </thead>
            <tbody>
              {players.map((p) => {
                const isOpen = expanded === p.playerId;
                const range = quantileRange(p);
                return (
                  <Fragment key={`${p.playerId}-${p.nickname}`}>
                    <tr className={`clickable ${isOpen ? "expanded" : ""}`} onClick={() => setExpanded(isOpen ? null : p.playerId)} aria-expanded={isOpen}>
                      <td>
                        <strong>{p.nickname}</strong>
                      </td>
                      <td className={p.team ?? ""}>{slotTeam(p.team, match)}</td>
                      <td className="num">
                        <strong>{formatNumber(p.mean, 1)}</strong>
                      </td>
                      <td className="num">{formatNumber(p.sd, 1)}</td>
                      <td className="num">{range}</td>
                      <td className="num">{formatNumber(p.deathsMean, 1)}</td>
                    </tr>
                    {isOpen && (
                      <tr className="detail-row">
                        <td colSpan={6}>
                          {p.bars.length ? (
                            <Histogram bars={p.bars} xLabel="kills" refLine={Math.round(p.mean * 2) / 2} refLabel="mean" height={200} />
                          ) : (
                            <span className="muted small">No kill distribution.</span>
                          )}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

/** 10th-90th percentile of the kills pmf, or mean ± 1.28 sd when there is no pmf. */
function quantileRange(p: PlayerView): string {
  const total = p.bars.reduce((s, b) => s + b.p, 0);
  if (total > 0) {
    let acc = 0;
    let lo: number | null = null;
    let hi: number | null = null;
    for (const b of p.bars) {
      acc += b.p / total;
      if (lo === null && acc >= 0.1) lo = b.x;
      if (hi === null && acc >= 0.9) hi = b.x;
    }
    if (lo !== null && hi !== null) return `${lo}–${hi}`;
  }
  return `${Math.max(0, Math.round(p.mean - 1.28 * p.sd))}–${Math.round(p.mean + 1.28 * p.sd)}`;
}

// ---------------------------------------------------------------------------------------- lineups, maps, flags

export function LineupsCard({ lineups, match }: { lineups: Lineups; match: MatchSummary }) {
  const side = (slot: TeamSlot) => (
    <div>
      <h3 className={slot}>{slotTeam(slot, match)}</h3>
      {lineups[slot].length ? (
        <ul style={{ margin: 0, paddingLeft: "1.1rem" }}>
          {lineups[slot].map((p) => (
            <li key={p.player_id}>
              <strong>{p.nickname ?? `#${p.player_id}`}</strong>
              {p.role && p.role !== "unknown" && <span className="muted small"> · {humanize(p.role)}</span>}
              {p.is_standin && (
                <>
                  {" "}
                  <Badge tone="warn">stand-in</Badge>
                </>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <span className="faint small">lineup unknown</span>
      )}
    </div>
  );
  return (
    <Card title="Lineups">
      <div className="grid grid-2">
        {side("team1")}
        {side("team2")}
      </div>
    </Card>
  );
}

export function MapsPlayedCard({ maps, match }: { maps: MapOut[]; match: MatchSummary }) {
  if (!maps.length) return null;
  return (
    <Card title="Maps">
      <div className="table-wrap">
        <table className="table table-compact">
          <thead>
            <tr>
              <th>#</th>
              <th>Map</th>
              <th>Picked by</th>
              <th className="num">Score</th>
              <th>Winner</th>
              <th className="num">OT rounds</th>
            </tr>
          </thead>
          <tbody>
            {[...maps]
              .sort((a, b) => a.map_number - b.map_number)
              .map((m) => (
                <tr key={m.id}>
                  <td>{m.map_number}</td>
                  <td style={{ textTransform: "capitalize" }}>{m.map_name}</td>
                  <td className="small">{m.is_decider ? "decider" : m.picked_by ? slotTeam(m.picked_by, match) : "—"}</td>
                  <td className="num">
                    {m.team1_rounds ?? "–"}:{m.team2_rounds ?? "–"}
                  </td>
                  <td className={m.winner ?? ""}>{m.winner ? slotTeam(m.winner, match) : humanize(m.status)}</td>
                  <td className="num">{m.overtime_rounds || "—"}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

export function ContextFlagsCard({ flags, match }: { flags: ContextFlagOut[]; match: MatchSummary }) {
  return (
    <Card title="Context flags" sub="From the Claude context layer; logit shift is from the first team's perspective">
      {!flags.length ? (
        <Empty>No context flags for this match.</Empty>
      ) : (
        <div className="stack" style={{ gap: "0.6rem" }}>
          {flags.map((f) => (
            <div key={f.id} className={`alert ${f.veto ? "alert-critical" : f.active ? "alert-warning" : "alert-info"}`}>
              <div className="alert-body">
                <div className="row">
                  <strong>{humanize(f.flag_type)}</strong>
                  <span className={f.team ?? "muted"}>{f.team ? slotTeam(f.team, match) : "match-wide"}</span>
                  {f.veto && <Badge tone="neg">blocks betting</Badge>}
                  {!f.active && <Badge>inactive</Badge>}
                  <span className="small muted">
                    severity {formatPct(f.severity, 0)} · confidence {formatPct(f.confidence, 0)} · logit shift{" "}
                    {formatSigned(f.logit_shift, 2)}
                  </span>
                </div>
                <div style={{ marginTop: "0.25rem" }}>{f.rationale}</div>
                <div className="small muted" style={{ marginTop: "0.25rem" }}>
                  {f.model ?? "model ?"} · {formatDateTime(f.created_at)}
                  {f.expires_at ? ` · expires ${formatDateTime(f.expires_at)}` : ""}
                </div>
                {f.evidence.length > 0 && (
                  <ul className="small" style={{ margin: "0.3rem 0 0", paddingLeft: "1.1rem" }}>
                    {f.evidence.map((ev, i) => {
                      const text = typeof ev === "string" ? ev : JSON.stringify(ev);
                      const href = typeof ev === "string" ? safeHref(ev) : null;
                      return (
                        <li key={i}>
                          {href ? (
                            <a href={href} target="_blank" rel="noopener noreferrer nofollow">
                              {text}
                            </a>
                          ) : (
                            text
                          )}
                        </li>
                      );
                    })}
                  </ul>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
