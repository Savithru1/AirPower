#!/usr/bin/env bash
# Phase 3 end to end (about 15 minutes): real-data models, fleet runs, risk records, validation report.
#   bash tools/phase3_pipeline.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/Scripts/python.exe}

$PY -m airpower.health.engine_rul
$PY -m airpower.health.gearbox
$PY -m airpower.health.battery
[ -f outputs/health/premaint.json ] || $PY -m airpower.health.premaint

# training run (gearbox thresholds / calibration) and unseen test run for A5, three years each
$PY -m airpower.fleetsim run --days 1095 --seed 2 --out outputs/fleetsim/risk_s2_3y
$PY -m airpower.fleetsim run --days 1095 --seed 3 --out outputs/fleetsim/risk_s3_3y
$PY -m airpower.ingest.load outputs/fleetsim/risk_s2_3y --db sqlite:///outputs/risk_s2.sqlite
$PY -m airpower.ingest.load outputs/fleetsim/risk_s3_3y --db sqlite:///outputs/risk_s3.sqlite
$PY -m airpower.health.fleet_risk fit-gearbox outputs/fleetsim/risk_s2_3y --db sqlite:///outputs/risk_s2.sqlite
$PY -m airpower.health.fleet_risk build --db sqlite:///outputs/risk_s3.sqlite
$PY -m airpower.health.fleet_risk evaluate outputs/fleetsim/risk_s3_3y --db sqlite:///outputs/risk_s3.sqlite

# dashboard data set (one year)
$PY -m airpower.fleetsim run --days 365 --seed 1
$PY -m airpower.ingest.load outputs/fleetsim/p0_s1_365d
$PY -m tools.reconcile outputs/fleetsim/p0_s1_365d
$PY -m airpower.health.fleet_risk build
$PY -m tools.build_validation
