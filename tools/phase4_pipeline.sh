#!/usr/bin/env bash
# Phase 4 end to end (about 15 minutes; needs Phase 3's runs and stores): real defect text, reliability,
# rogue serials, repeat defects, validation report.
#   bash tools/phase4_pipeline.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/Scripts/python.exe}

$PY tools/fetch_real_data.py faa_sdrs                    # FAA Service Difficulty Reports 2020-2025 (~190 MB)
$PY -m airpower.nlp.ata                                  # A7 on real defect text
$PY -m airpower.reliability.sdr_repeats                  # A8 on real reports
$PY -m airpower.reliability.analyse evaluate outputs/fleetsim/risk_s3_3y --db sqlite:///outputs/risk_s3.sqlite
$PY -m airpower.reliability.analyse build                # dashboard store
$PY -m tools.build_validation
