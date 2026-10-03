#!/usr/bin/env bash
# Start the approval console on mocked AWS at http://127.0.0.1:${PORT:-8000}
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD

if [ ! -d "$ROOT/.platform/ml_platform" ]; then
  echo "Platform checkout missing: git clone --branch feature/v7-alignment https://github.com/Debashis2007/ML-Platform.git .platform" >&2
  exit 1
fi

if [ ! -d ui/frontend/dist ] || [ "${REBUILD:-0}" = "1" ]; then
  (cd ui/frontend && npm install --no-fund --no-audit && npm run build)
fi

export PYTHONPATH="$ROOT/.platform:$ROOT/ui/backend"
export MLP_CONSOLE_MODE="${MLP_CONSOLE_MODE:-local}"
exec python3 -m uvicorn app:app --host 127.0.0.1 --port "${PORT:-8000}"
