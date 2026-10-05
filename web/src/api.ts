// Typed access to the AirPower API. All times from the API are UTC; the UI shows base local time.

export type Status = "SERVICEABLE" | "NMCM" | "NMCS" | "SCHEDULED";

export interface UnitReadiness {
  unit: string; type: string; base: string; aircraft: number;
  serviceable: number; nmcm: number; nmcs: number; scheduled: number;
}
export interface AircraftState { tail: string; unit_id: string; type_id: string; status: Status; reason: string | null; since: string }
export interface Readiness {
  at: string; units: UnitReadiness[]; aircraft: AircraftState[];
  nmcs_drivers: { pn: string; description: string; aircraft: string[] }[];
}
export interface DailyRow { day: string; unit: string; serviceable: number; nmcm: number; nmcs: number; scheduled: number; aircraft: number }
export interface Meta {
  run: string; loaded_utc: string; utc_offset_h: number; window: [string, string]; default_at: string;
  units: { id: string; type_id: string; base: string; type_name: string }[];
}
export interface DQ {
  records_in: Record<string, Record<string, number>>;
  convention_conversions: Record<string, number>;
  tables: Record<string, number>;
  issues: { source: string; rule: string; severity: string; resolution: string; n: number }[];
  seconds: number;
}
export interface DQIssue {
  id: number; source: string; entity: string; record_key: string; rule: string; severity: string;
  field: string | null; raw_value: string | null; resolved_value: string | null; resolution: string; detail: string | null;
}
export interface AircraftDetail {
  aircraft: { tail: string; msn: string; type_id: string; unit_id: string; base: string; hums_unit: string | null };
  at: string;
  timeline_90d: { start: string; end: string; status: Status; reason: string | null }[];
  open_defects: { defect_no: string; raised: string; category: string; ata: number | null; ata_source: string | null; text: string; wo_id: string | null }[];
  configuration: { position: string; pn: string; description: string; sn: string; installed: string | null }[];
  last_30d: { sorties: number; fh: number };
  hums_channels: { position: string; channel: string }[];
}

export interface Risk {
  tail: string; position: string; sn: string | null; pn: string | null; kind: "engine_rul" | "gearbox_anomaly" | "battery_rul";
  as_of: string; age_fh: number | null; rul_fh: number | null; rul_fh_lo: number | null; rul_fh_hi: number | null;
  p_fail_100fh: number | null; health_index: number | null; status: "ok" | "watch" | "alert";
  explanation: Record<string, unknown> | null; model: string;
}

export interface PartRel {
  pn: string; description: string; ata: number; category: string; installed: number; component_hours: number;
  unscheduled: number; confirmed_failures: number; findings: number; nff: number;
  mtbur_h: number | null; mtbf_h: number | null; nff_rate: number | null;
  weibull_beta: number | null; weibull_eta_h: number | null; weibull_beta_lo: number | null; weibull_beta_hi: number | null;
  weibull_failures: number | null; weibull_beta_landings: number | null; weibull_eta_landings: number | null;
}
export interface Reliability {
  kpis: { fleet_flight_hours: number; defects: number; mfhbd_h: number; unscheduled_removals: number; mfhbur_h: number;
          nff_share: number; mttr_unscheduled_h_median: number; logistics_delay_days_mean: number | null;
          demands_waiting_over_7_days: number | null } | null;
  parts: PartRel[];
  rogues: { pn: string; sn: string; removals: number; component_hours: number; expected: number; p_value: number; nff: number; tails: string }[];
  repeats: { chain_id: number; tail: string; ata: number; occurrences: number; first: string; last: string; text: string; defects: string }[];
}
export interface DefectRow { defect_no: string; tail: string; raised: string; closed: string | null; category: string; ata: number | null;
  ata_source: string | null; text: string; rectification: string | null }

