#!/usr/bin/env bash
set -euo pipefail

readonly PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly RUNTIME_DIR="/opt/home-butler"
readonly SERVICE_USER="homebutler"
readonly SERVICE_HOME="/home/homebutler"
readonly UNIT_DIR="/etc/systemd/system"

activate=false
if [[ "${1:-}" == "--activate" ]]; then
  activate=true
elif [[ $# -ne 0 ]]; then
  printf '%s\n' 'usage: install-home-butler-service.sh [--activate]' >&2
  exit 2
fi
(( EUID == 0 )) || { printf '%s\n' 'Run as root.' >&2; exit 2; }
[[ -d "$PROJECT_DIR/.git" || -f "$PROJECT_DIR/.git" ]] \
  || { printf '%s\n' 'Project checkout is unavailable.' >&2; exit 2; }

readonly -a RUNTIME_SCRIPTS=(
  alice_claim_finalizer.py
  alice_skill_gateway.py
  alice_skill_health.py
  alice_tailscale_funnel.py
  bounded_ha_agent.py
  home_assistant_inventory.py
  home_assistant_mcp.py
  home_assistant_read.py
  local_chat_gateway.py
  model_runtime_policy.py
  ollama_endpoint.py
  owner_chat.py
  rotate-alice-webhook.py
  safe_attribute_sanitizer.py
  shadow_action_policy.py
)
readonly -a UNITS=(
  home-butler-local-chat.service
  home-butler-inventory.service
  home-butler-inventory.timer
  home-butler-alice-skill.service
  home-butler-alice-tunnel.service
  home-butler-alice-health.service
  home-butler-alice-health.timer
  home-butler-alice-finalize.service
  home-butler-alice-finalize.path
  home-butler-alice-rotation-finalize.service
  home-butler-alice-rotation-finalize.path
)
readonly -a ACTIVE_UNITS=(
  home-butler-local-chat.service
  home-butler-inventory.timer
  home-butler-alice-skill.service
  home-butler-alice-tunnel.service
  home-butler-alice-health.timer
  home-butler-alice-finalize.path
  home-butler-alice-rotation-finalize.path
)
readonly -a OBSOLETE_UNITS=(home-butler.service)

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$SERVICE_HOME" \
    --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -o root -g root -m 0755 "$RUNTIME_DIR" "$RUNTIME_DIR/scripts" \
  "$RUNTIME_DIR/config" "$UNIT_DIR/ollama.service.d"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 \
  "$SERVICE_HOME/.local/state/home-butler" \
  "$SERVICE_HOME/.local/state/home-butler/alice"

# Runtime is a closed set. Git history and the safety tag retain removed code.
find "$RUNTIME_DIR/scripts" -mindepth 1 -maxdepth 1 -type f -delete
for script in "${RUNTIME_SCRIPTS[@]}"; do
  [[ -f "$PROJECT_DIR/scripts/$script" && ! -L "$PROJECT_DIR/scripts/$script" ]] \
    || { printf 'Missing runtime script: %s\n' "$script" >&2; exit 2; }
  mode=0755
  [[ "$script" == *.py ]] && mode=0555
  install -o root -g root -m "$mode" "$PROJECT_DIR/scripts/$script" \
    "$RUNTIME_DIR/scripts/$script"
done
install -o root -g root -m 0444 "$PROJECT_DIR/config/home-assistant.example.env" \
  "$RUNTIME_DIR/config/home-assistant.env"
install -o root -g root -m 0444 "$PROJECT_DIR/SOUL.md" "$RUNTIME_DIR/SOUL.md"
install -o root -g root -m 0444 "$PROJECT_DIR/config/ollama.service.override.conf" \
  "$UNIT_DIR/ollama.service.d/zz-home-butler.conf"

for unit in "${UNITS[@]}"; do
  install -o root -g root -m 0444 "$PROJECT_DIR/config/systemd/$unit" "$UNIT_DIR/$unit"
done

systemctl daemon-reload
if $activate; then
  rm -f -- "$UNIT_DIR/ollama.service.d/home-butler.conf"
  for unit in "${OBSOLETE_UNITS[@]}"; do
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
    rm -f -- "$UNIT_DIR/$unit"
  done
  rm -rf -- "$RUNTIME_DIR/hermes" "$RUNTIME_DIR/hermes-agent"
  mapfile -t installed < <(find "$UNIT_DIR" -maxdepth 1 -type f \
    \( -name 'home-butler.service' -o -name 'home-butler-*' \) -printf '%f\n' | sort)
  for unit in "${installed[@]}"; do
    keep=false
    for expected in "${UNITS[@]}"; do
      [[ "$unit" == "$expected" ]] && { keep=true; break; }
    done
    if ! $keep; then
      systemctl disable --now "$unit" >/dev/null 2>&1 || true
      rm -f -- "$UNIT_DIR/$unit"
    fi
  done
  systemctl daemon-reload
  for unit in "${OBSOLETE_UNITS[@]}"; do
    systemctl reset-failed "$unit" >/dev/null 2>&1 || true
  done
  systemctl enable "${ACTIVE_UNITS[@]}"
  systemctl restart ollama.service
  systemctl restart home-butler-inventory.service
  systemctl restart home-butler-local-chat.service \
    home-butler-alice-skill.service home-butler-alice-tunnel.service
  health_ok=false
  for _attempt in {1..12}; do
    if systemctl start home-butler-alice-health.service; then
      health_ok=true
      break
    fi
    systemctl reset-failed home-butler-alice-health.service
    if (( _attempt < 12 )); then
      sleep 3
    fi
  done
  $health_ok || { printf '%s\n' 'Alice health check failed.' >&2; exit 1; }
fi

printf 'Home Butler Stage 71 installed (activated=%s).\n' "$activate"
