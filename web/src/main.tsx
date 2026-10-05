import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { api, localTime, Me, Meta, onUnauthorized, Permission } from "./api";
import { AuthContext } from "./auth";
import Readiness from "./Readiness";
import Integration from "./Integration";
import ReliabilityView from "./ReliabilityView";
import SparesView from "./SparesView";
import PlanView from "./PlanView";
import HealthMap from "./HealthMap";
import TailPage from "./TailPage";
import WhatIf from "./WhatIf";
import Evidence from "./Evidence";
import Admin from "./Admin";
import { Guide } from "./Guide";
import { Banner, ChangePassword, SignIn } from "./Login";
import "./styles.css";

type Tab = "readiness" | "health" | "plan" | "spares" | "whatif" | "evidence" | "reliability" | "integration" | "admin";
// tab, label, a permission of which the user needs at least one
const TABS: [Tab, string, Permission[]][] = [
  ["readiness", "Readiness", ["view"]], ["health", "Health map", ["view"]], ["plan", "Planner", ["view"]], ["spares", "Spares", ["view"]],
  ["whatif", "What if", ["run_whatif"]], ["evidence", "Evidence", ["view"]], ["reliability", "Reliability", ["view"]],
  ["integration", "Data integration", ["view_dq"]], ["admin", "Administration", ["manage_users", "view_audit"]],
];
const DATED: Tab[] = ["readiness", "health"];

// location.hash is "#<tab>" or "#aircraft/<tail>"
function parse(h: string): { tab: Tab; tail: string | null } {
  const m = h.match(/^#aircraft\/([A-Za-z0-9-]+)/);
  if (m) return { tab: "readiness", tail: m[1].toUpperCase() };
  return { tab: (TABS.find(([t]) => "#" + t === h)?.[0] ?? "readiness") as Tab, tail: null };
}

function Shell({ me, setMe }: { me: Me; setMe: (m: Me | null) => void }) {
  const user = me.user!;
  const can = (p: Permission) => user.permissions.includes(p);
  const allowed = TABS.filter(([, , need]) => need.some(can));
  const [meta, setMeta] = useState<Meta | null>(null);
  const init = parse(location.hash);
  const [tab, setTab] = useState<Tab>(init.tab);
  const [tail, setTail] = useState<string | null>(init.tail);
  const [day, setDay] = useState<string>("");
  const [err, setErr] = useState<string | null>(null);
  const [pw, setPw] = useState(false);
  useEffect(() => {
    if (user.must_change) return;
    api.meta().then((m) => { setMeta(m); setDay(m.default_at.slice(0, 10)); }).catch((e) => setErr(String(e.message ?? e)));
  }, [user.must_change]);
  // replaceState does not fire hashchange, so the app's own URL updates cannot race a click
  useEffect(() => {
    const h = tail ? `#aircraft/${tail}` : "#" + tab;
    if (location.hash !== h) history.replaceState(null, "", h);
  }, [tab, tail]);
  useEffect(() => {
    const on = () => { const p = parse(location.hash); setTail(p.tail); if (!p.tail) setTab(p.tab); };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  useEffect(() => { window.scrollTo(0, 0); }, [tab, tail]);
  // a link to a view this role may not open lands on the first one it may
  useEffect(() => { if (!allowed.some(([t]) => t === tab)) setTab(allowed[0][0]); }, [tab, allowed.length]);
  const signOut = () => api.logout().finally(() => { setMe({ ...me, user: null }); });

  const head = (
    <>
      <Banner text={me.classification} />
      {!me.auth_enabled && <Banner text="AUTHENTICATION IS OFF: development mode, loopback only" warn />}
    </>
  );
  if (user.must_change) {
    return <>{head}<main style={{ maxWidth: 460, marginTop: 40 }}><ChangePassword forced onDone={() => setMe({ ...me, user: { ...user, must_change: false } })} /></main></>;
  }
  if (err) return <>{head}<main><p className="err">Cannot reach the AirPower API: {err}</p></main></>;
  if (!meta) return <>{head}<main><p className="muted">Loading…</p></main></>;
  // readiness is reported at 08:00 base local time of the chosen day
  const at = new Date(Date.parse(day + "T08:00:00Z") - meta.utc_offset_h * 3600e3).toISOString().slice(0, 19);
  const open = (t: string) => setTail(t);
  const go = (t: Tab) => { setTail(null); setTab(t); };
  const view = tail ? "tail" : tab;
  return (
    <AuthContext.Provider value={me}>
      {head}
      <header className="top">
        <div className="brand">AirPower<small>fleet readiness & predictive maintenance</small></div>
        <nav className="tabs" role="tablist">
          {allowed.map(([t, label]) => <button key={t} role="tab" aria-selected={!tail && tab === t} onClick={() => go(t)}>{label}</button>)}
        </nav>
        {(tail || DATED.includes(tab)) && (
          <label className="asof">As of 08:00 on
            <input type="date" value={day} min={meta.window[0].slice(0, 10)} max={meta.default_at.slice(0, 10)}
                   onChange={(e) => e.target.value && setDay(e.target.value)} />
          </label>
        )}
        {!tail && tab === "integration" && <span className="asof">Loaded {localTime(meta.loaded_utc, meta.utc_offset_h)} · run {meta.run}</span>}
        {!tail && tab === "readiness" && can("export_reports") && <a className="x report" href={`/report/fleet?at=${encodeURIComponent(at)}`} target="_blank" rel="noreferrer">Fleet report</a>}
        <div className="who">
          <span title={`${user.username}`}><b>{user.display_name}</b> · {user.role_label}</span>
          {me.auth_enabled && <><button className="x" onClick={() => setPw(!pw)}>Password</button><button className="x" onClick={signOut}>Sign out</button></>}
        </div>
      </header>
      <main>
        {pw && <ChangePassword forced={false} onDone={() => setPw(false)} onCancel={() => setPw(false)} />}
        <Guide key={view} view={view} />
        {tail ? <TailPage tail={tail} at={at} meta={meta} back={() => setTail(null)} />
          : tab === "readiness" ? <Readiness meta={meta} at={at} open={open} />
          : tab === "health" ? <HealthMap meta={meta} at={at} open={open} />
          : tab === "plan" ? <PlanView meta={meta} open={open} />
          : tab === "spares" ? <SparesView />
          : tab === "whatif" ? <WhatIf />
          : tab === "evidence" ? <Evidence />
          : tab === "reliability" ? <ReliabilityView meta={meta} />
          : tab === "admin" ? <Admin off={meta.utc_offset_h} />
          : <Integration />}
      </main>
    </AuthContext.Provider>
  );
}

function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.me().then(setMe).catch((e) => setErr(String(e.message ?? e)));
    onUnauthorized.handler = () => setMe((m) => (m ? { ...m, user: null } : m));
  }, []);
  if (err) return <main><p className="err">Cannot reach the AirPower API: {err}</p></main>;
  if (!me) return <main><p className="muted">Loading…</p></main>;
  if (!me.user) return <SignIn classification={me.classification} onDone={setMe} />;
  return <Shell key={me.user.username} me={me} setMe={setMe} />;
}

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