export interface Spares {
  metric: { baseline_cost_lakh: number; baseline_availability: number; baseline_by_type: Record<string, number>;
            metric_cost_lakh: number; metric_availability: number; metric_by_type: Record<string, number>;
            metric_cost_to_match_baseline_lakh: number | null; curve: { cost_lakh: number; availability: number }[] } | null;
  targets: { pn: string; description: string; category: string; cost_lakh: number; demand_per_day: number; pipeline_days: number;
             pipeline_mean: number; baseline_stock: number; metric_stock: number; held_now: number; action: string; qty: number }[];
  aog: { tail: string; position: string; pn: string; sn: string; p_within_lead: number; lead_days: number; basis: string;
         base_stock: number; fleet_stock: number; expected_demand_in_lead: number }[];
  validation: { reps: number; arms: Record<string, { stock_value_lakh: number; availability: number; ci95: number; nmcs: number;
                 paired_diff_vs_baseline?: { mean: number; ci95: number } }> } | null;
  forecast_backtest: { horizon_days: number; origins: number; total_actual: number;
                       all: Record<string, { wape: number; bias: number }>; rotables: Record<string, { wape: number; bias: number }>;
                       high_value_ge_10_lakh: Record<string, { wape: number; bias: number }>; predictive_interval90_coverage: number } | null;
}
export interface PlanJob { tail: string; unit: string; job: string; kind: "inspection" | "condition" | "blocked"; agency: string | null;
  start_day: number | null; end_day: number | null; due_day: number | null; bundled_into: string | null; reason: string | null }
interface PlanStats { shortfall_aircraft_days: number; down_aircraft_days: number; inductions: number; condition_jobs: number;
  bundled: number; condition_late_days: number }
export interface Plan {
  summary: { t0: string; horizon_days: number; aircraft: number; jobs: number; inspections: number; condition_jobs: number;
             blocked_aircraft: number; requirement_per_flying_day: Record<string, number>;
             solver: { status: string; seconds: number; variables?: number; constraints?: number };
             due_date_plan: PlanStats; optimised_plan: PlanStats | null } | null;
  daily: { unit: string; day: number; available: number; required: number; shortfall: number }[];
  jobs: PlanJob[];
}

// ---------------------------------------------------------------- Phase 6-7: twin, what-if, evidence
export type Level = 0 | 1 | 2;
export interface HealthCell { level: Level; note: string; n?: number; days?: number }
export interface HealthMap {
  at: string; columns: string[];
  rows: { tail: string; unit: string; type: string; status: Status | null; reason: string | null; cells: Record<string, HealthCell | null> }[];
}
export interface Action { priority: 1 | 2 | 3; source: string; action: string; why: string }
export interface LifeRow { tail: string; position: string; pn: string; sn: string; limit: string; consumed: number | null;
  days_left: number | null; due: string | null; basis: string }
export interface InspRow { tail: string; inspection: string; level: string; last_done: string | null; fh_since: number;
  interval_fh: number | null; interval_days: number | null; days_to_due: number; due: string }
export interface TailTwin {
  aircraft: { tail: string; msn: string; type: string; unit: string; base: string; hums_unit: string | null };
  at: string; status: { status: Status; reason: string | null; since: string } | null; usage_fh_per_day: number;
  configuration: { position: string; pn: string; description: string; sn: string; installed: string | null; tso_h_at_export: number | null }[];
  life: LifeRow[]; inspections: InspRow[]; risk: Risk[]; actions: Action[];
  defects: { defect_no: string; raised: string; closed: string | null; category: string; ata: number | null; ata_source: string | null;
             text: string; rectification: string | null; wo_id: string | null }[];
  defects_by_ata: { ata: number | null; n: number }[]; defects_total: number;
}
export interface Band { day: string; mean: number; p10: number; p90: number }
export interface CI { mean: number; ci95: number | null }
export interface ForecastSide { daily: Band[]; by_unit: Record<string, number[]>; availability: CI; task_met: CI; nmcs: CI; nmcm: CI;
  scheduled: CI; failures: CI; backorders: CI; serviceable_aircraft_mean: number }
export interface Levers { flying: number; surge: { start_day: number; days: number; factor: number } | null; lead: number;
  capacity: { O: number; I: number; D: number }; stock: "current" | "metric"; policy: "P0" | "P2" }
export interface Forecast { levers: Levers; days: number; reps: number; start_date: string; records_end_utc: string; aircraft: number;
  open_items: number; baseline: ForecastSide; scenario: ForecastSide; seconds: number;
  difference: { availability: CI; task_met: CI; serviceable_aircraft: CI } }
