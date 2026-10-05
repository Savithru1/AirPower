import { useEffect, useMemo, useState } from "react";
import { api, Meta, Plan, PlanDecision, PlanJob } from "./api";
import { Chart, css } from "./Chart";
import { useAuth } from "./auth";

const KIND_CLASS: Record<PlanJob["kind"], string> = { inspection: "s-SCHEDULED", condition: "s-NMCM", blocked: "s-NMCS" };

export default function PlanView({ meta, open }: { meta: Meta; open: (tail: string) => void }) {
  const [which, setWhich] = useState<"optimised" | "due_date">("optimised");
  const [p, setP] = useState<Plan | null>(null);
  const { can } = useAuth();
  const [dec, setDec] = useState<PlanDecision[]>([]);
  const [move, setMove] = useState<Record<string, number>>({});
  useEffect(() => { setP(null); api.plan(which).then(setP); }, [which]);
  useEffect(() => { api.decisions().then(setDec).catch(() => setDec([])); }, []);
  const decide = (j: PlanJob, decision: "accept" | "override") =>
    api.decide({ tail: j.tail, job: j.job, decision, ...(decision === "override" ? { start_day: move[j.tail + j.job] ?? j.start_day ?? 0 } : {}) })
      .then(() => api.decisions().then(setDec));
  const units = meta.units.map((u) => u.id);
  const chart = useMemo(() => {
    if (!p?.daily.length) return {};
    const H = p.summary?.horizon_days ?? 90;
    const palette = [css("--series-1"), css("--series-2"), css("--series-3")];
    return {
      tooltip: { trigger: "axis" },
      legend: { top: 0, textStyle: { color: css("--ink-2") } },
      xAxis: { type: "category", data: Array.from({ length: H }, (_, i) => `D+${i}`) },
      yAxis: { type: "value", name: "aircraft" },
      series: units.flatMap((u, i) => [
        { name: `${u} available`, type: "line", step: "middle", showSymbol: false, color: palette[i % 3],
          data: p.daily.filter((d) => d.unit === u).map((d) => d.available) },
        { name: `${u} required`, type: "line", step: "middle", showSymbol: false, color: palette[i % 3], lineStyle: { type: "dashed", width: 1 },
          data: p.daily.filter((d) => d.unit === u).map((d) => d.required) },
      ]),
    } as object;
  }, [p, units]);
  if (!p) return <p className="muted">Loading plan…</p>;
  if (!p.summary) return <p className="muted">No plan for this store (python -m airpower.plan.planner build).</p>;
  const s = p.summary;
  const o = s.optimised_plan, b = s.due_date_plan;
  const H = s.horizon_days;
  const tails = [...new Set(p.jobs.map((j) => j.tail))].sort();
  const t0 = new Date(s.t0 + "Z");
  return (
    <>
      <div className="card" style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
        <div><h2 style={{ margin: 0 }}>Maintenance plan</h2>
          <span className="muted">{H} days from {t0.toISOString().slice(0, 10)}: {s.inspections} inspections, {s.condition_jobs} predicted removals,
            {" "}{s.blocked_aircraft} aircraft already unserviceable. Solver {s.solver.status.toLowerCase()} in {s.solver.seconds} s.</span></div>
        <div className="tabs" style={{ marginLeft: "auto" }} role="tablist">
          <button role="tab" aria-selected={which === "optimised"} onClick={() => setWhich("optimised")}>Optimised</button>
          <button role="tab" aria-selected={which === "due_date"} onClick={() => setWhich("due_date")}>Due-date rule</button>
        </div>
      </div>
      <div className="grid g4">
        {[["Aircraft-days down", "down_aircraft_days"], ["Separate inductions", "inductions"], ["Removals bundled into inspections", "bundled"],
          ["Flying-task shortfall (aircraft-days)", "shortfall_aircraft_days"]].map(([label, k]) => (
          <div className="card" key={k}><h2>{label}</h2>
            <div className="big">{o ? (o as never)[k] : "–"}<small> vs {(b as never)[k]}</small></div>
            <p className="muted" style={{ margin: "6px 0 0" }}>optimised vs due-date rule</p></div>))}
      </div>
      <div className="card"><h2>Aircraft available vs required, per unit</h2><Chart option={chart} height={280} /></div>
      <div className="card">
        <h2>Per aircraft ({which === "optimised" ? "optimised" : "due-date rule"})</h2>
        <div className="legend" style={{ marginBottom: 8 }}>
          <span><i className="dot" style={{ background: "var(--sched)" }} />inspection</span>
          <span><i className="dot" style={{ background: "var(--nmcm)" }} />predicted removal (own induction)</span>
          <span><i className="dot" style={{ background: "var(--nmcs)" }} />already unserviceable</span>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "64px 1fr", gap: "3px 8px", fontSize: 12 }}>
          {tails.map((t) => (
            <div key={t} style={{ display: "contents" }}>
              <b>{t}</b>
              <div style={{ position: "relative", height: 16, background: "var(--panel-2)", borderRadius: 3 }}>
                {p.jobs.filter((j) => j.tail === t && j.start_day != null && j.start_day < H).map((j, i) => (
                  <span key={i} className={KIND_CLASS[j.kind]} title={`${j.job}: day ${j.start_day}–${j.end_day}${j.due_day != null ? ` (due ${j.due_day})` : ""}${j.reason ? "\n" + j.reason : ""}`}
                        style={{ position: "absolute", left: `${(100 * j.start_day!) / H}%`, width: `${Math.max(0.8, (100 * (Math.min(j.end_day!, H) - j.start_day!)) / H)}%`,
                                 top: 2, bottom: 2, background: "var(--c)", borderRadius: 2, opacity: 0.9 }} />))}
              </div>
            </div>))}
        </div>
        <p className="muted">Predicted removals bundled into an inspection do not appear as separate bars. Hover a bar for the job, its due day and why.</p>
      </div>
      <div className="card">
        <h2>Next 14 days: decide</h2>
        <table><thead><tr><th>Aircraft</th><th>Job</th><th className="num">Start</th><th className="num">Due</th><th>Why</th><th>Decision</th></tr></thead>
          <tbody>{p.jobs.filter((j) => j.kind !== "blocked" && j.start_day != null && j.start_day < 14).map((j) => {
            const k = j.tail + j.job, d = [...dec].reverse().find((x) => x.tail === j.tail && x.job === j.job);
            return (
              <tr key={k}><td><a href={`#aircraft/${j.tail}`} onClick={(e) => { e.preventDefault(); open(j.tail); }}><b>{j.tail}</b></a></td>
                <td>{j.job}{j.bundled_into ? <span className="muted"> (bundled into {j.bundled_into})</span> : null}<br /><span className="muted">{j.agency}</span></td>
                <td className="num">day {j.start_day}</td><td className="num">{j.due_day == null ? "–" : `day ${j.due_day}`}</td>
                <td className="muted">{j.reason ?? (j.kind === "inspection" ? "maintenance programme" : "")}</td>
                <td>{d ? <span className={`pill ${d.decision === "accept" ? "s-SERVICEABLE" : "s-NMCM"}`}>{d.decision === "accept" ? "accepted" : `moved to day ${d.start_day}`} · {d.user}</span>
                  : !can("decide_plan") ? <span className="muted">engineering officers decide</span> : <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                      <button className="x" onClick={() => decide(j, "accept")}>Accept</button>
                      <input type="number" min={0} max={H - 1} value={move[k] ?? j.start_day ?? 0} aria-label="move to day"
                             onChange={(e) => setMove({ ...move, [k]: Number(e.target.value) })} style={{ width: 56 }} />
                      <button className="x" onClick={() => decide(j, "override")}>Move</button></span>}</td></tr>);
          })}</tbody></table>
        <p className="muted">Decisions are recorded with the user and time (audit trail); the next plan run keeps accepted and moved jobs on their date.</p>
      </div>
    </>
  );
}
