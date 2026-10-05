import { useEffect, useMemo, useRef, useState } from "react";
import { api, Forecast, Levers, pct, pts } from "./api";
import { Chart, css } from "./Chart";

const DEFAULT: Levers = { flying: 1, surge: null, lead: 1, capacity: { O: 0, I: 0, D: 0 }, stock: "current", policy: "P0" };
const PRESETS: { name: string; why: string; levers: Partial<Levers> }[] = [
  { name: "Exercise next month", why: "flying task x1.4 for three weeks from day 10", levers: { surge: { start_day: 10, days: 21, factor: 1.4 } } },
  { name: "Repair shop slower", why: "repair turnaround and procurement x1.5", levers: { lead: 1.5 } },
  { name: "Recommended stock", why: "the METRIC allocation of the same budget", levers: { stock: "metric" } },
  { name: "Predictive policy", why: "P2: HUMS predictions drive spares and removal timing", levers: { policy: "P2" } },
  { name: "One more I-level bay", why: "capacity +1 at each intermediate maintenance facility", levers: { capacity: { O: 0, I: 1, D: 0 } } },
];

function Num({ label, value, min, max, step, onChange, fmt }: { label: string; value: number; min: number; max: number; step: number;
  onChange: (v: number) => void; fmt: (v: number) => string }) {
  return (
    <label className="lever"><span>{label} <b>{fmt(value)}</b></span>
      <input type="range" min={min} max={max} step={step} value={value} onChange={(e) => onChange(Number(e.target.value))} />
    </label>
  );
}

