#!/usr/bin/env bash
# Interactive owner launcher for the fixed Tuya Local 2026.7.2 maintenance action.
set -euo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C
umask 077

readonly PROJECT_DIR="/root/Jarvis/home-butler"
readonly UPGRADE_SCRIPT="$PROJECT_DIR/scripts/tuya_local_upgrade.py"
readonly INVENTORY="/home/homebutler/.local/state/home-butler/incidents/inventory.json"
readonly RECOVERY_KEY="$PROJECT_DIR/secrets/ha-recovery-ed25519"
readonly KNOWN_HOSTS="$PROJECT_DIR/config/ha-recovery-known_hosts"
readonly REMOTE_HOST="192.168.1.127"
readonly HOST_ALIAS="homebutler-recovery-target"
readonly MODE="${1:-upgrade}"
readonly -a RECOVERY_TIMERS=(
  home-butler-recovery.timer
  home-butler-core-recovery.timer
  home-butler-out-of-band-recovery.timer
)

fail() {
  printf '%s\n' "$1" >&2
  exit 2
}

(( EUID == 0 )) || fail 'Run as root inside Ubuntu WSL.'
(( $# <= 1 )) || fail 'Usage: upgrade-tuya-local.sh [--check]'
[[ "$MODE" == "upgrade" || "$MODE" == "--check" ]] \
  || fail 'Usage: upgrade-tuya-local.sh [--check]'
for path in "$UPGRADE_SCRIPT" "$INVENTORY" "$RECOVERY_KEY" "$KNOWN_HOSTS"; do
  [[ -f "$path" && ! -L "$path" ]] || fail "Unsafe or missing local file: $path"
  (( (8#$(stat -c '%a' -- "$path") & 8#022) == 0 )) \
    || fail "Local file is writable by group or others: $path"
done
python3 "$UPGRADE_SCRIPT" --check
if [[ "$MODE" == "--check" ]]; then
  printf '%s\n' 'TUYA_LOCAL_UPGRADE_LAUNCHER_CHECK_OK'
  exit 0
fi

for timer in "${RECOVERY_TIMERS[@]}"; do
  [[ "$(systemctl is-enabled "$timer" 2>/dev/null)" == "enabled" \
    && "$(systemctl is-active "$timer" 2>/dev/null)" == "active" ]] \
    || fail "Required recovery timer is not active: $timer"
done
recovery_result=$(timeout 15 ssh -F /dev/null -i "$RECOVERY_KEY" \
  -o BatchMode=yes -o IdentitiesOnly=yes -o ClearAllForwardings=yes \
  -o RequestTTY=no -o StrictHostKeyChecking=yes \
  -o "UserKnownHostsFile=$KNOWN_HOSTS" -o GlobalKnownHostsFile=/dev/null \
  -o "HostKeyAlias=$HOST_ALIAS" -o UpdateHostKeys=no -o VerifyHostKeyDNS=no \
  -o ConnectTimeout=5 -o ConnectionAttempts=1 -o ControlMaster=no \
  -o ControlPath=none -o LogLevel=ERROR \
  "homebutler-recovery@$REMOTE_HOST" status)
[[ "$recovery_result" == 'status=healthy_no_action' ]] \
  || fail 'Independent recovery proof is not healthy.'

systemctl start home-butler-inventory.service
python3 - "$INVENTORY" <<'PY'
import json, stat, sys, time
from pathlib import Path

path = Path(sys.argv[1])
metadata = path.lstat()
if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 8 * 1024 * 1024:
    raise SystemExit(2)
if time.time() - metadata.st_mtime > 300:
    raise SystemExit(2)
data = json.loads(path.read_text(encoding="ascii"))
backup = data.get("backup_readiness")
capabilities = data.get("integration_capabilities")
tuya = capabilities.get("tuya_local") if isinstance(capabilities, dict) else None
if not isinstance(backup, dict) or backup.get("status") != "recent_complete_backup":
    raise SystemExit(2)
if backup.get("core_version") != "2026.7.4" or not isinstance(backup.get("age_seconds"), int):
    raise SystemExit(2)
if not 0 <= backup["age_seconds"] <= 3600 or not isinstance(backup.get("restore_tested"), bool):
    raise SystemExit(2)
if not isinstance(tuya, dict) or tuya.get("core_version") != "2026.7.4":
    raise SystemExit(2)
if tuya.get("installed_version") != "2026.5.4" or tuya.get("latest_version") != "2026.7.2":
    raise SystemExit(2)
if tuya.get("reviewed_target_version") != "2026.7.2" or tuya.get("upgrade_status") != "backup_required_before_update":
    raise SystemExit(2)
PY

timers_stopped=0
restore_timers() {
  status=$?
  if (( timers_stopped == 1 )); then
    for timer in "${RECOVERY_TIMERS[@]}"; do
      systemctl start "$timer" >/dev/null 2>&1 || true
    done
  fi
  exit "$status"
}
trap restore_timers EXIT INT TERM HUP

printf '%s\n' \
  'Будет обновлена только Tuya Local: 2026.5.4 -> 2026.7.2.' \
  'Home Assistant будет планово перезапущен один раз; допустимый простой — до 10 минут.'
for timer in "${RECOVERY_TIMERS[@]}"; do
  systemctl stop "$timer"
done
timers_stopped=1
for service in home-butler-recovery.service home-butler-core-recovery.service home-butler-out-of-band-recovery.service; do
  [[ "$(systemctl is-active "$service" 2>/dev/null || true)" != "active" ]] \
    || fail "Recovery worker is still active: $service"
done

HOME_BUTLER_PLANNED_MAINTENANCE=1 python3 "$UPGRADE_SCRIPT"
postcheck_passed=0
for _attempt in $(seq 1 24); do
  systemctl start home-butler-inventory.service
  if python3 - "$INVENTORY" <<'PY'
import json, sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="ascii"))
capabilities = data.get("integration_capabilities")
tuya = capabilities.get("tuya_local") if isinstance(capabilities, dict) else None
if not isinstance(tuya, dict):
    raise SystemExit(2)
if tuya.get("core_version") != "2026.7.4" or tuya.get("installed_version") != "2026.7.2":
    raise SystemExit(2)
if tuya.get("automatic_ip_recovery") is not True:
    raise SystemExit(2)
if tuya.get("upgrade_status") != "automatic_ip_recovery_available":
    raise SystemExit(2)
failed = [
    item for item in data.get("config_entries", [])
    if item.get("domain") == "tuya_local" and item.get("state") == "setup_error"
]
if failed:
    raise SystemExit(2)
PY
  then
    postcheck_passed=1
    break
  fi
  sleep 5
done
(( postcheck_passed == 1 )) || fail 'Tuya Local post-maintenance inventory did not converge.'

post_recovery_result=$(timeout 15 ssh -F /dev/null -i "$RECOVERY_KEY" \
  -o BatchMode=yes -o IdentitiesOnly=yes -o ClearAllForwardings=yes \
  -o RequestTTY=no -o StrictHostKeyChecking=yes \
  -o "UserKnownHostsFile=$KNOWN_HOSTS" -o GlobalKnownHostsFile=/dev/null \
  -o "HostKeyAlias=$HOST_ALIAS" -o UpdateHostKeys=no -o VerifyHostKeyDNS=no \
  -o ConnectTimeout=5 -o ConnectionAttempts=1 -o ControlMaster=no \
  -o ControlPath=none -o LogLevel=ERROR \
  "homebutler-recovery@$REMOTE_HOST" status)
[[ "$post_recovery_result" == 'status=healthy_no_action' ]] \
  || fail 'Post-maintenance independent recovery proof is not healthy.'

printf '%s\n' 'Tuya Local 2026.7.2 установлена; автоматическое восстановление IP подтверждено.'
