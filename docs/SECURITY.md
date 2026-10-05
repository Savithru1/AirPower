# AirPower: security

Threat model, the controls that exist in this repository, the gaps, and what must happen before any
operational use. Structure follows AeroTwin's `SECURITY.md` (assets, STRIDE, controls, residual risk).

**Status.** The controls marked *implemented* are in code and covered by tests
(`tests/test_security.py`, `tests/test_integration.py`). The system runs on **synthetic data** with
**demo keys and demo accounts**. It has not been penetration-tested, has no PKI or multi-factor sign-in,
and the SQLite store is not encrypted. Section 6 lists what closes those gaps. The algorithms used
(scrypt, HMAC-SHA256, TLS 1.2/1.3) are standard placeholders; algorithms and key management for an
operational military system need approval from the authority the programme designates.

## 1. Assets and trust boundaries

| Asset | Why it matters |
|---|---|
| Fleet state: readiness, component health, spares position, maintenance plans | Confidentiality: it reveals operational capability. Integrity: wrong data leads to wrong maintenance, spares or tasking decisions |
| Maintenance records, defect text, HUMS downloads | Integrity: they are the basis of every prediction. Confidentiality: aircraft-level detail |
| Model files (joblib) | Integrity: loading a changed pickle runs the code inside it |
| Users, roles, password hashes, session key | Compromise defeats every access control |
| Audit log | Evidence of who decided what; its value depends on it being unalterable |
| Keys: HUMS ingest HMAC key, model manifest key, session key | Compromise defeats the matching control |

Trust boundaries: (1) the browser and the API; (2) the HUMS ground station and the ingest endpoint;
(3) the four source systems and the integration run; (4) the application and the store; (5) the
container and the host.

## 2. Roles and what they may do

Deny by default: every `/api` and `/report` route has an access rule in `server/app.py` (`RULES`);
a route without a rule is refused, and `tests/test_security.py` fails if one is added without a rule.
Role and disabled state are read from the store on every request, so a change applies at once.

| Role | Holds | In practice |
|---|---|---|
| Commander | view, export_reports, run_whatif | Readiness, forecast, what-if, evidence, reports |
| Engineering officer | view, export_reports, run_whatif, **decide_plan** | The above and accepting or moving planned jobs |
| Logistics officer | view, export_reports, run_whatif | Spares and risk views, what-if |
| Analyst | view, export_reports, run_whatif, **view_dq** | The above and the data-integration views |
| Administrator | view, view_dq, **manage_users, view_audit** | Users and the audit log; cannot decide plans, run what-ifs or export reports (separation of duties) |

## 3. Threat model (STRIDE)

S = spoofing, T = tampering, R = repudiation, I = information disclosure, D = denial of service,
E = elevation of privilege.

### 3.1 Browser to API

