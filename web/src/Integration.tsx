import { useEffect, useMemo, useState } from "react";
import { api, DQ, DQIssue } from "./api";
import { Chart, css } from "./Chart";

const SOURCES: Record<string, string> = {
  records: "Technical records", stores: "Stores / inventory", agencies: "Maintenance agencies", hums: "HUMS ground station",
};
const RULE: Record<string, string> = {
  duplicate_record: "Defect entered twice",
  duplicate_upload: "HUMS file uploaded twice",
  late_entry: "Defect entered > 24 h after the event",
  missing_ata: "Defect without ATA chapter",
  missing_defect_link: "Work order lost its defect link",
  missing_aircraft: "Stores demand without aircraft",
  missing_hums_download: "Sortie flown, no HUMS download",
  part_number_format: "Part number in a non-standard format",
  serial_number_error: "Serial number typo",
  superseded_part_number: "Part number superseded",
  stock_xref_missing: "Stock number without P/N cross-reference",
  life_card_mismatch: "Life card disagrees with change history",
  unknown_serial: "Serial number not found anywhere",
  unknown_part_number: "Part number not recognised",
  unknown_work_order: "Defect cites an unknown work order",
  no_matching_sortie: "HUMS download matches no sortie",
  unknown_hums_unit: "Unknown HUMS unit",
};
const CONV: Record<string, string> = {
  "records.tail": "Tail numbers normalised (records: J-1001)",
  "agencies.tail": "Tail numbers normalised (agencies: J 1001 / j1001)",
  "stores.tail": "Tail numbers resolved from number only (stores: 1001)",
  "records.superseded_pn": "Superseded part numbers mapped to the current number",
};

export default function Integration() {
  const [dq, setDq] = useState<DQ | null>(null);
  const [rule, setRule] = useState<string | null>(null);
  const [rows, setRows] = useState<DQIssue[]>([]);
  useEffect(() => { api.dq().then(setDq); }, []);
  useEffect(() => { if (rule) api.dqIssues(rule, 40).then(setRows); }, [rule]);

  const byRule = useMemo(() => {
    const m = new Map<string, { source: string; rule: string; severity: string; done: number; flagged: number }>();
    for (const i of dq?.issues ?? []) {
      const k = `${i.source}|${i.rule}`;
      const e = m.get(k) ?? { source: i.source, rule: i.rule, severity: i.severity, done: 0, flagged: 0 };
      if (i.resolution === "flagged" || i.resolution === "dropped") e.flagged += i.n; else e.done += i.n;
      m.set(k, e);
    }
    return [...m.values()].sort((a, b) => b.done + b.flagged - (a.done + a.flagged));
  }, [dq]);

  const chart = useMemo(() => ({
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    legend: { top: 0, textStyle: { color: css("--ink-2") } },
    grid: { left: 250, right: 24, top: 28, bottom: 24 },
    xAxis: { type: "value" },
    yAxis: { type: "category", data: byRule.map((r) => RULE[r.rule] ?? r.rule).reverse(), axisLabel: { color: css("--ink-2"), width: 240, overflow: "truncate" } },
    series: [
      { name: "Fixed, linked or merged", type: "bar", stack: "a", color: css("--ok"), data: byRule.map((r) => r.done).reverse() },
      { name: "Flagged for review", type: "bar", stack: "a", color: css("--nmcm"), data: byRule.map((r) => r.flagged).reverse() },
    ],
  }), [byRule]);

  if (!dq) return <p className="muted">Loading data-quality report…</p>;
  const done = byRule.reduce((a, r) => a + r.done, 0), flagged = byRule.reduce((a, r) => a + r.flagged, 0);
  return (
    <>
      <div className="grid g4">
        {Object.entries(dq.records_in).map(([src, counts]) => (
          <div className="card" key={src}>
            <h2>{SOURCES[src] ?? src}</h2>
            <table><tbody>{Object.entries(counts).map(([k, v]) => (
              <tr key={k}><td>{k.replace(/_/g, " ")}</td><td className="num">{v.toLocaleString()}</td></tr>))}</tbody></table>
          </div>
        ))}
      </div>
      <div className="grid g3">
        <div className="card"><h2>Inconsistencies resolved</h2><div className="big">{done.toLocaleString()}</div>
          <p className="muted" style={{ margin: "6px 0 0" }}>fixed, linked across systems or merged</p></div>
        <div className="card"><h2>Flagged for review</h2><div className="big">{flagged.toLocaleString()}</div>
          <p className="muted" style={{ margin: "6px 0 0" }}>reported with evidence, not guessed</p></div>
        <div className="card"><h2>Convention conversions</h2>
          <table><tbody>{Object.entries(dq.convention_conversions).map(([k, v]) => (
            <tr key={k}><td>{CONV[k] ?? k}</td><td className="num">{v.toLocaleString()}</td></tr>))}</tbody></table></div>
      </div>
      <div className="card">
        <h2>What the integration layer found</h2>
        <Chart option={chart} height={Math.max(220, 34 * byRule.length + 60)} />
      </div>
      <div className="grid g2">
        <div className="card">
          <h2>By rule (select one to see records)</h2>
          <table>
            <thead><tr><th>Source</th><th>Finding</th><th className="num">Resolved</th><th className="num">Flagged</th></tr></thead>
            <tbody>{byRule.map((r) => (
              <tr key={r.source + r.rule} className="click" onClick={() => setRule(r.rule)} aria-selected={rule === r.rule}>
                <td>{SOURCES[r.source] ?? r.source}</td><td>{RULE[r.rule] ?? r.rule}</td>
                <td className="num">{r.done}</td><td className="num">{r.flagged}</td>
              </tr>))}</tbody>
          </table>
        </div>
        <div className="card">
          <h2>{rule ? RULE[rule] ?? rule : "Sample records"}</h2>
          {!rule ? <p className="muted">Select a rule.</p> : (
            <table>
              <thead><tr><th>Record</th><th>Raw → resolved</th><th>Outcome</th></tr></thead>
              <tbody>{rows.map((i) => (
                <tr key={i.id}>
                  <td><code>{i.record_key}</code>{i.field && <><br /><span className="muted">{i.field}</span></>}</td>
                  <td>{i.raw_value ?? <span className="muted">(empty)</span>} → {i.resolved_value ?? <span className="muted">–</span>}
                    {i.detail && <><br /><span className="muted">{i.detail}</span></>}</td>
                  <td><span className={`pill r-${i.resolution}`}>{i.resolution}</span></td>
                </tr>))}</tbody>
            </table>
          )}
        </div>
      </div>
    </>
  );
}
