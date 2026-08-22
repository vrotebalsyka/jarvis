#!/usr/bin/env bash
set -euo pipefail

readonly PROJECT_DIR="/opt/home-butler"
readonly EXPECTED_HOME="/home/homebutler"
readonly EXPECTED_HERMES_HOME="$EXPECTED_HOME/.hermes"
readonly HERMES_PYTHON="$PROJECT_DIR/hermes-agent/venv/bin/python"

if (( EUID == 0 )); then
  printf '%s\n' 'Refusing to run the Hermes gateway as root.' >&2
  exit 2
fi
if [[ "${HOME:-}" != "$EXPECTED_HOME" \
  || "${HERMES_HOME:-}" != "$EXPECTED_HERMES_HOME" ]]; then
  printf '%s\n' 'Hermes gateway runtime identity is invalid.' >&2
  exit 2
fi

HOME_BUTLER_OLLAMA_BASE_URL="$(
  /usr/bin/python3 "$PROJECT_DIR/scripts/ollama_endpoint.py"
)"
export HOME_BUTLER_OLLAMA_BASE_URL
export PYTHONDONTWRITEBYTECODE=1

exec "$HERMES_PYTHON" -m hermes_cli.main gateway run --external-supervisor
