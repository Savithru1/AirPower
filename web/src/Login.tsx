import { FormEvent, useState } from "react";
import { api, ApiError, Me } from "./api";

export function Banner({ text, warn }: { text: string; warn?: boolean }) {
  return <div className={`classification${warn ? " warn" : ""}`} role="note">{text}</div>;
}

export function SignIn({ classification, onDone }: { classification: string; onDone: (m: Me) => void }) {
  const [user, setUser] = useState("");
  const [pw, setPw] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(null);
    api.login(user, pw).then(onDone).catch((x: ApiError) => { setErr(x.detail); setPw(""); }).finally(() => setBusy(false));
  };
  return (
    <>
      <Banner text={classification} />
      <main style={{ maxWidth: 420, marginTop: 60 }}>
        <div className="card">
          <h3 style={{ marginBottom: 4 }}>AirPower</h3>
          <p className="muted" style={{ marginTop: 0 }}>Fleet readiness &amp; predictive maintenance. Authorised users only; sign-ins and decisions are recorded.</p>
          <form onSubmit={submit} style={{ display: "grid", gap: 12 }}>
            <label className="lever"><span>User name</span>
              <input autoFocus autoComplete="username" value={user} onChange={(e) => setUser(e.target.value)} required /></label>
            <label className="lever"><span>Password</span>
              <input type="password" autoComplete="current-password" value={pw} onChange={(e) => setPw(e.target.value)} required /></label>
            {err && <p className="err" role="alert" style={{ margin: 0 }}>{err}</p>}
            <button className="x primary" disabled={busy || !user || !pw}>{busy ? "Signing in…" : "Sign in"}</button>
          </form>
        </div>
      </main>
    </>
  );
}

export function ChangePassword({ forced, onDone, onCancel }: { forced: boolean; onDone: () => void; onCancel?: () => void }) {
  const [cur, setCur] = useState("");
  const [n1, setN1] = useState("");
  const [n2, setN2] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (n1 !== n2) { setErr("the two new passwords differ"); return; }
    api.changePassword(cur, n1).then(onDone).catch((x: ApiError) => setErr(x.detail));
  };
  return (
    <div className="card" style={{ maxWidth: 420 }}>
      <h2>{forced ? "Choose a new password to continue" : "Change password"}</h2>
      <form onSubmit={submit} style={{ display: "grid", gap: 12 }}>
        <label className="lever"><span>Current password</span><input type="password" autoComplete="current-password" value={cur} onChange={(e) => setCur(e.target.value)} required /></label>
        <label className="lever"><span>New password (12 characters or more)</span><input type="password" autoComplete="new-password" value={n1} onChange={(e) => setN1(e.target.value)} required /></label>
        <label className="lever"><span>Repeat new password</span><input type="password" autoComplete="new-password" value={n2} onChange={(e) => setN2(e.target.value)} required /></label>
        {err && <p className="err" role="alert" style={{ margin: 0 }}>{err}</p>}
        <div style={{ display: "flex", gap: 8 }}>
          <button className="x primary">Change password</button>
          {onCancel && <button type="button" className="x" onClick={onCancel}>Cancel</button>}
        </div>
      </form>
    </div>
  );
}
