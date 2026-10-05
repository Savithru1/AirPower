#!/usr/bin/env bash
# Prove the README works from nothing: copy the repository (without environments, data or outputs) to a scratch
# directory, install into a new virtual environment, run the tests that need no datasets, simulate and integrate
# 90 days, start the server with a bootstrap administrator, sign in and read data over the API.
#
#   bash tools/clean_install_check.sh [--web]      (--web also builds the dashboard with npm ci; needs Node)
#
# Needs network access for pip. About 10 minutes. Exit status 0 only if every step passed.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${CHECK_DIR:-$(mktemp -d)}"
WEB=0; [ "${1:-}" = "--web" ] && WEB=1
step() { printf '\n== %s (%s)\n' "$1" "$(date +%H:%M:%S)"; }

step "copy the repository to $WORK"
mkdir -p "$WORK/airpower"
(cd "$SRC" && tar --exclude=.venv --exclude=node_modules --exclude=outputs --exclude=web/dist --exclude=data/raw \
    --exclude=data/processed --exclude=deploy/secrets --exclude=deploy/certs --exclude=.claude --exclude=__pycache__ \
    --exclude=.pytest_cache --exclude='*.sqlite*' -cf - .) | (cd "$WORK/airpower" && tar -xf -)
cd "$WORK/airpower"

step "create the virtual environment and install"
python -m venv .venv
if [ -x .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe; else PY=.venv/bin/python; fi
$PY -m pip install --quiet --disable-pip-version-check -r requirements.txt
$PY -m pip install --quiet --no-deps --ignore-requires-python progpy==1.7.1
$PY -m pip install --quiet --no-deps --no-build-isolation filterpy==1.4.5 || $PY -m pip install --quiet --no-deps filterpy==1.4.5

step "tests that need no datasets"
$PY -m pytest -q -m "not slow and not data" -p no:warnings

step "simulate 90 days (parametric signals), integrate, reconcile"
$PY -m airpower.fleetsim run --days 90 --seed 1 --out outputs/fleetsim/check --no-real-signals
$PY -m airpower.ingest.load outputs/fleetsim/check --db sqlite:///outputs/check.sqlite
$PY -m tools.reconcile outputs/fleetsim/check --db sqlite:///outputs/check.sqlite

if [ $WEB = 1 ]; then
  step "build the dashboard"
  (cd web && npm ci --no-audit --no-fund && npm run build)
fi

step "start the server with a bootstrap administrator and use the API"
export AIRPOWER_DB="sqlite:///$WORK/airpower/outputs/check.sqlite"
export AIRPOWER_ADMIN_PASSWORD="check-Passphrase-$RANDOM-$RANDOM"
PORT=$((20000 + RANDOM % 20000))
$PY -m server.app --port "$PORT" > "$WORK/server.log" 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
for i in $(seq 1 60); do
  if curl -fs "http://127.0.0.1:$PORT/api/health" > /dev/null 2>&1; then break; fi
  sleep 1
done
curl -fs "http://127.0.0.1:$PORT/api/health"; echo
curl -fs "http://127.0.0.1:$PORT/api/ready"; echo
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/readiness")
[ "$code" = "401" ] || { echo "expected 401 without sign-in, got $code"; exit 1; }
JAR="$WORK/jar.txt"
curl -fs -c "$JAR" -H "X-Requested-With: airpower" -H "Content-Type: application/json" \
  -d "{\"username\":\"admin\",\"password\":\"$AIRPOWER_ADMIN_PASSWORD\"}" "http://127.0.0.1:$PORT/api/auth/login" | head -c 200; echo
code=$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" "http://127.0.0.1:$PORT/api/readiness")
[ "$code" = "403" ] || { echo "expected 403 until the temporary password is changed, got $code"; exit 1; }
NEW="check-New-Passphrase-$RANDOM-$RANDOM"
curl -fs -b "$JAR" -c "$JAR" -H "X-Requested-With: airpower" -H "Content-Type: application/json" \
  -d "{\"current\":\"$AIRPOWER_ADMIN_PASSWORD\",\"new\":\"$NEW\"}" "http://127.0.0.1:$PORT/api/auth/password"; echo
curl -fs -b "$JAR" "http://127.0.0.1:$PORT/api/readiness" | head -c 200; echo
curl -fs -b "$JAR" "http://127.0.0.1:$PORT/api/admin/audit/verify"; echo
step "clean install check PASSED"
[ -n "${CHECK_DIR:-}" ] || echo "(scratch directory: $WORK)"
