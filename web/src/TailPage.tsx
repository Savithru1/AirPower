import { useEffect, useMemo, useState } from "react";
import { AircraftDetail, api, localTime, Meta, pct, Risk, TailTwin } from "./api";
import { Chart, css } from "./Chart";
import { Legend } from "./Readiness";
import { useAuth } from "./auth";

const STATUS_TEXT: Record<string, string> = { SERVICEABLE: "Serviceable", NMCM: "In maintenance", NMCS: "Awaiting spares", SCHEDULED: "Scheduled servicing" };
const RISK_CLASS: Record<Risk["status"], string> = { ok: "s-SERVICEABLE", watch: "s-NMCM", alert: "s-NMCS" };
const PRIO: Record<number, [string, string]> = { 1: ["act now", "s-NMCS"], 2: ["plan", "s-NMCM"], 3: ["scheduled", "s-SCHEDULED"] };
const KIND: Record<Risk["kind"], string> = { engine_rul: "engine trend", gearbox_anomaly: "gearbox vibration", battery_rul: "battery capacity" };

export default function TailPage({ tail, at, meta, back }: { tail: string; at: string; meta: Meta; back: () => void }) {
  const [t, setT] = useState<TailTwin | null>(null);
  const [d, setD] = useState<AircraftDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [ch, setCh] = useState("");
  const [series, setSeries] = useState<{ ts: string; value: number; sn: string }[]>([]);
  const [q, setQ] = useState("");
  const [ata, setAta] = useState<number | null>(null);
  const [showCfg, setShowCfg] = useState(false);
  const off = meta.utc_offset_h;
  const { can } = useAuth();
  useEffect(() => {
    setT(null); setD(null); setErr(null);
    api.twin(tail, at).then(setT).catch((e) => setErr(String(e)));
    api.aircraft(tail, at).then((x) => { setD(x); const c = x.hums_channels[0]; setCh(c ? `${c.position}|${c.channel}` : ""); }).catch(() => undefined);
  }, [tail, at]);
  useEffect(() => {
    if (!ch) { setSeries([]); return; }
    const [p, c] = ch.split("|");
    api.health(tail, p, c).then(setSeries);
  }, [tail, ch]);

  const rulChart = useMemo(() => {
    const rows = (t?.risk ?? []).filter((r) => r.rul_fh != null);
    return {
      grid: { left: 70, right: 24, top: 10, bottom: 34 },
      tooltip: { trigger: "item" },
      xAxis: { type: "value", name: "remaining flight hours", nameLocation: "middle", nameGap: 24 },
      yAxis: { type: "category", data: rows.map((r) => r.position), inverse: true },
      series: [
        { type: "bar", stack: "i", itemStyle: { color: "transparent" }, data: rows.map((r) => r.rul_fh_lo ?? 0), tooltip: { show: false } },
        { type: "bar", stack: "i", barWidth: 10, name: "90 % interval",
          data: rows.map((r) => ({ value: (r.rul_fh_hi ?? r.rul_fh!) - (r.rul_fh_lo ?? r.rul_fh!),
                                   itemStyle: { color: css(r.status === "alert" ? "--nmcs" : r.status === "watch" ? "--nmcm" : "--ok"), opacity: 0.45 } })),
          tooltip: { formatter: (p: { dataIndex: number }) => { const r = rows[p.dataIndex]; return `${r.position}: ${Math.round(r.rul_fh!)} FH (90 % ${Math.round(r.rul_fh_lo ?? 0)}–${Math.round(r.rul_fh_hi ?? 0)})`; } } },
        { type: "scatter", symbolSize: 10, name: "prediction", data: rows.map((r, i) => [r.rul_fh, i]), itemStyle: { color: css("--ink") },
          tooltip: { formatter: (p: { dataIndex: number }) => `${rows[p.dataIndex].position}: ${Math.round(rows[p.dataIndex].rul_fh!)} FH` } },
      ],
    } as object;
  }, [t]);
  const trend = useMemo(() => ({
    tooltip: { trigger: "axis" }, xAxis: { type: "time" }, yAxis: { type: "value", scale: true },
    series: [{ type: "line", showSymbol: false, color: css("--accent"), data: series.map((s) => [s.ts + "Z", s.value]) }],
  }), [series]);

  if (err) return <p className="err">{err}</p>;
  if (!t) return <p className="muted">Loading {tail}…</p>;
  const a = t.aircraft;
  const defects = t.defects.filter((x) => (ata == null || x.ata === ata) && (!q || `${x.text} ${x.rectification ?? ""}`.toLowerCase().includes(q.toLowerCase())));
  const t0 = d ? new Date(d.timeline_90d[0]?.start ?? d.at).getTime() : 0, t1 = d ? new Date(d.at).getTime() : 1;
  return (
    <>
      <div className="card" style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
        <button className="x" onClick={back} aria-label="Back">← Back</button>
        <div>
          <h3 style={{ fontSize: 22 }}>{a.tail} <span className={`pill s-${t.status?.status ?? "SERVICEABLE"}`}>{STATUS_TEXT[t.status?.status ?? "SERVICEABLE"]}</span></h3>
          <div className="muted">MSN {a.msn} · type {a.type} · {a.unit} · {a.base}{a.hums_unit ? ` · HUMS ${a.hums_unit}` : ""} · flying {t.usage_fh_per_day.toFixed(2)} FH/day
            {t.status?.reason ? ` · ${t.status.reason} since ${localTime(t.status.since, off)}` : ""}</div>
        </div>
        {can("export_reports") && <a className="x" style={{ marginLeft: "auto", textDecoration: "none" }} href={`/report/aircraft/${a.tail}?at=${encodeURIComponent(at)}`} target="_blank" rel="noreferrer">Aircraft report (print / PDF)</a>}
      </div>

      <div className="card">
        <h2>Recommended actions ({t.actions.length})</h2>
        {t.actions.length === 0 ? <p className="muted">Nothing to do beyond the maintenance programme.</p> : (
          <table><tbody>{t.actions.map((x, i) => (
            <tr key={i}><td style={{ width: 90 }}><span className={`pill ${PRIO[x.priority][1]}`}>{PRIO[x.priority][0]}</span></td>
              <td><b>{x.action}</b><br /><span className="muted">{x.why}</span></td><td className="muted" style={{ width: 130 }}>{x.source}</td></tr>))}</tbody></table>
        )}
      </div>

      <div className="grid g2">
        <div className="card">
          <h2>Component health: predicted remaining life</h2>
          {t.risk.some((r) => r.rul_fh != null) ? <Chart option={rulChart} height={60 + 28 * t.risk.filter((r) => r.rul_fh != null).length} />
            : <p className="muted">No remaining-life models for this type's monitored components.</p>}
          <table><thead><tr><th>Component</th><th>Model</th><th className="num">P(removal ≤ 100 FH)</th><th>Status</th></tr></thead>
            <tbody>{t.risk.map((r) => (
              <tr key={r.position + r.kind}><td>{r.position}<br /><code>{r.sn}</code></td><td>{KIND[r.kind]}<br /><span className="muted">{r.model}</span></td>
                <td className="num">{pct(r.p_fail_100fh)}</td><td><span className={`pill ${RISK_CLASS[r.status]}`}>{r.status}</span></td></tr>))}</tbody></table>
        </div>
        <div className="card">
          <h2>Scheduled inspections</h2>
          <table><thead><tr><th>Inspection</th><th className="num">FH since</th><th className="num">Due in</th></tr></thead>
            <tbody>{t.inspections.map((r) => (
              <tr key={r.inspection}><td>{r.inspection} <span className="muted">({r.level})</span><br /><span className="muted">last {r.last_done ? localTime(r.last_done, off, false) : "–"}</span></td>
                <td className="num">{Math.round(r.fh_since)}</td>
                <td className="num"><span className={`pill ${r.days_to_due < 0 ? "s-NMCS" : r.days_to_due < 7 ? "s-NMCM" : ""}`}>{Math.round(r.days_to_due)} days</span><br /><span className="muted">{r.due}</span></td></tr>))}</tbody></table>
        </div>
      </div>

      <div className="card">
        <h2>Life-limited parts</h2>
        <table><thead><tr><th>Position</th><th>Part / serial</th><th style={{ width: "34%" }}>Life consumed</th><th className="num">Days left</th><th>Due</th></tr></thead>
          <tbody>{[...t.life].sort((x, y) => (y.consumed ?? 0) - (x.consumed ?? 0)).map((r) => {
            const c = r.consumed ?? 0, cls = c >= 1 ? "s-NMCS" : c >= 0.9 ? "s-NMCM" : "s-SERVICEABLE";
            return (
              <tr key={r.position}><td>{r.position}</td><td><code>{r.pn}</code> <code>{r.sn}</code></td>
                <td><div className={`bar ${cls}`} style={{ margin: "4px 0" }}><span style={{ width: `${Math.min(100, 100 * c)}%`, background: "var(--c)" }} /></div>
                  <span className="muted">{r.consumed == null ? r.basis : `${pct(r.consumed)} · ${r.basis}`}</span></td>
                <td className="num">{r.days_left == null ? "–" : Math.round(r.days_left)}</td><td>{r.due ?? "–"}</td></tr>);
          })}</tbody></table>
      </div>

      <div className="grid g2">
        <div className="card">
          <h2>HUMS trend</h2>
          {d && d.hums_channels.length > 0 ? (<>
            <select value={ch} onChange={(e) => setCh(e.target.value)} aria-label="HUMS channel">
              {d.hums_channels.map((c) => <option key={`${c.position}|${c.channel}`} value={`${c.position}|${c.channel}`}>{c.position} · {c.channel}</option>)}
            </select>
            <Chart option={trend} height={230} /></>) : <p className="muted">No HUMS data.</p>}
        </div>
        <div className="card">
          <h2>Last 90 days</h2>
          {d && (<>
            <div className="timeline">
              {d.timeline_90d.map((s, i) => {
                const x0 = Math.max(new Date(s.start).getTime(), t0), x1 = Math.min(new Date(s.end).getTime(), t1);
                return <span key={i} className={`s-${s.status}`} style={{ width: `${(100 * Math.max(x1 - x0, 0)) / (t1 - t0)}%`, background: "var(--c)" }}
                             title={`${s.status} ${localTime(s.start, off)} – ${localTime(s.end, off)}${s.reason ? "\n" + s.reason : ""}`} />;
              })}
            </div>
            <div style={{ marginTop: 8 }}><Legend /></div>
            <p className="muted">{d.last_30d.sorties} sorties, {d.last_30d.fh.toFixed(0)} FH in the last 30 days; {d.open_defects.length} open defects.</p></>)}
        </div>
      </div>

      <div className="card">
        <h2>Defect history ({t.defects_total})</h2>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginBottom: 8 }}>
          <input placeholder="search text" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search defects"
                 style={{ font: "inherit", padding: "4px 8px", border: "1px solid var(--line)", borderRadius: 6, background: "var(--panel)", color: "var(--ink)" }} />
          <span className="muted">ATA chapter:</span>
          <button className={`x ${ata == null ? "on" : ""}`} onClick={() => setAta(null)}>all</button>
          {t.defects_by_ata.map((x) => <button key={String(x.ata)} className={`x ${ata === x.ata ? "on" : ""}`} onClick={() => setAta(x.ata)}>{x.ata ?? "?"} ({x.n})</button>)}
        </div>
        <div style={{ maxHeight: 420, overflow: "auto" }}>
          <table><thead><tr><th>Raised</th><th>ATA</th><th>Report</th><th>Rectification</th></tr></thead>
            <tbody>{defects.slice(0, 150).map((x) => (
              <tr key={x.defect_no}><td className="muted" style={{ whiteSpace: "nowrap" }}>{localTime(x.raised, off, false)}<br /><code>{x.defect_no}</code></td>
                <td>{x.ata ?? "?"}{x.ata_source === "inferred" ? <span className="muted"> (inferred)</span> : null}</td>
                <td>{x.text}<br /><span className="muted">{x.category}</span></td><td className="muted">{x.rectification ?? "open"}</td></tr>))}</tbody></table>
        </div>
      </div>

      <div className="card">
        <h2 style={{ display: "flex", alignItems: "center", gap: 12 }}>Configuration ({t.configuration.length} positions)
          <button className="x" onClick={() => setShowCfg(!showCfg)}>{showCfg ? "hide" : "show"}</button></h2>
        {showCfg && (
          <table><thead><tr><th>Position</th><th>Part</th><th>Serial</th><th className="num">Hours since overhaul</th></tr></thead>
            <tbody>{t.configuration.map((c) => (
              <tr key={c.position}><td>{c.position}</td><td>{c.description}<br /><code>{c.pn}</code></td><td><code>{c.sn}</code></td>
                <td className="num">{c.tso_h_at_export == null ? "–" : Math.round(c.tso_h_at_export)}</td></tr>))}</tbody></table>)}
      </div>
    </>
  );
}
