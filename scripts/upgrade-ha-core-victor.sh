#!/usr/bin/env bash
# Interactive owner launcher for the fixed HA Core 2026.7.4 maintenance action.
set -euo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C
umask 077

readonly PROJECT_DIR="/root/Jarvis/home-butler"
readonly REMOTE_TARGET="victor@192.168.1.127"
readonly REMOTE_HOST="192.168.1.127"
readonly HOST_ALIAS="homebutler-recovery-target"
readonly KNOWN_HOSTS="$PROJECT_DIR/config/ha-recovery-known_hosts"
readonly UPGRADE_SCRIPT="$PROJECT_DIR/scripts/ha-core-upgrade-host.sh"
readonly PREFLIGHT_SCRIPT="$PROJECT_DIR/scripts/ha-container-upgrade-preflight.py"
readonly RECOVERY_KEY="$PROJECT_DIR/secrets/ha-recovery-ed25519"
readonly INVENTORY="/home/homebutler/.local/state/home-butler/incidents/inventory.json"
MODE="${1:-upgrade}"

fail() {
  printf '%s\n' "$1" >&2
  exit 2
}

(( EUID == 0 )) || fail 'Run as root inside Ubuntu WSL.'
(( $# <= 1 )) || fail 'Usage: upgrade-ha-core-victor.sh [--check|--verify|--rollback]'
[[ "$MODE" == "upgrade" || "$MODE" == "--check" || "$MODE" == "--verify" \
  || "$MODE" == "--rollback" ]] \
  || fail 'Usage: upgrade-ha-core-victor.sh [--check|--verify|--rollback]'
for path in "$KNOWN_HOSTS" "$UPGRADE_SCRIPT" "$PREFLIGHT_SCRIPT" "$RECOVERY_KEY"; do
  [[ -f "$path" && ! -L "$path" ]] || fail "Unsafe or missing local file: $path"
  (( (8#$(stat -c '%a' -- "$path") & 8#022) == 0 )) \
    || fail "Local file is writable by group or others: $path"
done
bash -n "$UPGRADE_SCRIPT"
if [[ "$MODE" == "--check" ]]; then
  printf '%s\n' 'HA_CORE_UPGRADE_LAUNCHER_CHECK_OK'
  exit 0
fi

[[ "$(systemctl is-enabled home-butler-out-of-band-recovery.timer 2>/dev/null)" == "enabled" \
  && "$(systemctl is-active home-butler-out-of-band-recovery.timer 2>/dev/null)" == "active" ]] \
  || fail 'Independent recovery timer is not active.'
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
if [[ "$MODE" == "upgrade" ]]; then
  current_core=$(python3 - "$INVENTORY" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="ascii"))
capabilities = data.get("integration_capabilities")
tuya = capabilities.get("tuya_local") if isinstance(capabilities, dict) else None
print(tuya.get("core_version", "unknown") if isinstance(tuya, dict) else "unknown")
PY
)
  [[ "$current_core" != "2026.7.4" ]] || MODE="--verify"
fi

if [[ "$MODE" == "--verify" ]]; then
  snapshot_file=$(mktemp /tmp/homebutler-ha-core-verify.XXXXXXXX)
  trap 'rm -f -- "$snapshot_file"' EXIT INT TERM HUP
  python3 "$PROJECT_DIR/scripts/home_assistant_read.py" snapshot >"$snapshot_file"
  python3 - "$snapshot_file" "$INVENTORY" <<'PY'
import json, sys
from pathlib import Path
snapshot = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
inventory = json.loads(Path(sys.argv[2]).read_text(encoding="ascii"))
if snapshot.get("status") not in {"healthy", "stale_data"}:
    raise SystemExit(2)
if not isinstance(snapshot.get("entity_count"), int) or snapshot["entity_count"] <= 0:
    raise SystemExit(2)
capabilities = inventory.get("integration_capabilities")
tuya = capabilities.get("tuya_local") if isinstance(capabilities, dict) else None
if not isinstance(tuya, dict) or tuya.get("core_version") != "2026.7.4":
    raise SystemExit(2)
PY
  printf '%s\n' 'Проверено: Home Assistant Core уже работает на версии 2026.7.4.'
  exit 0
fi

if [[ "$MODE" == "upgrade" ]]; then
  python3 - "$INVENTORY" <<'PY'
import json, os, stat, sys, time
from pathlib import Path

path = Path(sys.argv[1])
metadata = path.lstat()
if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 8 * 1024 * 1024:
    raise SystemExit(2)
if time.time() - metadata.st_mtime > 900:
    raise SystemExit(2)
data = json.loads(path.read_text(encoding="ascii"))
backup = data.get("backup_readiness")
capabilities = data.get("integration_capabilities")
tuya = capabilities.get("tuya_local") if isinstance(capabilities, dict) else None
if not isinstance(backup, dict) or backup.get("status") != "recent_complete_backup":
    raise SystemExit(2)
if backup.get("core_version") != "2026.5.2" or not isinstance(backup.get("age_seconds"), int):
    raise SystemExit(2)
if not 0 <= backup["age_seconds"] <= 3600 or not isinstance(backup.get("restore_tested"), bool):
    raise SystemExit(2)
if not isinstance(tuya, dict) or tuya.get("installed_version") != "2026.5.4":
    raise SystemExit(2)
if tuya.get("reviewed_target_version") != "2026.7.2" or tuya.get("upgrade_status") != "core_upgrade_required":
    raise SystemExit(2)
PY
fi

readonly CONTROL_DIR="$(mktemp -d /tmp/homebutler-ha-upgrade-control.XXXXXXXX)"
readonly CONTROL_SOCKET="$CONTROL_DIR/ssh.sock"
[[ "$CONTROL_DIR" =~ ^/tmp/homebutler-ha-upgrade-control\.[A-Za-z0-9]{8}$ ]] \
  || fail 'Local control directory validation failed.'
remote_dir=""
master_started=0
ssh_base=(
  -F /dev/null -o ClearAllForwardings=yes -o RequestTTY=no
  -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$KNOWN_HOSTS"
  -o GlobalKnownHostsFile=/dev/null -o "HostKeyAlias=$HOST_ALIAS"
  -o UpdateHostKeys=no -o VerifyHostKeyDNS=no -o ConnectTimeout=10
  -o ConnectionAttempts=1 -o LogLevel=ERROR
)
cleanup() {
  status=$?
  if (( master_started == 1 )); then
    if [[ "$remote_dir" =~ ^/tmp/homebutler-ha-upgrade\.[A-Za-z0-9]{8}$ ]]; then
      ssh "${ssh_base[@]}" -S "$CONTROL_SOCKET" "$REMOTE_TARGET" \
        "/bin/rm -rf -- '$remote_dir'" >/dev/null 2>&1 || true
    fi
    ssh "${ssh_base[@]}" -S "$CONTROL_SOCKET" -O exit "$REMOTE_TARGET" \
      >/dev/null 2>&1 || true
  fi
  if [[ "$CONTROL_DIR" =~ ^/tmp/homebutler-ha-upgrade-control\.[A-Za-z0-9]{8}$ ]]; then
    rm -rf -- "$CONTROL_DIR"
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM HUP

printf '%s\n' \
  'OpenSSH запросит пароль victor. Пароль не отображается и не сохраняется.' \
  'После sudo Home Assistant будет планово перезапущен один раз; допустимый простой — до 10 минут.'
ssh "${ssh_base[@]}" -M -S "$CONTROL_SOCKET" -o ControlPersist=900 -Nf "$REMOTE_TARGET"
master_started=1
remote_dir=$(ssh "${ssh_base[@]}" -S "$CONTROL_SOCKET" "$REMOTE_TARGET" \
  "/usr/bin/env sh -c 'umask 077; /usr/bin/mktemp -d /tmp/homebutler-ha-upgrade.XXXXXXXX'")
[[ "$remote_dir" =~ ^/tmp/homebutler-ha-upgrade\.[A-Za-z0-9]{8}$ ]] \
  || fail 'Remote maintenance directory validation failed.'
tar -C "$PROJECT_DIR/scripts" -cf - -- \
  ha-core-upgrade-host.sh ha-container-upgrade-preflight.py \
  | ssh "${ssh_base[@]}" -S "$CONTROL_SOCKET" "$REMOTE_TARGET" \
      "/usr/bin/tar -C '$remote_dir' -xf -"
for name in ha-core-upgrade-host.sh ha-container-upgrade-preflight.py; do
  local_hash=$(sha256sum -- "$PROJECT_DIR/scripts/$name" | cut -d' ' -f1)
  remote_hash=$(ssh "${ssh_base[@]}" -S "$CONTROL_SOCKET" "$REMOTE_TARGET" \
    "/usr/bin/sha256sum -- '$remote_dir/$name'" | cut -d' ' -f1)
  [[ "$remote_hash" == "$local_hash" ]] || fail "Remote hash mismatch: $name"
done

host_mode=''
[[ "$MODE" == "--rollback" ]] && host_mode='--rollback'
result_file="$CONTROL_DIR/result"
ssh "${ssh_base[@]}" -S "$CONTROL_SOCKET" -tt "$REMOTE_TARGET" \
  "sudo -- /bin/bash '$remote_dir/ha-core-upgrade-host.sh' $host_mode" \
  | tr -d '\r' | tee "$result_file"
if [[ "$MODE" == "upgrade" ]]; then
  grep -Fqx 'status=upgrade_completed core_from=2026.5.2 core_to=2026.7.4' \
    "$result_file" || fail 'HA Core upgrade did not return the exact success proof.'
else
  grep -Fqx 'status=rollback_completed core=2026.5.2' "$result_file" \
    || fail 'HA Core rollback did not return the exact success proof.'
fi

snapshot_file="$CONTROL_DIR/snapshot.json"
python3 "$PROJECT_DIR/scripts/home_assistant_read.py" snapshot >"$snapshot_file"
python3 - "$snapshot_file" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if data.get("status") not in {"healthy", "stale_data"}:
    raise SystemExit(2)
if not isinstance(data.get("entity_count"), int) or data["entity_count"] <= 0:
    raise SystemExit(2)
PY
systemctl start home-butler-inventory.service
python3 - "$INVENTORY" "$MODE" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="ascii"))
mode = sys.argv[2]
capabilities = data.get("integration_capabilities")
tuya = capabilities.get("tuya_local") if isinstance(capabilities, dict) else None
if not isinstance(tuya, dict) or tuya.get("installed_version") != "2026.5.4":
    raise SystemExit(2)
expected_core = "2026.7.4" if mode == "upgrade" else "2026.5.2"
expected_status = "backup_required_before_update" if mode == "upgrade" else "core_upgrade_required"
if tuya.get("core_version") != expected_core or tuya.get("upgrade_status") != expected_status:
    raise SystemExit(2)
PY
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

printf '%s\n' 'Плановая операция завершена с проверенным результатом.'
