#!/usr/bin/env bash
set -euo pipefail
runtime_dir="$(cd "$(dirname "$0")/.." && pwd)"
set -a
source "$runtime_dir/.env"
source "$runtime_dir/voice.conf"
set +a
node_bin="${PI_NODE_BIN:-/home/lamppi/.local/lib/node-v24.21.0-linux-arm64/bin/node}"
if [[ ! -x "$node_bin" ]]; then
  node_bin="$(command -v node)"
fi
exec "$node_bin" "$runtime_dir/pi-agent/dist/server.js"