export interface WhatIfJob { id: string; status: "running" | "done" | "failed"; days: number; reps: number; levers: Partial<Levers>;
  result?: Forecast | null; error?: string | null; seconds?: number }
export interface ArmResult { [metric: string]: CI | Record<string, number> | number }
export interface Study {
  reps: number; days: number; warmup_days: number; seed0: number; seconds: number;
  arms: Record<string, { policy: string; args?: Record<string, unknown>; stock: string }>;
  results: Record<string, Record<string, CI> & { by_unit: Record<string, number>; seconds_per_run: number }>;
  paired_vs_P0: Record<string, Record<string, CI>>; policy: Record<string, Record<string, number>>;
  value_of_prediction: { arm: string; error_multiple: number | null; gain: number; ci95: number; engine_rmse_fh: number | null;
                         gearbox_auc: number | null; in_service_failures: number; predictive_removals: number }[];
  [k: string]: unknown;
}
export interface TwinCheck { run: string; seed: number; records_days: number; forecast_days: number; reps: number;
  forecast: { availability: number; sd: number; failures: number; backorders: number; nmcs: number; task_met: number };
  actual: { availability: number; failures: number; backorders: number; nmcs: number; task_met: number }; inside_95_band: boolean }
export interface PlanDecision { id: number; tail: string; job: string; decision: "accept" | "override"; start_day: number | null;
  note: string | null; user: string; at: string }

// ---------------------------------------------------------------- access control (Phase 8)
export type Permission = "view" | "view_dq" | "export_reports" | "run_whatif" | "decide_plan" | "manage_users" | "view_audit";
export interface Me {
  auth_enabled: boolean; classification: string;
  user: { username: string; display_name: string; role: string; role_label: string; permissions: Permission[]; must_change: boolean } | null;
}
export interface AppUser { id: number; username: string; display_name: string; role: string; disabled: boolean; must_change: boolean;
  failed_logins: number; locked_until: string | null; created: string; last_login: string | null }
export interface AuditRow { id: number; ts: string; actor: string; role: string | null; action: string; target: string | null;
  detail: string | null; ip: string | null; outcome: "ok" | "denied" | "failed" }

export class ApiError extends Error {
  constructor(public status: number, public detail: string, path: string) { super(`${path}: ${status} ${detail}`); }
}

// a 401 anywhere means the session ended: the shell shows the sign-in screen
export const onUnauthorized = { handler: () => undefined as void };

export const API_BASE = (import.meta.env.VITE_API_URL ?? "").replace(/\/+$/, "");

export function reportUrl(path: string): string {
  return API_BASE ? `${API_BASE}${path}` : path;
}

