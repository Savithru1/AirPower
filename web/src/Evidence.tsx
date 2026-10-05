import { useEffect, useMemo, useState } from "react";
import { api, CI, pct, pts, Study, TwinCheck } from "./api";
import { Chart, css } from "./Chart";

const LABEL: Record<string, string> = {
  P0: "P0 reactive (today)", P1: "P1 preventive (fixed intervals)", P2: "P2 predictive (AirPower)",
  "P0+METRIC": "P0 + recommended stock", "P2+METRIC": "P2 + recommended stock",
  "P2-rules": "P2, models as built", "P2-rules records": "P2 with no HUMS (records only)", "P2-rules no positioning": "P2 without spares positioning",
  "P2-rules x0": "P2, perfect prediction", "P2-rules x1.5": "P2, errors x1.5", "P2-rules x2": "P2, errors x2", "P2-rules x3": "P2, errors x3",
};
const MAIN = ["P0", "P1", "P2", "P0+METRIC", "P2+METRIC"];
// paired differences between arms (study.py): what each part of P2 adds
const PARTS: [string, string][] = [
  ["paired_P2-rules_vs_P2-rules records", "HUMS predictions (vs life limits and records only)"],
  ["paired_P2-rules_vs_P2-rules no positioning", "spares positioning"],
  ["paired_P2_vs_P2-rules", "the weekly CP-SAT plan (vs timing by rules)"],
  ["paired_P2-rules x0_vs_P2-rules", "perfect prediction would add"],
];

