import { useEffect, useMemo, useState } from "react";
import { api, DailyRow, Forecast, localTime, Meta, pct, Readiness as R, Risk, Status } from "./api";
import { RiskTable } from "./RiskTable";
import { Chart, css } from "./Chart";

const LABEL: Record<Status, string> = {
  SERVICEABLE: "Serviceable", NMCM: "In maintenance", NMCS: "Awaiting spares", SCHEDULED: "Scheduled servicing",
};
const ORDER: Status[] = ["SERVICEABLE", "NMCM", "NMCS", "SCHEDULED"];
// consequence order for the risk list: an engine or gearbox removal grounds an aircraft for days, a battery for an hour
const RANK: Record<Risk["kind"], number> = { engine_rul: 0, gearbox_anomaly: 1, battery_rul: 2 };
const VAR: Record<Status, string> = { SERVICEABLE: "--ok", NMCM: "--nmcm", NMCS: "--nmcs", SCHEDULED: "--sched" };

export function Legend() {
  return (
    <div className="legend">
      {ORDER.map((s) => <span key={s}><i className="dot" style={{ background: `var(${VAR[s]})` }} />{LABEL[s]}</span>)}
    </div>
  );
}

export default function Readiness({ meta, at, open }: { meta: Meta; at: string; open: (tail: string) => void }) {
  const [r, setR] = useState<R | null>(null);
  const [daily, setDaily] = useState<DailyRow[]>([]);
  const [risk, setRisk] = useState<Risk[]>([]);
  const [fc, setFc] = useState<Forecast | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { api.readiness(at).then(setR).catch((e) => setErr(String(e))); }, [at]);
  useEffect(() => { api.risk(at).then((x) => setRisk(x.rows)).catch(() => setRisk([])); }, [at]);
  useEffect(() => { api.availability(365).then(setDaily).catch((e) => setErr(String(e))); }, []);
  useEffect(() => { api.forecast().then((x) => setFc("baseline" in x ? x : null)).catch(() => setFc(null)); }, []);
  const off = meta.utc_offset_h;
  const typeName = Object.fromEntries(meta.units.map((u) => [u.id, u.type_name]));

  const trend = useMemo(() => {
    const units = [...new Set(daily.map((d) => d.unit))];
    const days = [...new Set(daily.map((d) => d.day))].sort();
    const palette = [css("--series-1"), css("--series-2"), css("--series-3")];
    return {
      tooltip: { trigger: "axis", valueFormatter: (v: unknown) => `${Number(v).toFixed(0)} %` },
      legend: { top: 0, textStyle: { color: css("--ink-2") } },
      xAxis: { type: "category", data: days.map((d) => localTime(d, 0, false)), axisLabel: { color: css("--ink-2"), hideOverlap: true } },
      yAxis: { type: "value", min: 0, max: 100, axisLabel: { formatter: "{value} %", color: css("--ink-2") } },
      series: units.map((u, i) => {
        const byDay = new Map(daily.filter((d) => d.unit === u).map((d) => [d.day, (100 * d.serviceable) / d.aircraft]));
        return { name: `${u}`, type: "line", showSymbol: false, smooth: 0.3, lineStyle: { width: 1.6 }, color: palette[i % 3],
                 data: days.map((d) => byDay.get(d) ?? null) };
      }),
    } as object;
  }, [daily]);

  const fchart = useMemo(() => {
    if (!fc) return {};
    const b = fc.baseline.daily;
    return {
      tooltip: { trigger: "axis", valueFormatter: (v: unknown) => `${Number(v).toFixed(0)} %` },
      xAxis: { type: "category", data: b.map((d) => d.day.slice(5)), axisLabel: { hideOverlap: true } },
      yAxis: { type: "value", min: 40, max: 100, axisLabel: { formatter: "{value} %" } },
      series: [
        { type: "line", data: b.map((d) => 100 * d.p10), stack: "b", lineStyle: { opacity: 0 }, symbol: "none", tooltip: { show: false } },
        { type: "line", data: b.map((d) => 100 * (d.p90 - d.p10)), stack: "b", lineStyle: { opacity: 0 }, symbol: "none",
          areaStyle: { color: css("--series-1"), opacity: 0.15 }, tooltip: { show: false } },
        { type: "line", name: "expected", data: b.map((d) => 100 * d.mean), color: css("--series-1"), showSymbol: false, lineStyle: { width: 2 } },
      ],
    } as object;
  }, [fc]);

  if (err) return <p className="err">{err}</p>;
  if (!r) return <p className="muted">Loading readiness…</p>;
  const fleet = r.units.reduce((a, u) => ({ ac: a.ac + u.aircraft, sv: a.sv + u.serviceable }), { ac: 0, sv: 0 });

  return (
    <>
      <div className="grid g4">
        <div className="card">
          <h2>Fleet</h2>
          <div className="big">{fleet.sv}<small> / {fleet.ac} serviceable</small></div>
          <p className="muted" style={{ margin: "8px 0 0" }}>{Math.round((100 * fleet.sv) / fleet.ac)} % at {localTime(r.at, off)}</p>
        </div>
        {r.units.map((u) => (
          <div className="card" key={u.unit}>
            <h2>{u.unit} · {typeName[u.unit] ?? u.type} · {u.base}</h2>
            <div className="big">{u.serviceable}<small> / {u.aircraft}</small></div>
            <div className="bar" role="img" aria-label={`${u.serviceable} serviceable, ${u.nmcm} in maintenance, ${u.nmcs} awaiting spares, ${u.scheduled} scheduled`}>
              {ORDER.map((s) => {
                const n = { SERVICEABLE: u.serviceable, NMCM: u.nmcm, NMCS: u.nmcs, SCHEDULED: u.scheduled }[s];
                return <span key={s} style={{ width: `${(100 * n) / u.aircraft}%`, background: `var(${VAR[s]})` }} />;
              })}
            </div>
            <div className="legend">
              <span>{u.nmcm} maint.</span><span>{u.nmcs} spares</span><span>{u.scheduled} sched.</span>
            </div>
          </div>
        ))}
      </div>

      <div className="grid g2">
        <div className="card">
          <h2>Serviceable at 08:00, last 12 months</h2>
          <Chart option={trend} height={290} />
        </div>
        <div className="card">
          <h2>Forecast: next {fc?.days ?? 30} days (fleet twin)</h2>
          {fc ? (<>
            <Chart option={fchart} height={220} />
            <p className="muted" style={{ margin: 0 }}>Expected availability {pct(fc.baseline.availability.mean, 1)}, flying task met {pct(fc.baseline.task_met.mean, 1)}.
              Line: mean of {fc.reps} simulated futures from the records' end; shading: 10th–90th percentile. Try changes in What if.</p></>)
            : <p className="muted">Forecast not built (python -m airpower.twin.whatif forecast).</p>}
        </div>
      </div>

      <div className="grid g2">
        <div className="card">
          <h2>Aircraft waiting for parts</h2>
          {r.nmcs_drivers.length === 0 ? <p className="muted">No aircraft are waiting for spares.</p> : (
            <table>
              <thead><tr><th>Part</th><th className="num">Aircraft</th></tr></thead>
              <tbody>
                {r.nmcs_drivers.map((d) => (
                  <tr key={d.pn}>
                    <td>{d.description}<br /><code>{d.pn}</code></td>
                    <td className="num">{d.aircraft.length}<br /><span className="muted">{d.aircraft.join(", ")}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        <div className="card">
          <h2>Components at risk (HUMS)</h2>
          <p className="muted" style={{ marginTop: -6 }}>{risk.filter((x) => x.status === "alert").length} alert, {risk.filter((x) => x.status === "watch").length} watch of {risk.length} monitored components</p>
          <RiskTable rows={risk.filter((x) => x.status !== "ok")
            .sort((a, b) => RANK[a.kind] - RANK[b.kind] || (b.p_fail_100fh ?? 0) - (a.p_fail_100fh ?? 0)).slice(0, 12)} open={open} />
        </div>
      </div>

      <div className="card">
        <h2>Aircraft</h2>
        <div style={{ marginBottom: 12 }}><Legend /></div>
        {meta.units.map((u) => (
          <div key={u.id} style={{ marginBottom: 14 }}>
            <h3 style={{ fontSize: 14, margin: "0 0 6px" }}>{u.id} <span className="muted">· {u.type_name}</span></h3>
            <div className="board">
              {r.aircraft.filter((a) => a.unit_id === u.id).map((a) => (
                <button key={a.tail} className={`tail s-${a.status}`} onClick={() => open(a.tail)}
                        title={`${LABEL[a.status]}${a.reason ? ": " + a.reason : ""}`} aria-label={`${a.tail}, ${LABEL[a.status]}`}>
                  <b>{a.tail}</b><span>{LABEL[a.status]}</span>
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
    </>
  );
}
