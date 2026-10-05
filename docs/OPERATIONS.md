# AirPower: operations

Install, run, administer, back up and upgrade. Everything here was exercised on the build machine except
the container and PostgreSQL steps, which are marked.

## 1. Install from a clean machine

Needs Python 3.13 and Node 24 (Node only to build the dashboard). On Windows or Linux:

```bash
git clone <repository> airpower && cd airpower
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt            # Windows: .venv/Scripts/python
.venv/bin/python -m pip install --no-deps --ignore-requires-python progpy==1.7.1
.venv/bin/python -m pip install --no-deps --no-build-isolation filterpy==1.4.5
(cd web && npm ci && npm run build)
.venv/bin/python -m pytest -q -m "not slow and not data"        # tests that need no datasets
```

`tools/clean_install_check.sh` does all of this in a scratch directory, then simulates 90 days, integrates
them and starts the server, to prove the README works from nothing. Datasets: see [DATA_PLAN.md](DATA_PLAN.md);
without them the simulator uses parametric signals and the trained models in `outputs/models/` are needed for
the health views (rebuild them with `bash tools/phase3_pipeline.sh`).

## 2. Run

```bash
# a store to look at: simulate a year, integrate it
python -m airpower.fleetsim run --days 365 --seed 1
python -m airpower.ingest.load outputs/fleetsim/p0_s1_365d
python -m airpower.health.fleet_risk build                 # risk records (needs the trained models)

# the server (sign-in is on)
python -m server.app --demo-users                          # demo accounts: deploy/demo_users.yaml; demos and tests only
```

For a real first start, give the first administrator a password and a session key instead of `--demo-users`:

```bash
python -m tools.init_deploy                                # writes deploy/secrets/ with random values
export AIRPOWER_SECRET_FILE=deploy/secrets/airpower_secret.txt
export AIRPOWER_ADMIN_PASSWORD_FILE=deploy/secrets/airpower_admin_password.txt
python -m server.app
```

The administrator is `admin`; the temporary password must be changed at first sign-in. Settings are in
`server/settings.py`. Never start with `AIRPOWER_AUTH=off` outside a development machine.

## 3. Users

*Administration* in the dashboard: add users, change roles, disable, reset passwords, read the audit log.
From the command line (no dashboard needed, for example to recover an administrator):

```bash
python -m server.users list
python -m server.users create --username a.kumar --role engineering_officer --name "A Kumar"
python -m server.users reset-password --username a.kumar
python -m server.users disable --username a.kumar          # --enable to undo
```

Roles are in [SECURITY.md](SECURITY.md) section 2. After five wrong passwords an account locks for five
minutes; clear it sooner by resetting the password.

## 4. Updating the data

Integration rebuilds the integrated tables from the cumulative exports of the four systems, atomically from
the user's point of view (`/api/ready` is false until it finishes), and keeps users, plan decisions and the
audit log. Live HUMS downloads arrive through the signed uplink and do not need a load.

```bash
python -m airpower.ingest.load <run folder with sources/>            # then, to refresh the analytics:
python -m airpower.health.fleet_risk build
python -m airpower.reliability.analyse build
python -m airpower.spares.metric build && python -m airpower.spares.actions build
python -m airpower.plan.planner build
python -m airpower.twin.whatif forecast --days 30 --reps 20
```

Each load is recorded in the audit log. Decisions engineering officers made on the previous plan are kept
and applied to the next one.

## 5. Back up and restore

```bash
python -m tools.backup backup                              # online SQLite backup + integrity and audit-chain check, keeps 14
python -m tools.backup verify outputs/backups/airpower-<stamp>.sqlite
python -m tools.backup restore outputs/backups/airpower-<stamp>.sqlite --force      # stop the server first
```

Back up `outputs/models/` (with its `MANIFEST.json`) and `deploy/secrets/` separately and securely; they are
not in the database backup. PostgreSQL: `pg_dump --format=custom` and `pg_restore` (the backup tool prints
the command; **not exercised here**). Restore drill: restore into a scratch location, `verify`, start the
server against it with `AIRPOWER_DB`, and check *Administration > Verify the chain*.

## 6. Containers (written, not built here)

```bash
python -m tools.init_deploy --demo-cert                    # secrets, and a self-signed certificate for a demo
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml --profile tls up -d --build              # + https://127.0.0.1:8443
docker compose -f deploy/docker-compose.yml --profile postgres --profile tls up -d --build
```

The compose file passes `docker compose config` (checked). The image, the TLS proxy and PostgreSQL were not
run, because no container engine was running where this was prepared: run them once and fix what the first
start shows. The app mounts `outputs/` (store and models) and `data/` read-only. For the PostgreSQL profile set
`AIRPOWER_DB=postgresql+psycopg://airpower:<password>@db/airpower?sslmode=require` and load a run into it with
`--db`. To run behind your own proxy instead, set `AIRPOWER_TRUSTED_PROXY` to the proxy's address.

## 7. Health and logs

| Check | How |
|---|---|
| Process alive | `GET /api/health` (no sign-in) |
| Store loaded | `GET /api/ready` (503 until an integration has finished) |
| Audit chain | *Administration > Verify the chain*, or `GET /api/admin/audit/verify` |
| Logs | One line per request on standard output: request id, method, path, status, time, user. Passwords and request bodies are never logged. Every response carries `X-Request-ID`, and an error shown to a user quotes it |
| Level | `AIRPOWER_LOG` (default INFO) |

## 8. Upgrading

1. Back up (section 5).
2. Pull, `pip install -r requirements.txt`, `npm ci && npm run build`.
3. Start the server: operational tables are migrated automatically (`airpower/model/migrate.py`; or run `python -m airpower.model.migrate`), the version is in `schema_info`.
4. Re-run `pip-audit -r requirements.txt` and `npm audit --omit=dev`; regenerate `docs/THIRD_PARTY.md` (`python -m tools.licences`).
5. After retraining models: `python -m airpower.health.artifacts verify` (training records new files in the manifest itself).

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Sign-in screen forever / 401 everywhere | Server restarted without `AIRPOWER_SECRET`: sessions end at restart. Set a secret |
| "password change required" on every call | The account has a temporary password: change it (the dashboard asks) |
| `ArtifactError ... does not match the model manifest` | A model file changed since training. If you trained it, `python -m airpower.health.artifacts manifest`; if not, treat it as tampering |
| `/api/ready` 503 | No store, or a load is running or failed part-way: load again |
| What-if says the server is busy | Two forecasts are running (one per user): wait a minute |
| Health views empty | Risk records not built: `python -m airpower.health.fleet_risk build` |
| Forecast uses parametric signals | The optional datasets are not mounted: set `AEROTWIN_DATA` |
