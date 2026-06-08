#!/usr/bin/env bash
# Render / CI: install Python deps; build SPA into static/ when Node is available.
set -euo pipefail
cd "$(dirname "$0")/.."

pip install -r requirements.txt

if command -v npm >/dev/null 2>&1 && [[ -f frontend/package.json ]]; then
  echo "[render_build] Building frontend → static/"
  (cd frontend && npm ci && npm run build)
else
  echo "[render_build] npm not available; using committed static/ (rebuild locally if UI changed)"
fi
