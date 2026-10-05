import { useEffect, useState } from "react";
import { api, HealthMap as HM, Level, Meta } from "./api";

const LEVEL_CLASS: Record<Level, string> = { 0: "s-SERVICEABLE", 1: "s-NMCM", 2: "s-NMCS" };
const LEVEL_TEXT: Record<Level, string> = { 0: "ok", 1: "watch", 2: "alert" };
const STATUS_TEXT: Record<string, string> = { SERVICEABLE: "serviceable", NMCM: "in maintenance", NMCS: "awaiting spares", SCHEDULED: "scheduled" };

export default function HealthMap({ meta, at, open }: { meta: Meta; at: string; open: (tail: string) => void }) {
  const [m, setM] = useState<HM | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [only, setOnly] = useState(false);
  useEffect(() => { setM(null); api.healthmap(at).then(setM).catch((e) => setErr(String(e))); }, [at]);
  if (err) return <p className="err">{err}</p>;
  if (!m) return <p className="muted">Building the fleet health map…</p>;
  const count = (c: string, l: Level) => m.rows.filter((r) => r.cells[c]?.level === l).length;
  const rows = only ? m.rows.filter((r) => Object.values(r.cells).some((c) => c && c.level > 0)) : m.rows;
  return (
    <>
      <div className="card">
        <div style={{ display: "flex", gap: 16, alignItems: "baseline", flexWrap: "wrap" }}>
          <h2 style={{ margin: 0 }}>Fleet health map</h2>
          <span className="muted">Each cell is the worst finding for that system: HUMS model risk, life consumed against the limit, open defects,
            the next scheduled inspection and parts likely to be awaited. Click a row for the aircraft.</span>
          <label style={{ marginLeft: "auto", fontSize: 13 }}><input type="checkbox" checked={only} onChange={(e) => setOnly(e.target.checked)} /> only aircraft with findings</label>
        </div>
        <div className="legend" style={{ marginTop: 8 }}>
          <span><i className="dot" style={{ background: "var(--ok)" }} />ok</span>
          <span><i className="dot" style={{ background: "var(--nmcm)" }} />watch: plan it</span>
          <span><i className="dot" style={{ background: "var(--nmcs)" }} />alert: act now</span>
          <span><i className="dot" style={{ background: "var(--panel-2)", border: "1px solid var(--line)" }} />not fitted to this type</span>
        </div>
      </div>
      <div className="card" style={{ overflowX: "auto" }}>
        <table className="hm">
          <thead>
            <tr><th>Aircraft</th><th>Now</th>{m.columns.map((c) => <th key={c} className="c">{c}<br />
              <span className="muted" style={{ fontWeight: 400 }}>{count(c, 2)} alert · {count(c, 1)} watch</span></th>)}</tr>
          </thead>
          <tbody>
            {meta.units.map((u) => {
              const rs = rows.filter((r) => r.unit === u.id);
              if (!rs.length) return null;
              return [
                <tr key={u.id}><td colSpan={m.columns.length + 2} className="grp">{u.id} · {u.type_name} · {u.base}</td></tr>,
                ...rs.map((r) => (
                  <tr key={r.tail} className="click" onClick={() => open(r.tail)}>
                    <td><b>{r.tail}</b></td>
                    <td><span className={`pill s-${r.status ?? "SERVICEABLE"}`} title={r.reason ?? ""}>{STATUS_TEXT[r.status ?? "SERVICEABLE"]}</span></td>
                    {m.columns.map((c) => {
                      const cell = r.cells[c];
                      if (!cell) return <td key={c} className="hc na" aria-label="not fitted" />;
                      return (
                        <td key={c} className={`hc ${LEVEL_CLASS[cell.level]}`} title={`${r.tail} · ${c}: ${LEVEL_TEXT[cell.level]}\n${cell.note}`}>
                          <span>{c === "Open defects" ? (cell.n ?? 0) : c === "Next inspection" ? `${Math.max(0, Math.round(cell.days ?? 0))} d` : LEVEL_TEXT[cell.level]}</span>
                        </td>);
                    })}
                  </tr>)),
              ];
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
