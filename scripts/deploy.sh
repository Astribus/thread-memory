#!/usr/bin/env bash
# Build and deploy to Fly.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v fly >/dev/null; then
  echo "flyctl isn't installed. Run: brew install flyctl" >&2
  exit 1
fi
fly deploy --ha=false   # single machine: the volume attaches to one
echo "Deployed. Check it: fly status   |   logs: fly logs"
