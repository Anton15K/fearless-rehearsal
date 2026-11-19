#!/usr/bin/env bash
# Start a production frontend and the API on the same origin.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
package="."
if [[ -n "${FEARLESS_BUNDLE:-}" ]]; then
  package=".[model]"
fi
.venv/bin/python -m pip --disable-pip-version-check install -e "$package"
npm --prefix web ci
npm --prefix web run build
mkdir -p .local/rehearsal
export FEARLESS_DB="${FEARLESS_DB:-$PWD/.local/rehearsal/plans.sqlite3}"
export FEARLESS_WEB="$PWD/web/dist"
exec .venv/bin/python -m uvicorn fearless.api:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
