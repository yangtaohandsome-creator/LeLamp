#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TARGET=${LELAMP_PI_TARGET:-lamppi@192.168.40.77:/home/lamppi/lelamp_runtime/}

# Pi-generated state, recordings, calibration, models, secrets and diagnostics are
# device data. Code syncs must never overwrite or delete them.
rsync -az --no-group --delete \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='.env' \
  --exclude='runtime_state/' \
  --exclude='voice_debug/' \
  --exclude='kws_models/' \
  --exclude='node_modules/' \
  --exclude='__pycache__/' \
  --exclude='lelamp/recordings/' \
  --exclude='lelamp/*/calibration/' \
  "$ROOT/" "$TARGET"