| | Threat | Implemented | Gap / recommendation |
|---|---|---|---|
| S | Guessing or stealing a password | scrypt (N = 2^15) with per-user salt; 12-character minimum; five failures lock the account for five minutes; ten failures per address in ten minutes are refused (429); identical message for unknown user, wrong password and locked account, with equal work done (no user enumeration by message or timing); temporary passwords must be changed at first sign-in | No multi-factor sign-in; no integration with the unit's directory or smart cards |
| S | Forged or replayed session | Session = HMAC-SHA256 signed token, 8 hours, in an HttpOnly, SameSite=Strict cookie (Secure over HTTPS); sign-out revokes the token; a user disabled or demoted stops working immediately | Fixed lifetime: add idle timeout and a session list per user |
| T | Cross-site request forgery | SameSite=Strict, and every state-changing call must carry `X-Requested-With: airpower` (a header a cross-site page cannot send without a CORS grant, and none is given) | |
| T | Changing a planned job's date | Decisions are written to the audit log first; a decision that cannot be audited is refused; the user is taken from the session, never from the request | |
| R | "I did not accept that plan" | Hash-chained audit log: user, role, address, time, outcome for sign-ins, failures, lock-outs, plan decisions, what-if runs, report exports, user changes, data loads, signed uploads refused, access denied (section 4) | Forward the log to the unit's central log service |
| I | Reading data above one's role | Deny-by-default rules (above); API documentation and OpenAPI are not served when sign-in is on; errors are generic with a request id, no stack traces; `Cache-Control: no-store` on API and reports | Row-level restrictions (a squadron sees its own aircraft) are not implemented |
| I | Data leaving the system | Reports are the only export; each is audited and carries the classification label, who prepared it and when | No raw data export, by design; screenshots cannot be controlled |
| I | Script injection into the dashboard | Strict content security policy (`script-src 'self'`, no inline script), React escapes text, reports use a per-response script nonce, Jinja2 auto-escaping, `X-Content-Type-Options`, `X-Frame-Options: DENY`, `frame-ancestors 'none'`; echarts upgraded after an advisory (section 5) | `style-src` allows inline styles because the charting library writes them |
| D | Request floods, large bodies, expensive calls | Body limit (256 KB; checked also when no length is declared); what-if: one running forecast per user, two in the server, ranges on every lever, horizon and replications capped; the TLS proxy rate-limits sign-in | SQLite allows one writer: PostgreSQL is the production store |
| E | A role reaching another role's routes | Rules per method and path, tested for every role (`test_roles`); an administrator cannot demote or disable themselves, and the last active administrator cannot be removed | |

### 3.2 HUMS uplink (`POST /api/ingest/hums`)

