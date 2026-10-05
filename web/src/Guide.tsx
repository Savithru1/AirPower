import { useEffect, useState } from "react";

// One short "how to read this" per view, so a first-time user can follow the demo without a briefing.
export const GUIDE: Record<string, { title: string; lines: string[] }> = {
  readiness: { title: "Readiness: is the fleet ready, and what is holding it back?", lines: [
    "Top row: aircraft serviceable now, per unit, split into in maintenance, awaiting spares and scheduled servicing.",
    "The chart shows the last 12 months and the fleet twin's forecast for the coming weeks, with its uncertainty band.",
    "Below: the parts aircraft are waiting for, the components at risk according to the HUMS models, and every aircraft. Click an aircraft for its page.",
    "Change the date (top right) to see the fleet as it was on any morning in the records."] },
  health: { title: "Health map: where are the problems?", lines: [
    "One row per aircraft, one column per system. Green ok, amber plan it, red act now; grey means the system is not fitted to that type.",
    "A cell combines the HUMS model risk with life consumed against the part's limit. Hover for the reason, click for the aircraft page."] },
  tail: { title: "Aircraft page: the digital twin of one aircraft", lines: [
    "Recommended actions come first, each with why it was raised and which model or record raised it.",
    "Remaining life is shown with its 90 % interval: plan on the left end of the bar, not on the dot.",
    "The report button opens a printable version (save as PDF from the print dialog)."] },
  plan: { title: "Planner: when should each aircraft go in?", lines: [
    "An optimiser (CP-SAT) places every inspection and predicted removal in the next 90 days, against crew and bay capacity and the flying task.",
    "Compare with the due-date rule (each job on its last allowed day). Bundling a removal into an inspection saves a separate induction.",
    "Accept or move a job: the decision is recorded with your name and time, and the next plan keeps the job on that date."] },
  spares: { title: "Spares: what to stock and what is at risk", lines: [
    "The curve shows fleet availability for each level of spares money, with the best allocation (METRIC).",
    "The table lists parts to buy or reduce, and aircraft likely to wait for a part within its lead time."] },
  whatif: { title: "What if: test a decision before taking it", lines: [
    "Pick a preset or set the levers: flying rate, an exercise, repair times, capacity, stock, maintenance policy.",
    "The twin runs many simulated futures from today's records, with and without the change, on the same random futures.",
    "Read the difference with its 95 % interval: if the interval crosses zero, the change does not reliably matter."] },
  evidence: { title: "Evidence: does it work?", lines: [
    "The maintenance policies are compared in a simulated fleet over many paired years, with the platform's own models in the loop.",
    "The value-of-prediction chart degrades the models on purpose: the gain that survives tells how good the predictions must be.",
    "The last table checks the twin: forecasts made from the records against what the simulated fleet actually did next."] },
  reliability: { title: "Reliability and records", lines: [
    "Per part: removals, mean time between unscheduled removals and failures, no-fault-found rate, and the failure pattern (wear-out or random).",
    "Repeat defects and suspected rogue serials point to root causes; the search covers every defect report."] },
  admin: { title: "Administration: who can do what", lines: [
    "Five roles: commander, engineering officer, logistics officer, analyst and administrator. The administrator manages users and reads the audit log; it does not decide maintenance plans.",
    "New users get a temporary password and must choose their own at first sign-in. After five wrong passwords an account is locked for five minutes.",
    "The audit log cannot be edited; verifying the chain proves nothing was removed or changed."] },
  integration: { title: "Data integration: can the numbers be trusted?", lines: [
    "Records from four separate systems (technical records, HUMS, stores, maintenance agencies) are matched to the same aircraft, parts and serials.",
    "Every inconsistency found is listed with what was done about it: fixed, linked, merged, flagged or dropped."] },
};

export function Guide({ view }: { view: string }) {
  const g = GUIDE[view];
  const keyName = `airpower.guide.${view}`;
  const [open, setOpen] = useState(false);
  useEffect(() => {
    let seen = true;
    try { seen = localStorage.getItem(keyName) === "1"; } catch { /* storage blocked */ }
    setOpen(!seen);
  }, [keyName]);
  const close = () => { setOpen(false); try { localStorage.setItem(keyName, "1"); } catch { /* storage blocked */ } };
  if (!g) return null;
  if (!open) return <button className="guide-btn" onClick={() => setOpen(true)} aria-label="How to read this view">? How to read this view</button>;
  return (
    <div className="card guide" role="note">
      <div style={{ display: "flex", gap: 12, alignItems: "baseline" }}>
        <b>{g.title}</b>
        <button className="x" style={{ marginLeft: "auto" }} onClick={close}>Got it</button>
      </div>
      <ul>{g.lines.map((l, i) => <li key={i}>{l}</li>)}</ul>
    </div>
  );
}
