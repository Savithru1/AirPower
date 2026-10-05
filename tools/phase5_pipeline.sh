#!/usr/bin/env bash
# Phase 5 end to end (about 10 minutes; needs the stores from tools/rebuild_stores.sh): spares
# forecast backtest, METRIC (training store and dashboard store), validation in fresh simulated
# fleets, AOG risk, maintenance plans, validation report.
#   bash tools/phase5_pipeline.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/Scripts/python.exe}
S2=sqlite:///outputs/risk_s2.sqlite
S3=sqlite:///outputs/risk_s3.sqlite

$PY -m airpower.spares.forecast backtest --db $S3 --rel-db $S2 --horizon 60
$PY -m airpower.spares.metric build --db $S2
$PY -m airpower.spares.validate --db $S2 --reps 20 --procs 4
$PY -m airpower.spares.metric build                      # dashboard store
$PY -m airpower.spares.actions build
$PY -m airpower.plan.planner build
$PY -m airpower.plan.planner build --surge 1.4 --no-store
$PY -m tools.build_validation
