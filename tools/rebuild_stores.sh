#!/usr/bin/env bash
# Rebuild the stores and everything derived from them, from the existing FleetSim runs and trained
# models (no model retraining). About 20 minutes.
#   bash tools/rebuild_stores.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/Scripts/python.exe}
S2=sqlite:///outputs/risk_s2.sqlite
S3=sqlite:///outputs/risk_s3.sqlite

$PY -m airpower.fleetsim run --days 365 --seed 1
$PY -m airpower.ingest.load outputs/fleetsim/p0_s1_365d
$PY -m tools.reconcile outputs/fleetsim/p0_s1_365d
$PY -m airpower.health.fleet_risk build
$PY -m airpower.reliability.analyse build

$PY -m airpower.ingest.load outputs/fleetsim/risk_s2_3y --db $S2
$PY -m airpower.ingest.load outputs/fleetsim/risk_s3_3y --db $S3
$PY -m airpower.health.fleet_risk fit-gearbox outputs/fleetsim/risk_s2_3y --db $S2
$PY -m airpower.health.fleet_risk build --db $S3
$PY -m airpower.health.fleet_risk evaluate outputs/fleetsim/risk_s3_3y --db $S3
$PY -m airpower.reliability.analyse build --db $S2
$PY -m airpower.reliability.analyse evaluate outputs/fleetsim/risk_s3_3y --db $S3
$PY -m tools.build_validation
echo REBUILD_DONE
