import { Risk } from "./api";

const KIND: Record<Risk["kind"], string> = {
  engine_rul: "Engine trend", gearbox_anomaly: "Gearbox vibration", battery_rul: "Battery capacity",
};
const STATUS_CLASS: Record<Risk["status"], string> = { ok: "s-SERVICEABLE", watch: "s-NMCM", alert: "s-NMCS" };

function why(r: Risk): string {
  const e = r.explanation ?? {};
  if (r.kind === "gearbox_anomaly") return `${e.top_feature} ${e.sigma_above_baseline}σ vs own baseline (limit ${e.threshold_sigma}σ)`;
  if (r.kind === "battery_rul") return `state of health ${e.soh}, fading ${e.fade_per_100fh} per 100 FH`;
  const d = (e.drivers as { feature: string; effect_cycles: number }[] | undefined)?.[0];
  return d ? `${e.age_from_records ? "age from records; " : "age unknown; "}main driver ${d.feature}` : e.age_from_records ? "age from records" : "age unknown";
}

export function RiskTable({ rows, showTail = true, open }: { rows: Risk[]; showTail?: boolean; open?: (tail: string) => void }) {
  if (!rows.length) return <p className="muted">No HUMS risk records for this time (run fleet_risk build).</p>;
  return (
    <table>
      <thead><tr>{showTail && <th>Aircraft</th>}<th>Component</th><th className="num">Remaining FH</th>
        <th className="num">P(removal ≤ 100 FH)</th><th>Status</th></tr></thead>
      <tbody>{rows.map((r) => (
        <tr key={`${r.tail}${r.position}${r.kind}`} className={open ? "click" : undefined} onClick={open ? () => open(r.tail) : undefined}>
          {showTail && <td><b>{r.tail}</b></td>}
          <td>{r.position} · {KIND[r.kind]}<br /><span className="muted">{why(r)}</span></td>
          <td className="num">{r.rul_fh == null ? "–" : `${Math.round(r.rul_fh)}`}
            {r.rul_fh_lo != null && r.rul_fh_hi != null && <><br /><span className="muted">{Math.round(r.rul_fh_lo)}–{Math.round(r.rul_fh_hi)}</span></>}</td>
          <td className="num">{r.p_fail_100fh == null ? "–" : `${Math.round(100 * r.p_fail_100fh)} %`}</td>
          <td><span className={`pill ${STATUS_CLASS[r.status]}`}>{r.status}</span></td>
        </tr>))}</tbody>
    </table>
  );
}