export default function WhatIf() {
  const [lv, setLv] = useState<Levers>(DEFAULT);
  const [days, setDays] = useState(90);
  const [reps, setReps] = useState(20);
  const [res, setRes] = useState<Forecast | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const timer = useRef<number | null>(null);
  useEffect(() => () => { if (timer.current) window.clearTimeout(timer.current); }, []);

  const run = (levers: Levers) => {
    setErr(null); setBusy("Simulating futures…"); setRes(null);
    const t0 = Date.now();
    api.whatif(levers, days, reps).then((job) => {
      const poll = () => api.whatifJob(job.id).then((j) => {
        if (j.status === "done" && j.result) { setRes(j.result); setBusy(null); }
        else if (j.status === "failed") { setErr(j.error ?? "failed"); setBusy(null); }
        else { setBusy(`Simulating ${reps} futures of ${days} days… ${Math.round((Date.now() - t0) / 1000)} s`); timer.current = window.setTimeout(poll, 1500); }
      });
      poll();
    }).catch((e) => { setErr(String(e)); setBusy(null); });
  };

  const chart = useMemo(() => {
    if (!res) return {};
    const b = res.baseline.daily, s = res.scenario.daily;
    const x = b.map((d) => d.day.slice(5));
    const band = (side: typeof b, color: string, name: string) => [
      { type: "line", data: side.map((d) => 100 * d.p10), stack: name, lineStyle: { opacity: 0 }, symbol: "none", tooltip: { show: false }, name: `${name} band` },
      { type: "line", data: side.map((d) => 100 * (d.p90 - d.p10)), stack: name, lineStyle: { opacity: 0 }, symbol: "none",
        areaStyle: { color, opacity: 0.15 }, tooltip: { show: false }, name: `${name} band ` },
      { type: "line", data: side.map((d) => 100 * d.mean), name, color, showSymbol: false, lineStyle: { width: 2 } },
    ];
    return {
      tooltip: { trigger: "axis", valueFormatter: (v: unknown) => `${Number(v).toFixed(0)} %` },
      legend: { top: 0, data: ["today's settings", "scenario"], textStyle: { color: css("--ink-2") } },
      xAxis: { type: "category", data: x, axisLabel: { hideOverlap: true } },
      yAxis: { type: "value", min: 40, max: 100, axisLabel: { formatter: "{value} %" } },
      series: [...band(b, css("--ink-3"), "today's settings"), ...band(s, css("--series-1"), "scenario")],
    } as object;
  }, [res]);

  const set = (p: Partial<Levers>) => setLv({ ...lv, ...p });
  const dif = res?.difference;
  return (
    <>
      <div className="card">
        <h2>What if…</h2>
        <p className="muted" style={{ marginTop: -6 }}>The fleet twin starts from today's records (airframe hours, inspections, component ages, stock,
          aircraft already down) and simulates many possible futures. Each scenario runs on the same random futures as today's settings, so the
          difference is measured, not guessed.</p>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {PRESETS.map((p) => (
            <button key={p.name} className="x" title={p.why} onClick={() => { const l = { ...DEFAULT, ...p.levers }; setLv(l); run(l); }}>{p.name}</button>))}
        </div>
      </div>
      <div className="grid g2">
        <div className="card">
          <h2>Forecast: aircraft serviceable at 08:00</h2>
          {busy && <p className="muted">{busy}</p>}
          {err && <p className="err">{err}</p>}
          {res ? <>
            <Chart option={chart} height={320} />
            <p className="muted">Lines: mean of {res.reps} simulated futures from {res.start_date}; shading: 10th–90th percentile.
              {res.open_items > 0 ? ` ${res.open_items} aircraft already down at the start are carried in.` : ""} ({res.seconds} s)</p></>
            : !busy && <p className="muted">Choose a preset or set the levers and run.</p>}
        </div>
        <div className="card">
          <h2>Levers</h2>
          <div style={{ display: "grid", gap: 12 }}>
            <Num label="Flying rate" value={lv.flying} min={0.6} max={1.6} step={0.05} onChange={(v) => set({ flying: v })} fmt={(v) => `x${v.toFixed(2)}`} />
            <label className="lever"><span><input type="checkbox" checked={!!lv.surge} onChange={(e) => set({ surge: e.target.checked ? { start_day: 10, days: 21, factor: 1.4 } : null })} /> Exercise</span></label>
            {lv.surge && <div style={{ display: "grid", gap: 8, paddingLeft: 16 }}>
              <Num label="starts on day" value={lv.surge.start_day} min={0} max={days - 7} step={1} onChange={(v) => set({ surge: { ...lv.surge!, start_day: v } })} fmt={(v) => `${v}`} />
              <Num label="lasting" value={lv.surge.days} min={3} max={42} step={1} onChange={(v) => set({ surge: { ...lv.surge!, days: v } })} fmt={(v) => `${v} days`} />
              <Num label="flying task" value={lv.surge.factor} min={1.1} max={2} step={0.05} onChange={(v) => set({ surge: { ...lv.surge!, factor: v } })} fmt={(v) => `x${v.toFixed(2)}`} />
            </div>}
            <Num label="Repair and procurement times" value={lv.lead} min={0.5} max={2} step={0.05} onChange={(v) => set({ lead: v })} fmt={(v) => `x${v.toFixed(2)}`} />
            {(["O", "I", "D"] as const).map((k) => (
              <Num key={k} label={`${{ O: "Squadron (O)", I: "Base workshop (I)", D: "Depot (D)" }[k]} crews / bays`} value={lv.capacity[k]} min={-2} max={3} step={1}
                   onChange={(v) => set({ capacity: { ...lv.capacity, [k]: v } })} fmt={(v) => (v > 0 ? `+${v}` : `${v}`)} />))}
            <label className="lever"><span>Stock</span>
              <select value={lv.stock} onChange={(e) => set({ stock: e.target.value as Levers["stock"] })}>
                <option value="current">as held today</option><option value="metric">recommended (METRIC, same budget)</option></select></label>
            <label className="lever"><span>Maintenance policy</span>
              <select value={lv.policy} onChange={(e) => set({ policy: e.target.value as Levers["policy"] })}>
                <option value="P0">as today (reactive)</option><option value="P2">predictive (P2)</option></select></label>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="horizon">
                {[30, 60, 90, 180].map((d) => <option key={d} value={d}>{d} days</option>)}</select>
              <select value={reps} onChange={(e) => setReps(Number(e.target.value))} aria-label="replications">
                {[10, 20, 40].map((r) => <option key={r} value={r}>{r} futures</option>)}</select>
              <button className="x primary" disabled={!!busy} onClick={() => run(lv)}>Run</button>
              <button className="x" onClick={() => setLv(DEFAULT)}>Reset</button>
            </div>
          </div>
        </div>
      </div>
      {res && dif && (
        <div className="grid g4">
          <div className="card"><h2>Availability</h2><div className="big">{pct(res.scenario.availability.mean, 1)}</div>
            <p className="muted" style={{ margin: "6px 0 0" }}>today's settings {pct(res.baseline.availability.mean, 1)}</p></div>
          <div className="card"><h2>Difference</h2><div className="big" style={{ color: dif.availability.mean >= 0 ? "var(--ok)" : "var(--nmcs)" }}>{pts(dif.availability)}</div>
            <p className="muted" style={{ margin: "6px 0 0" }}>{(dif.serviceable_aircraft.mean >= 0 ? "+" : "") + dif.serviceable_aircraft.mean.toFixed(1)} aircraft on average (95 % CI)</p></div>
          <div className="card"><h2>Flying task met</h2><div className="big">{pct(res.scenario.task_met.mean, 1)}</div>
            <p className="muted" style={{ margin: "6px 0 0" }}>{pts(dif.task_met)} vs today's settings</p></div>
          <div className="card"><h2>Awaiting spares</h2><div className="big">{pct(res.scenario.nmcs.mean, 1)}</div>
            <p className="muted" style={{ margin: "6px 0 0" }}>of aircraft-time; today's settings {pct(res.baseline.nmcs.mean, 1)}</p></div>
        </div>)}
    </>
  );
}
