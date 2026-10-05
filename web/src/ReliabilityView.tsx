import { useEffect, useState } from "react";
import { api, DefectRow, localTime, Meta, Reliability } from "./api";

function shape(beta: number | null, lo: number | null): { text: string; cls: string } {
  if (beta == null) return { text: "too few failures", cls: "" };
  if (lo != null && lo > 1.1) return { text: "wear-out: replace before failure helps", cls: "s-SCHEDULED" };
  if (beta < 1.2) return { text: "random: preventive replacement wastes spares", cls: "s-NMCM" };
  return { text: "mild wear-out", cls: "" };
}

const f0 = (x: number | null | undefined) => (x == null ? "–" : Math.round(x).toLocaleString());
const pc = (x: number | null | undefined) => (x == null ? "–" : `${Math.round(100 * x)} %`);

export default function ReliabilityView({ meta }: { meta: Meta }) {
  const [r, setR] = useState<Reliability | null>(null);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<DefectRow[] | null>(null);
  useEffect(() => { api.reliability().then(setR); }, []);
  useEffect(() => {
    if (q.trim().length < 3) { setHits(null); return; }
    const t = setTimeout(() => api.defects(q.trim(), 40).then(setHits), 300);
    return () => clearTimeout(t);
  }, [q]);
  if (!r) return <p className="muted">Loading reliability…</p>;
  if (!r.kpis) return <p className="muted">Reliability not built for this store (python -m airpower.reliability.analyse build).</p>;
  const k = r.kpis;
  const off = meta.utc_offset_h;
  return (
    <>
      <div className="grid g4">
        <div className="card"><h2>Flight hours between defects</h2><div className="big">{k.mfhbd_h.toFixed(1)}</div>
          <p className="muted" style={{ margin: "6px 0 0" }}>{k.defects.toLocaleString()} defects in {f0(k.fleet_flight_hours)} FH</p></div>
        <div className="card"><h2>Flight hours between unscheduled removals</h2><div className="big">{k.mfhbur_h.toFixed(1)}</div>
          <p className="muted" style={{ margin: "6px 0 0" }}>{k.unscheduled_removals.toLocaleString()} removals; {pc(k.nff_share)} no fault found</p></div>
        <div className="card"><h2>Unscheduled repair time</h2><div className="big">{k.mttr_unscheduled_h_median.toFixed(1)}<small> h median</small></div></div>
        <div className="card"><h2>Waiting for a part</h2><div className="big">{k.logistics_delay_days_mean?.toFixed(1) ?? "–"}<small> days mean</small></div>
          <p className="muted" style={{ margin: "6px 0 0" }}>{pc(k.demands_waiting_over_7_days)} of demands wait more than 7 days</p></div>
      </div>

      <div className="card">
        <h2>Part reliability</h2>
        <p className="muted" style={{ marginTop: -6 }}>Component hours = hours flown while fitted. Weibull shape from units installed inside the data window;
          NFF from the stores system's repair findings.</p>
        <div style={{ overflowX: "auto" }}>
          <table>
            <thead><tr><th>Part</th><th className="num">Fitted</th><th className="num">Unsched. removals</th><th className="num">MTBUR (h)</th>
              <th className="num">MTBF (h)</th><th className="num">NFF</th><th className="num">Weibull β (95 %)</th><th className="num">η (h)</th><th>Failure pattern</th></tr></thead>
            <tbody>{r.parts.map((p) => {
              const s = shape(p.weibull_beta ?? p.weibull_beta_landings, p.weibull_beta_lo);
              return (
                <tr key={p.pn}>
                  <td>{p.description}<br /><code>{p.pn}</code> <span className="muted">ATA {p.ata}</span></td>
                  <td className="num">{p.installed}</td><td className="num">{p.unscheduled}</td>
                  <td className="num">{f0(p.mtbur_h)}</td><td className="num">{f0(p.mtbf_h)}</td><td className="num">{pc(p.nff_rate)}</td>
                  <td className="num">{p.weibull_beta != null ? `${p.weibull_beta.toFixed(2)} (${p.weibull_beta_lo?.toFixed(2)}–${p.weibull_beta_hi?.toFixed(2)})`
                    : p.weibull_beta_landings != null ? `${p.weibull_beta_landings.toFixed(2)} (landings)` : "–"}</td>
                  <td className="num">{f0(p.weibull_eta_h)}</td>
                  <td>{s.cls ? <span className={`pill ${s.cls}`}>{s.text}</span> : <span className="muted">{s.text}</span>}</td>
                </tr>);
            })}</tbody>
          </table>
        </div>
      </div>

      <div className="grid g2">
        <div className="card">
          <h2>Repeat defects (same aircraft, same ATA chapter, within 15 days)</h2>
          <table>
            <thead><tr><th>Aircraft</th><th>Defect</th><th className="num">Times</th><th>Last</th></tr></thead>
            <tbody>{r.repeats.slice(0, 15).map((x) => (
              <tr key={x.chain_id}><td><b>{x.tail}</b></td><td>{x.text}<br /><span className="muted">ATA {x.ata}</span></td>
                <td className="num">{x.occurrences}</td><td>{localTime(x.last, off, false)}</td></tr>))}</tbody>
          </table>
        </div>
        <div className="card">
          <h2>Rogue serial candidates</h2>
          <p className="muted" style={{ marginTop: -6 }}>Units whose lives look like a third of their type's, or removed far more often than their part number.</p>
          {r.rogues.length === 0 ? <p className="muted">None flagged.</p> : (
            <table>
              <thead><tr><th>Serial</th><th className="num">Removals</th><th className="num">Expected</th><th>Aircraft</th></tr></thead>
              <tbody>{r.rogues.map((x) => (
                <tr key={x.pn + x.sn}><td><code>{x.sn}</code><br /><span className="muted">{x.pn}</span></td>
                  <td className="num">{x.removals}{x.nff ? ` (${x.nff} NFF)` : ""}</td><td className="num">{x.expected.toFixed(1)}</td>
                  <td>{x.tails}</td></tr>))}</tbody>
            </table>
          )}
        </div>
      </div>

      <div className="card">
        <h2>Search the records</h2>
        <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. hydraulic pressure, chip light, brake"
               aria-label="Search defects" style={{ width: "100%", padding: 8, border: "1px solid var(--line)", borderRadius: 6, background: "var(--panel)", color: "var(--ink)" }} />
        {hits && (
          <table style={{ marginTop: 10 }}>
            <thead><tr><th>Defect</th><th>Aircraft</th><th>Text</th><th>Rectification</th></tr></thead>
            <tbody>{hits.map((h) => (
              <tr key={h.defect_no}><td><code>{h.defect_no}</code><br /><span className="muted">{localTime(h.raised, off, false)}</span></td>
                <td>{h.tail}</td><td>{h.text}<br /><span className="muted">ATA {h.ata ?? "?"}{h.ata_source === "inferred" ? " (inferred)" : ""}</span></td>
                <td className="muted">{h.rectification ?? "open"}</td></tr>))}</tbody>
          </table>
        )}
      </div>
    </>
  );
}
