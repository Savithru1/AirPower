import { FormEvent, useEffect, useState } from "react";
import { ApiError, api, AppUser, AuditRow, localTime } from "./api";
import { useAuth } from "./auth";

const ROLES: [string, string][] = [["commander", "Commander"], ["engineering_officer", "Engineering officer"],
  ["logistics_officer", "Logistics officer"], ["analyst", "Analyst"], ["admin", "Administrator"]];
const ACTIONS = ["login", "logout", "password_change", "user_create", "user_change", "plan_accept", "plan_override", "whatif_run",
  "report_export", "data_load", "hums_ingest", "access_denied", "login_throttled", "users_seeded"];

export default function Admin({ off }: { off: number }) {
  const { can, user } = useAuth();
  return (
    <>
      {can("manage_users") && <Users me={user?.username ?? ""} off={off} />}
      {can("view_audit") && <Audit off={off} />}
    </>
  );
}

function Users({ me, off }: { me: string; off: number }) {
  const [rows, setRows] = useState<AppUser[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [f, setF] = useState({ username: "", display_name: "", role: "engineering_officer", password: "" });
  const load = () => api.adminUsers().then(setRows).catch((e: ApiError) => setErr(e.detail));
  useEffect(() => { load(); }, []);
  const run = (p: Promise<unknown>, ok: string) => p.then(() => { setNote(ok); setErr(null); load(); }).catch((e: ApiError) => { setErr(e.detail); setNote(null); });
  const create = (e: FormEvent) => { e.preventDefault(); run(api.createUser(f), `created ${f.username}: they must change the password at first sign-in`).then(() => setF({ ...f, username: "", display_name: "", password: "" })); };
  return (
    <div className="card">
      <h2>Users</h2>
      {err && <p className="err" role="alert">{err}</p>}
      {note && <p className="muted">{note}</p>}
      <div style={{ overflowX: "auto" }}>
        <table>
          <thead><tr><th>User</th><th>Role</th><th>Last sign-in</th><th>Status</th><th>Actions</th></tr></thead>
          <tbody>{rows.map((u) => (
            <tr key={u.username}>
              <td><b>{u.username}</b><br /><span className="muted">{u.display_name}</span></td>
              <td><select value={u.role} disabled={u.username === me} aria-label={`role of ${u.username}`}
                          onChange={(e) => run(api.changeUser(u.username, { role: e.target.value }), `${u.username} is now ${e.target.value}`)}>
                {ROLES.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></td>
              <td className="muted">{u.last_login ? localTime(u.last_login, off) : "never"}</td>
              <td>{u.disabled ? <span className="pill s-NMCS">disabled</span> : u.locked_until && new Date(u.locked_until + "Z") > new Date() ? <span className="pill s-NMCM">locked</span> : u.must_change ? <span className="pill s-NMCM">must change password</span> : <span className="pill s-SERVICEABLE">active</span>}</td>
              <td style={{ whiteSpace: "nowrap" }}>
                <button className="x" disabled={u.username === me} onClick={() => run(api.changeUser(u.username, { disabled: !u.disabled }), `${u.username} ${u.disabled ? "enabled" : "disabled"}`)}>{u.disabled ? "Enable" : "Disable"}</button>{" "}
                <button className="x" onClick={() => { const p = window.prompt(`New temporary password for ${u.username} (12 characters or more; they must change it at sign-in)`); if (p) run(api.changeUser(u.username, { password: p }), `password reset for ${u.username}`); }}>Reset password</button>
              </td>
            </tr>))}</tbody>
        </table>
      </div>
      <h2 style={{ marginTop: 18 }}>Add a user</h2>
      <form onSubmit={create} style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "end" }}>
        <label className="lever"><span>User name</span><input value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} required pattern="[a-z0-9][a-z0-9._-]{2,39}" title="3-40 characters: lower-case letters, digits, . _ -" /></label>
        <label className="lever"><span>Name</span><input value={f.display_name} onChange={(e) => setF({ ...f, display_name: e.target.value })} /></label>
        <label className="lever"><span>Role</span><select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>{ROLES.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></label>
        <label className="lever"><span>Temporary password</span><input type="password" autoComplete="new-password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} required minLength={12} /></label>
        <button className="x primary">Add user</button>
      </form>
    </div>
  );
}

function Audit({ off }: { off: number }) {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [total, setTotal] = useState(0);
  const [action, setAction] = useState("");
  const [outcome, setOutcome] = useState("");
  const [verify, setVerify] = useState<{ ok: boolean; rows: number; first_bad_id: number | null; reason: string | null } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { api.audit({ action, outcome }).then((r) => { setRows(r.rows); setTotal(r.total); }).catch((e: ApiError) => setErr(e.detail)); }, [action, outcome]);
  return (
    <div className="card">
      <h2>Audit log</h2>
      <p className="muted" style={{ marginTop: -6 }}>Append-only and hash-chained: every entry carries the hash of the one before it, so a removed or edited entry is detectable. Sign-ins, decisions, forecasts, report exports, data loads and refused requests are recorded; reading the dashboard is not.</p>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginBottom: 8 }}>
        <select value={action} onChange={(e) => setAction(e.target.value)} aria-label="action"><option value="">all actions</option>{ACTIONS.map((a) => <option key={a}>{a}</option>)}</select>
        <select value={outcome} onChange={(e) => setOutcome(e.target.value)} aria-label="outcome"><option value="">any outcome</option><option>ok</option><option>failed</option><option>denied</option></select>
        <span className="muted">{total} entries</span>
        <button className="x" style={{ marginLeft: "auto" }} onClick={() => api.auditVerify().then(setVerify)}>Verify the chain</button>
        {verify && <span className={`pill ${verify.ok ? "s-SERVICEABLE" : "s-NMCS"}`}>{verify.ok ? `chain intact (${verify.rows} entries)` : `broken at entry ${verify.first_bad_id}: ${verify.reason}`}</span>}
      </div>
      {err && <p className="err">{err}</p>}
      <div style={{ maxHeight: 460, overflow: "auto" }}>
        <table>
          <thead><tr><th>Time</th><th>User</th><th>Action</th><th>Target</th><th>Outcome</th><th>Address</th></tr></thead>
          <tbody>{rows.map((r) => (
            <tr key={r.id} title={r.detail ?? ""}>
              <td className="muted" style={{ whiteSpace: "nowrap" }}>{localTime(r.ts, off)}</td>
              <td>{r.actor}<br /><span className="muted">{r.role ?? ""}</span></td><td>{r.action}</td><td className="muted">{r.target ?? ""}</td>
              <td><span className={`pill ${r.outcome === "ok" ? "s-SERVICEABLE" : r.outcome === "denied" ? "s-NMCS" : "s-NMCM"}`}>{r.outcome}</span></td>
              <td className="muted">{r.ip ?? ""}</td></tr>))}</tbody>
        </table>
      </div>
    </div>
  );
}
