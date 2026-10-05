import { useEffect, useMemo, useState } from "react";
import { api, Spares } from "./api";
import { Chart, css } from "./Chart";

const pc = (x: number | null | undefined, d = 0) => (x == null ? "–" : `${(100 * x).toFixed(d)} %`);
const cr = (lakh: number | null | undefined) => (lakh == null ? "–" : `₹${(lakh / 100).toFixed(1)} cr`);

export default function SparesView() {
  const [s, setS] = useState<Spares | null>(null);
  useEffect(() => { api.spares().then(setS); }, []);
  const chart = useMemo(() => {
    if (!s?.metric) return {};
    const m = s.metric;
    return {
      tooltip: { trigger: "item" },
      grid: { left: 56, right: 20, top: 30, bottom: 44 },
      legend: { top: 0, textStyle: { color: css("--ink-2") } },
      xAxis: { type: "value", name: "Inventory value (₹ crore)", nameLocation: "middle", nameGap: 28,
               axisLabel: { formatter: (v: number) => (v / 100).toFixed(0) } },
      yAxis: { type: "value", min: 0, max: 1, axisLabel: { formatter: (v: number) => `${Math.round(v * 100)} %` } },
      series: [
        { name: "Best allocation (METRIC)", type: "line", showSymbol: false, color: css("--series-1"),
          data: m.curve.map((p) => [p.cost_lakh, p.availability]) },
        { name: "Stock held today", type: "scatter", symbolSize: 12, color: css("--nmcs"),
          data: [[m.baseline_cost_lakh, m.baseline_availability]] },
        { name: "Recommended", type: "scatter", symbolSize: 12, color: css("--ok"), data: [[m.metric_cost_lakh, m.metric_availability]] },
      ],
    } as object;
  }, [s]);
  if (!s) return <p className="muted">Loading spares…</p>;
  if (!s.metric) return <p className="muted">Spares model not built for this store (python -m airpower.spares.metric build).</p>;
  const m = s.metric;
  const arms = s.validation ? Object.entries(s.validation.arms) : [];
  const buys = s.targets.filter((t) => t.action === "buy");
  const reduce = s.targets.filter((t) => t.action === "reduce");
  return (
    <>
      <div className="grid g3">
        <div className="card"><h2>Stock held today</h2><div className="big">{pc(m.baseline_availability)}</div>
          <p className="muted" style={{ margin: "6px 0 0" }}>supply availability (aircraft not waiting for a part) for {cr(m.baseline_cost_lakh)} of spares</p></div>
        <div className="card"><h2>Within the same budget, best allocation</h2><div className="big">{pc(m.metric_availability)}</div>
          <p className="muted" style={{ margin: "6px 0 0" }}>for {cr(m.metric_cost_lakh)}: {buys.length} parts up, {reduce.length} parts down</p></div>
        <div className="card"><h2>Today's availability for less</h2><div className="big">{cr(m.metric_cost_to_match_baseline_lakh)}</div>
          <p className="muted" style={{ margin: "6px 0 0" }}>is enough with the best allocation</p></div>
      </div>
      <div className="grid g2">
        <div className="card"><h2>What spares money buys</h2><Chart option={chart} height={300} />
          <p className="muted">Model: Sherbrooke's METRIC on demand rates and repair / procurement pipelines estimated from the records.</p></div>
        <div className="card">
          <h2>Checked in the simulated fleet</h2>
          {arms.length === 0 ? <p className="muted">Not validated yet (python -m airpower.spares.validate).</p> : (
            <table>
              <thead><tr><th>Stock</th><th className="num">Value</th><th className="num">Availability</th><th className="num">vs today</th></tr></thead>
              <tbody>{arms.map(([name, a]) => (
                <tr key={name}><td>{name}</td><td className="num">{cr(a.stock_value_lakh)}</td>
                  <td className="num">{pc(a.availability, 1)}</td>
                  <td className="num">{a.paired_diff_vs_baseline ? `${(100 * a.paired_diff_vs_baseline.mean).toFixed(1)} ± ${(100 * a.paired_diff_vs_baseline.ci95).toFixed(1)} pts` : "–"}</td></tr>))}</tbody>
            </table>
          )}
          {s.validation && <p className="muted">Allocation computed from a separate training fleet's records, then tested in fresh fleets: {s.validation.reps} paired simulated years per arm; availability includes maintenance and scheduled down time.</p>}
          {s.forecast_backtest && (
            <p className="muted">Demand forecast backtest ({s.forecast_backtest.horizon_days}-day horizon, high-value parts): error {pc(s.forecast_backtest.high_value_ge_10_lakh.predictive.wape)} from
              predictions vs {pc(s.forecast_backtest.high_value_ge_10_lakh.sba.wape)} from demand history (SBA).</p>)}
        </div>
      </div>
      <div className="card">
        <h2>Aircraft at risk of waiting for a part</h2>
        <p className="muted" style={{ marginTop: -6 }}>Units likely to need a replacement within the part's lead time, at bases with no serviceable spare.</p>
        {s.aog.length === 0 ? <p className="muted">None.</p> : (
          <table>
            <thead><tr><th>Aircraft</th><th>Part</th><th className="num">P(needed within lead time)</th><th className="num">Lead (days)</th><th>Basis</th><th>Remedy</th></tr></thead>
            <tbody>{s.aog.slice(0, 15).map((a) => (
              <tr key={a.tail + a.position}><td><b>{a.tail}</b></td><td>{a.position}<br /><code>{a.pn}</code></td>
                <td className="num">{pc(a.p_within_lead)}</td><td className="num">{Math.round(a.lead_days)}</td><td>{a.basis.replace("_", " ")}</td>
                <td>{a.fleet_stock > 0 ? <span className="pill s-NMCM">transfer from another store</span> : <span className="pill s-NMCS">none in stock: expedite</span>}</td></tr>))}</tbody>
          </table>
        )}
      </div>
      <div className="card">
        <h2>Recommended stock levels</h2>
        <div style={{ overflowX: "auto" }}>
          <table>
            <thead><tr><th>Part</th><th className="num">Unit cost</th><th className="num">Demand / month</th><th className="num">Pipeline (days)</th>
              <th className="num">Held now</th><th className="num">Recommended</th><th>Action</th></tr></thead>
            <tbody>{s.targets.filter((t) => t.action !== "hold").map((t) => (
              <tr key={t.pn}><td>{t.description}<br /><code>{t.pn}</code></td><td className="num">₹{t.cost_lakh.toFixed(1)} L</td>
                <td className="num">{(t.demand_per_day * 30).toFixed(1)}</td><td className="num">{Math.round(t.pipeline_days)}</td>
                <td className="num">{t.held_now}</td><td className="num">{t.metric_stock}</td>
                <td><span className={`pill ${t.action === "buy" ? "s-SCHEDULED" : "s-NMCM"}`}>{t.action} {t.qty}</span></td></tr>))}</tbody>
          </table>
        </div>
      </div>
    </>
  );
}