| | Threat | Implemented | Gap / recommendation |
|---|---|---|---|
| S | A forged download | HMAC-SHA256 over the whole envelope, timestamp window, replay cache (AeroTwin's `SignedJson`); a wrong key, a replay and a tampered body are refused and audited | Per-ground-station keys held in a secure element or key service; the demo key label is public |
| T | Poisoned values reaching the models | Field validation (names, counts, numeric values only), unknown unit / airframe pairs refused, duplicates detected | Plausibility limits per channel before models see a value |
| D | Flooding | Body limit; the signature is checked before any database work | Rate limit per ground station |

### 3.3 Source systems and integration

| | Threat | Implemented | Gap / recommendation |
|---|---|---|---|
| T | A source export altered in transit or at rest | Every inconsistency found in the data is logged with what was done (data-quality issues); counts reconcile with the simulator's truth in tests | Exports should be signed by the source system and checked at load; not implemented |
| T | Wrong data from a legitimate source | Entity resolution, duplicate and link rules, and a data-quality report that flags what cannot be resolved | |
| R | Who loaded what | Each load is written to the audit log (operating-system user, run, table counts) | |
| D | A bad load leaving a half-built store | The integration report is written last and `/api/ready` stays false until it exists; users, decisions and the audit log are kept across loads | Load into a new database and switch over (PostgreSQL) |

### 3.4 Models and files

| | Threat | Implemented | Gap / recommendation |
|---|---|---|---|
| T / E | A replaced model file runs code when loaded (joblib is pickle) | Every model saved by the training code is recorded in `outputs/models/MANIFEST.json` (SHA-256); loading checks it first and refuses a changed or unlisted file; with `AIRPOWER_MODEL_KEY` the manifest is HMAC-signed, so replacing model and manifest needs the key; `AIRPOWER_VERIFY_MODELS=strict` refuses to load without a manifest | Models trained elsewhere should be signed by a release process; the manifest and key must not be writable by the application user |
| T | Replacing the store file | The audit chain shows edits to the audit log; the store file's own integrity is the host's responsibility | File-integrity monitoring on the host |

### 3.5 Deployment

| | Threat | Implemented | Gap / recommendation |
|---|---|---|---|
| E | A compromised application process | Container runs as an unprivileged user (uid 10001), read-only root file system, all capabilities dropped, `no-new-privileges`; secrets are mounted as files, not environment variables | Not built or run in the environment where this was written (no container engine was running); verify with `docker compose up` and a scan of the image |
| I | Traffic sniffed on the network | TLS 1.2 / 1.3 terminated by the proxy profile (or by the application with `AIRPOWER_TLS_CERT` and `_KEY`); HSTS; the Secure cookie flag; PostgreSQL `sslmode=require` in the connection URL | Certificates from the unit's PKI; the demo certificate is self-signed |
| I | Data at rest | PostgreSQL volume on an encrypted disk (host policy); secrets never in the image | The SQLite store is not encrypted |
| S | Spoofed client address behind the proxy | `X-Forwarded-For` is believed only from `AIRPOWER_TRUSTED_PROXY`, and the proxy replaces what the client sent | |
| D | Container resource exhaustion | Compose can set limits; none are set by default | Set CPU and memory limits for the deployment |

## 4. Audit log

Append-only: the database refuses UPDATE and DELETE on `audit_log` (a trigger on SQLite; a trigger
written for PostgreSQL but **not exercised** here). Hash-chained: each row stores the previous row's
hash and its own hash over (previous hash, time, user, role, action, target, detail, address,
outcome), so a removed or edited row breaks the chain from there. *Administration > Verify the chain*
recomputes it, and `python -m tools.backup verify FILE` does the same on a backup. Tested: the
database blocks edits, and when the trigger is dropped and a row edited the chain reports the row.

Recorded: sign-in, failure and lock-out, sign-out, password change, user create / change, plan
accepted / moved, what-if run, report export, signed HUMS upload refused, data load, access denied.
Not recorded: reading dashboard views, and accepted HUMS uploads (they would be one row per sortie;
the store holds them).

A hash chain stored next to the data it protects shows tampering but does not prevent a user with full
database rights from rewriting the whole chain. The remedy is to forward entries to a separate log service
as they are written.

## 5. Dependencies

`pip-audit` on `requirements.txt` and `npm audit --omit=dev` were run on 2026-10-05. Findings and what was done:

| Package | Finding | Action |
|---|---|---|
| starlette 1.0.0 | Host header and request path used unvalidated to rebuild `request.url`; a form-parsing limit; Windows static-file and endpoint-dispatch issues (PYSEC-2026-161, 248, 249, 2280, 2281) | Pinned 1.3.1 (all fixed) |
| cryptography 46.0.7 | Certificate-chain and PKCS#7 issues, bundled OpenSSL (PYSEC-2026-3552 to 3554, GHSA-537c-gmf6-5ccf) | Pinned 50.0.2. Used only to make the demo certificate |
| echarts 5.6 | Cross-site scripting advisory | Upgraded to 6.1.0; npm audit reports no vulnerabilities |

Licences are in [THIRD_PARTY.md](THIRD_PARTY.md) (generated). `psycopg` is LGPL-3.0 and `progpy` is under the NASA Open Source
Agreement; both are used unmodified. Re-run both audits before every release.

## 6. Before operational use

1. Sign-in through the unit's identity provider or smart cards, with multi-factor, replacing local passwords.
2. Keys in an HSM or key service; per-ground-station HUMS keys; a signed release process for models and for source exports.
3. PostgreSQL with TLS and an encrypted volume; the audit log forwarded to a separate service; backups tested for restore.
4. Row-level restrictions by unit; data classification inherited per record from each source system (here one label per store).
5. Independent penetration test and a review of the container image; a software bill of materials from the build.
6. Clock synchronisation (audit times, replay window), monitoring and alerting on audit events (repeated lock-outs, denied access).

## 7. Configuration reference

Environment variables are listed in `server/settings.py`. A deployment sets at least `AIRPOWER_SECRET_FILE`,
`AIRPOWER_ADMIN_PASSWORD_FILE`, `AIRPOWER_CLASSIFICATION`, `AIRPOWER_TRUSTED_PROXY` (behind a proxy) and
`AIRPOWER_MODEL_KEY`; never `AIRPOWER_AUTH=off` or `--demo-users`. With authentication off the server refuses to listen
on anything but loopback and the screen carries a red warning.