async function call<T>(path: string, method: string, body?: unknown): Promise<T> {
  const url = API_BASE ? `${API_BASE}${path}` : path;
  const r = await fetch(url, {
    method, credentials: API_BASE ? "include" : "same-origin",
    headers: { "X-Requested-With": "airpower", ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    let detail = await r.text();
    try { detail = (JSON.parse(detail) as { detail?: string }).detail ?? detail; } catch { /* plain text */ }
    if (r.status === 401 && path !== "/api/auth/login") onUnauthorized.handler();
    throw new ApiError(r.status, detail, url);
  }
  return r.json() as Promise<T>;
}
const get = <T,>(path: string) => call<T>(path, "GET");
const post = <T,>(path: string, body: unknown) => call<T>(path, "POST", body);
const patch = <T,>(path: string, body: unknown) => call<T>(path, "PATCH", body);

export const api = {
  me: () => get<Me>("/api/auth/me"),
  login: (username: string, password: string) => post<Me>("/api/auth/login", { username, password }),
  logout: () => post<{ status: string }>("/api/auth/logout", {}),
  changePassword: (current: string, next: string) => post<{ status: string }>("/api/auth/password", { current, new: next }),
  adminUsers: () => get<AppUser[]>("/api/admin/users"),
  createUser: (u: { username: string; display_name: string; role: string; password: string }) => post<{ status: string }>("/api/admin/users", u),
  changeUser: (username: string, change: { role?: string; disabled?: boolean; password?: string }) =>
    patch<{ status: string }>(`/api/admin/users/${encodeURIComponent(username)}`, change),
  audit: (q: { action?: string; outcome?: string; actor?: string; offset?: number }) =>
    get<{ total: number; rows: AuditRow[] }>(`/api/admin/audit?${new URLSearchParams(Object.entries({ limit: "100", ...q }).filter(([, v]) => v !== undefined && v !== "").map(([k, v]) => [k, String(v)]))}`),
  auditVerify: () => get<{ ok: boolean; rows: number; first_bad_id: number | null; reason: string | null }>("/api/admin/audit/verify"),
  meta: () => get<Meta>("/api/meta"),
  readiness: (at?: string) => get<Readiness>(`/api/readiness${at ? `?at=${encodeURIComponent(at)}` : ""}`),
  availability: (days = 365) => get<DailyRow[]>(`/api/availability?days=${days}`),
  aircraft: (tail: string, at?: string) => get<AircraftDetail>(`/api/aircraft/${tail}${at ? `?at=${encodeURIComponent(at)}` : ""}`),
  health: (tail: string, position: string, channel: string) =>
    get<{ ts: string; sn: string; value: number }[]>(`/api/aircraft/${tail}/health?position=${encodeURIComponent(position)}&channel=${encodeURIComponent(channel)}`),
  risk: (at?: string, tail?: string) =>
    get<{ at: string; rows: Risk[] }>(`/api/risk?${new URLSearchParams({ ...(at ? { at } : {}), ...(tail ? { tail } : {}) })}`),
  reliability: () => get<Reliability>("/api/reliability"),
  spares: () => get<Spares>("/api/spares"),
  plan: (which: "optimised" | "due_date") => get<Plan>(`/api/plan?which=${which}`),
  defects: (q: string, limit = 50) => get<DefectRow[]>(`/api/defects?q=${encodeURIComponent(q)}&limit=${limit}`),
  dq: () => get<DQ>("/api/dq"),
  dqIssues: (rule: string, limit = 50) => get<DQIssue[]>(`/api/dq/issues?rule=${encodeURIComponent(rule)}&limit=${limit}`),
  healthmap: (at?: string) => get<HealthMap>(`/api/healthmap${at ? `?at=${encodeURIComponent(at)}` : ""}`),
  twin: (tail: string, at?: string) => get<TailTwin>(`/api/aircraft/${tail}/twin${at ? `?at=${encodeURIComponent(at)}` : ""}`),
  forecast: () => get<Forecast | { status: string }>("/api/forecast"),
  whatif: (levers: Partial<Levers>, days: number, reps: number) => post<WhatIfJob>("/api/whatif", { levers, days, reps }),
  whatifJob: (id: string) => get<WhatIfJob>(`/api/whatif/${id}`),
  study: () => get<{ study: Study | null; twin_validation: TwinCheck[] | null }>("/api/study"),
  decide: (d: { tail: string; job: string; decision: "accept" | "override"; start_day?: number; note?: string }) =>
    post<PlanDecision>("/api/plan/decision", d),
  decisions: () => get<PlanDecision[]>("/api/plan/decisions"),
};

export const pct = (x: number | null | undefined, d = 0) => (x == null ? "–" : `${(100 * x).toFixed(d)} %`);
export const pts = (c: CI | null | undefined, d = 1) =>
  c == null ? "–" : `${c.mean >= 0 ? "+" : "−"}${Math.abs(100 * c.mean).toFixed(d)}${c.ci95 != null ? ` ± ${(100 * c.ci95).toFixed(d)}` : ""} pts`;

// UTC ISO -> base local time string (the API reports the base offset in /api/meta)
export function localTime(iso: string, offsetH: number, withTime = true): string {
  const d = new Date(new Date(iso.endsWith("Z") ? iso : iso + "Z").getTime() + offsetH * 3600e3);
  const p = (n: number) => String(n).padStart(2, "0");
  const date = `${p(d.getUTCDate())} ${["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getUTCMonth()]} ${d.getUTCFullYear()}`;
  return withTime ? `${date} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}` : date;
}
