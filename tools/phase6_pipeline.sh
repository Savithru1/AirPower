#!/usr/bin/env bash
# Phases 6-7 end to end (about 1.5 hours; needs the stores from tools/rebuild_stores.sh and the
# Phase 5 outputs): engine model with mid-life starts, risk records, the policy study in the
# simulated fleet (resumable), the twin's forecast check, the dashboard forecast, validation report.
#   bash tools/phase6_pipeline.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/Scripts/python.exe}
S3=sqlite:///outputs/risk_s3.sqlite

$PY -m airpower.health.engine_rul
$PY -m airpower.health.fleet_risk build --db $S3
$PY -m airpower.health.fleet_risk evaluate outputs/fleetsim/risk_s3_3y --db $S3
$PY -m airpower.health.fleet_risk build
$PY -m airpower.twin.study --reps 30 --procs 12
$PY -m airpower.twin.whatif validate --days 30 --reps 20 --procs 8
$PY -m airpower.twin.whatif forecast --days 30 --reps 20 --procs 8
$PY -m tools.build_validation