export default function Evidence() {
  const [s, setS] = useState<Study | null>(null);
  const [tv, setTv] = useState<TwinCheck[] | null>(null);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => { api.study().then((x) => { setS(x.study); setTv(x.twin_validation); setLoaded(true); }); }, []);
  const vop = useMemo(() => {
    if (!s) return {};
    const pts_ = s.value_of_prediction.filter((p) => p.error_multiple != null).sort((a, b) => a.error_multiple! - b.error_multiple!);
    const none = s.value_of_prediction.find((p) => p.error_multiple == null);
    const xs = pts_.map((p) => p.error_multiple!);
    return {
      tooltip: { trigger: "axis", valueFormatter: (v: unknown) => `${Number(v).toFixed(2)} pts` },
      legend: { top: 0, textStyle: { color: css("--ink-2") } },
      grid: { left: 50, right: 20, top: 34, bottom: 44 },
      xAxis: { type: "category", data: xs.map((x) => (x === 0 ? "perfect" : x === 1 ? "as built" : `x${x}`)), name: "prediction error", nameLocation: "middle", nameGap: 28 },
      yAxis: { type: "value", name: "availability gain over P0 (pts)" },
      series: [
        { name: "gain (95 % CI)", type: "line", color: css("--series-1"), data: pts_.map((p) => 100 * p.gain), symbolSize: 8 },
        { name: "CI low", type: "line", lineStyle: { type: "dotted", width: 1 }, color: css("--series-1"), showSymbol: false, data: pts_.map((p) => 100 * (p.gain - p.ci95)) },
        { name: "CI high", type: "line", lineStyle: { type: "dotted", width: 1 }, color: css("--series-1"), showSymbol: false, data: pts_.map((p) => 100 * (p.gain + p.ci95)) },
        ...(none ? [{ name: "no HUMS (records only)", type: "line", color: css("--series-3"), lineStyle: { type: "dashed" }, showSymbol: false,
                      data: xs.map(() => 100 * none.gain) }] : []),
      ],
    } as object;
  }, [s]);
  if (!loaded) return <p className="muted">Loading…</p>;
  if (!s) return <p className="muted">The policy study has not been run (python -m airpower.twin.study).</p>;
  const R = s.results, D = s.paired_vs_P0;
  const ci = (c: CI | undefined, d = 0) => (c ? `${c.mean.toFixed(d)}${c.ci95 != null ? ` ± ${c.ci95.toFixed(d)}` : ""}` : "–");
  const row = (n: string) => R[n] && (
    <tr key={n}><td><b>{LABEL[n] ?? n}</b></td>
      <td className="num">{pct(R[n].availability.mean, 1)}<br /><span className="muted">± {pct(R[n].availability.ci95, 1)}</span></td>
      <td className="num"><b>{D[n] ? pts(D[n].availability) : "–"}</b></td>
      <td className="num">{pct(R[n].task_met.mean, 1)}</td>
      <td className="num">{pct(R[n].nmcs.mean, 1)}</td>
      <td className="num">{ci(R[n].in_service_failures)}</td>
      <td className="num">{ci(R[n].removals)}<br /><span className="muted">{Math.round(R[n].nff_removals.mean)} NFF</span></td>
      <td className="num">{(R[n].stock_value_lakh.mean / 100).toFixed(0)}</td>
      <td className="num">{(R[n].material_cost_lakh.mean / 100).toFixed(0)}</td>
      <td className="num">{(R[n].labour_h.mean / 1000).toFixed(1)}k</td></tr>);
  const head = (<thead><tr><th>Policy</th><th className="num">Availability</th><th className="num">vs P0 (paired, 95 % CI)</th><th className="num">Task met</th>
    <th className="num">Awaiting spares</th><th className="num">In-service failures</th><th className="num">Removals</th>
    <th className="num">Stock (₹ cr)</th><th className="num">Repairs + buys (₹ cr/yr)</th><th className="num">Labour (h/yr)</th></tr></thead>);
  return (
    <>
      <div className="card">
        <h2>Does predictive maintenance raise availability? (simulated fleet)</h2>
        <p className="muted" style={{ marginTop: -6 }}>{s.reps} simulated years per policy on the same random futures (common random numbers), each after a
          one-year warm-up, with two exercises a year. 48 aircraft, two bases and a depot. The predictors in the loop are the platform's own models,
          trained on the real datasets and on separate simulated fleets, never on these runs.</p>
        <div style={{ overflowX: "auto" }}><table>{head}<tbody>{MAIN.map(row)}</tbody></table></div>
      </div>
      <div className="grid g2">
        <div className="card">
          <h2>Value of prediction</h2>
          <Chart option={vop} height={300} />
          <p className="muted">Each model's own errors are multiplied on purpose (x0 = perfect foresight, x1 = the models as built). Where the line crosses
            zero is how much worse the models can get before prediction stops paying: a poor predictor costs more in checks than it saves. The dashed line is P2
            with no HUMS at all.</p>
          <table><thead><tr><th>Arm</th><th className="num">Gain</th><th className="num">Engine RMSE (FH, last 125 cycles)</th><th className="num">Gearbox AUC</th><th className="num">Failures</th></tr></thead>
            <tbody>{s.value_of_prediction.map((p) => (
              <tr key={p.arm}><td>{LABEL[p.arm] ?? p.arm}</td><td className="num">{pts({ mean: p.gain, ci95: p.ci95 })}</td>
                <td className="num">{p.engine_rmse_fh == null ? "–" : Math.round(p.engine_rmse_fh)}</td>
                <td className="num">{p.gearbox_auc == null ? "–" : p.gearbox_auc.toFixed(2)}</td><td className="num">{Math.round(p.in_service_failures)}</td></tr>))}</tbody></table>
        </div>
        <div className="card">
          <h2>What each part of P2 adds</h2>
          <table><thead><tr><th>Part</th><th className="num">Availability (paired)</th></tr></thead>
            <tbody>
              {D["P2-rules records"] && <tr><td>life limits and records alone (kits, positioning), vs P0</td><td className="num">{pts(D["P2-rules records"].availability)}</td></tr>}
              {PARTS.filter(([k]) => s[k]).map(([k, label]) => (
                <tr key={k}><td>{label}</td><td className="num">{pts(s[k] as CI)}</td></tr>))}</tbody></table>
          {s.policy.P2 && (<>
            <h2 style={{ marginTop: 16 }}>P2 at work (per simulated year)</h2>
            <table><tbody>
              {[["kits_drawn", "spares drawn ahead of a predicted removal"], ["kits_handed_over", "held spares handed to a grounded aircraft"],
                ["findings_flown_on", "inspection findings flown on until the spare arrived"], ["bundled_removals", "removals done inside an inspection"],
                ["planned_removals", "short planned removals"], ["forced_removals", "removals forced by the prediction's lower bound"],
                ["confirmed", "predicted faults confirmed by an inspection"], ["checks_no_fault", "alerts checked, no fault found"],
                ["kits_released", "spares given back when an alert was cleared"],
                ["positioned", "spares moved to the base that will need them"], ["early_inspections", "inspections brought forward by the planner"]]
                .filter(([k]) => s.policy.P2[k] != null).map(([k, label]) => (
                  <tr key={k}><td>{label}</td><td className="num">{Math.round(s.policy.P2[k] / 2)}</td></tr>))}
            </tbody></table>
            <p className="muted">Counts cover the warm-up and the measured year, shown per year.</p></>)}
        </div>
      </div>
      {tv && (
        <div className="card">
          <h2>Can the twin forecast? Forecast from the records vs what the simulated fleet did next</h2>
          <table><thead><tr><th>Records</th><th className="num">Forecast availability (next {tv[0].forecast_days} days)</th><th className="num">Actual</th>
            <th className="num">Failures forecast / actual</th><th className="num">Backorders forecast / actual</th></tr></thead>
            <tbody>{tv.map((r) => (
              <tr key={r.run}><td>{r.run.split("/").pop()} ({r.records_days} days of records)</td>
                <td className="num">{pct(r.forecast.availability, 1)} <span className="muted">(sd {pct(r.forecast.sd, 1)})</span></td>
                <td className="num"><b>{pct(r.actual.availability, 1)}</b></td>
                <td className="num">{r.forecast.failures.toFixed(0)} / {r.actual.failures}</td>
                <td className="num">{r.forecast.backorders.toFixed(0)} / {r.actual.backorders}</td></tr>))}</tbody></table>
          <p className="muted">The twin reads only the store; "actual" is the same simulated run continued past the end of its records.</p>
        </div>)}
    </>
  );
}
