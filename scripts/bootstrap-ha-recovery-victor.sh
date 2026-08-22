#!/usr/bin/env bash
# Interactive one-time launcher for victor@192.168.1.127.
# Passwords are handled only by OpenSSH/sudo and are never read or stored here.
set -euo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C
umask 077

readonly PROJECT_DIR="/root/Jarvis/home-butler"
readonly REMOTE_USER="victor"
readonly REMOTE_HOST="192.168.1.127"
readonly REMOTE_TARGET="$REMOTE_USER@$REMOTE_HOST"
readonly HOST_ALIAS="homebutler-recovery-target"
readonly KNOWN_HOSTS="$PROJECT_DIR/config/ha-recovery-known_hosts"
readonly LOCAL_SCRIPT_DIR="$PROJECT_DIR/scripts"
readonly REMOTE_PREFIX="/tmp/homebutler-recovery-bootstrap."
readonly FILES=(
  bootstrap-ha-recovery-host.sh
  ha-recovery-host-command.sh
  ha-recovery-ssh-gate.sh
  ha-container-upgrade-preflight.py
)
readonly PREFLIGHT_TARGET="$PROJECT_DIR/secrets/ha-host-upgrade-preflight.json"

fail() {
  printf '%s\n' "$1" >&2
  exit 2
}

(( EUID == 0 )) || fail 'Run this launcher as root inside the Ubuntu WSL terminal.'
[[ -f "$KNOWN_HOSTS" && ! -L "$KNOWN_HOSTS" ]] \
  || fail 'Pinned Home Assistant SSH host key is missing.'
[[ "$(stat -c '%u:%a' -- "$KNOWN_HOSTS")" == "0:644" ]] \
  || fail 'Pinned Home Assistant SSH host key is unsafe.'
for name in "${FILES[@]}"; do
  path="$LOCAL_SCRIPT_DIR/$name"
  [[ -f "$path" && ! -L "$path" ]] \
    || fail "Missing or unsafe bootstrap file: $name"
  [[ "$(stat -c '%u' -- "$path")" == "0" ]] \
    || fail "Bootstrap file is not root-owned: $name"
  (( (8#$(stat -c '%a' -- "$path") & 8#022) == 0 )) \
    || fail "Bootstrap file is writable by group or others: $name"
done

if (( $# == 1 )) && [[ "$1" == "--check" ]]; then
  printf '%s\n' 'BOOTSTRAP_LAUNCHER_CHECK_OK'
  exit 0
fi
(( $# == 0 )) || fail 'Usage: bootstrap-ha-recovery-victor.sh [--check]'

readonly CONTROL_DIR="$(mktemp -d /tmp/homebutler-bootstrap-control.XXXXXXXX)"
readonly CONTROL_SOCKET="$CONTROL_DIR/ssh.sock"
[[ "$CONTROL_DIR" =~ ^/tmp/homebutler-bootstrap-control\.[A-Za-z0-9]{8}$ ]] \
  || fail 'Local control directory validation failed.'
remote_dir=""
master_started=0

ssh_base=(
  -F /dev/null
  -o ClearAllForwardings=yes
  -o RequestTTY=no
  -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=$KNOWN_HOSTS"
  -o GlobalKnownHostsFile=/dev/null
  -o "HostKeyAlias=$HOST_ALIAS"
  -o UpdateHostKeys=no
  -o VerifyHostKeyDNS=no
  -o ConnectTimeout=10
  -o ConnectionAttempts=1
  -o LogLevel=ERROR
)

cleanup() {
  status=$?
  if (( master_started == 1 )); then
    if [[ "$remote_dir" =~ ^/tmp/homebutler-recovery-bootstrap\.[A-Za-z0-9]{8}$ ]]; then
      ssh "${ssh_base[@]}" -S "$CONTROL_SOCKET" "$REMOTE_TARGET" \
        "/bin/rm -rf -- '$remote_dir'" >/dev/null 2>&1 || true
    fi
    ssh "${ssh_base[@]}" -S "$CONTROL_SOCKET" -O exit \
      "$REMOTE_TARGET" >/dev/null 2>&1 || true
  fi
  if [[ "$CONTROL_DIR" =~ ^/tmp/homebutler-bootstrap-control\.[A-Za-z0-9]{8}$ ]]; then
    rm -rf -- "$CONTROL_DIR"
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM HUP

printf '%s\n' \
  'Сейчас OpenSSH попросит пароль пользователя victor на 192.168.1.127.' \
  'Пароль вводится вслепую: символы на экране не отображаются.'
ssh "${ssh_base[@]}" -M -S "$CONTROL_SOCKET" \
  -o ControlPersist=120 -Nf "$REMOTE_TARGET"
master_started=1

remote_dir=$(ssh "${ssh_base[@]}" -S "$CONTROL_SOCKET" "$REMOTE_TARGET" \
  "/usr/bin/env sh -c 'umask 077; /usr/bin/mktemp -d ${REMOTE_PREFIX}XXXXXXXX'")
[[ "$remote_dir" =~ ^/tmp/homebutler-recovery-bootstrap\.[A-Za-z0-9]{8}$ ]] \
  || fail 'Remote bootstrap directory validation failed.'

tar -C "$LOCAL_SCRIPT_DIR" -cf - -- "${FILES[@]}" \
  | ssh "${ssh_base[@]}" -S "$CONTROL_SOCKET" "$REMOTE_TARGET" \
      "/usr/bin/tar -C '$remote_dir' -xf -"

for name in "${FILES[@]}"; do
  local_hash=$(sha256sum -- "$LOCAL_SCRIPT_DIR/$name" | cut -d' ' -f1)
  remote_hash=$(ssh "${ssh_base[@]}" -S "$CONTROL_SOCKET" "$REMOTE_TARGET" \
    "/usr/bin/sha256sum -- '$remote_dir/$name'" | cut -d' ' -f1)
  [[ "$remote_hash" == "$local_hash" ]] \
    || fail "Remote bootstrap hash mismatch: $name"
done

printf '%s\n' \
  'Теперь sudo может ещё раз попросить пароль victor.' \
  'Установка проверит Docker/HA и не будет перезапускать Home Assistant.'
remote_identity=$(
  ssh "${ssh_base[@]}" -S "$CONTROL_SOCKET" "$REMOTE_TARGET" \
    "/usr/bin/id -u homebutler-recovery >/dev/null 2>&1 && /usr/bin/printf installed || /usr/bin/printf absent"
)
case "$remote_identity" in
  absent) host_mode='' ;;
  installed) host_mode='--repair' ;;
  *) fail 'Remote recovery identity state is invalid.' ;;
esac
ssh "${ssh_base[@]}" -S "$CONTROL_SOCKET" -tt "$REMOTE_TARGET" \
  "sudo -- /bin/bash '$remote_dir/bootstrap-ha-recovery-host.sh' $host_mode"

readonly LOCAL_PREFLIGHT="$CONTROL_DIR/ha-host-upgrade-preflight.json"
ssh "${ssh_base[@]}" -S "$CONTROL_SOCKET" "$REMOTE_TARGET" \
  "/bin/cat -- '$remote_dir/ha-host-upgrade-preflight.json'" >"$LOCAL_PREFLIGHT"
python3 - "$LOCAL_PREFLIGHT" <<'PY'
import json, re, sys
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="ascii"))
expected = {
    "schema_version", "container_identity_hash", "container_name", "image",
    "image_identity_hash", "network_mode", "restart_policy",
    "config_mount_type", "config_mount_source", "docker_server_version",
    "architecture", "config_free_bytes", "compose", "upgrade_method",
    "environment_exported", "read_only",
}
if set(data) != expected or data.get("schema_version") != 1:
    raise SystemExit(2)
if not re.fullmatch(r"[a-f0-9]{64}", data.get("container_identity_hash", "")):
    raise SystemExit(2)
if not re.fullmatch(r"[a-f0-9]{64}", data.get("image_identity_hash", "")):
    raise SystemExit(2)
if data.get("upgrade_method") not in {"docker_compose", "manual_recreate_required"}:
    raise SystemExit(2)
if data.get("environment_exported") is not False or data.get("read_only") is not True:
    raise SystemExit(2)
compose = data.get("compose")
if not isinstance(compose, dict) or set(compose) != {
    "detected", "available", "files_safe", "project", "service",
    "working_dir", "config_files",
}:
    raise SystemExit(2)
PY
[[ -d "$PROJECT_DIR/secrets" && ! -L "$PROJECT_DIR/secrets" \
  && "$(stat -c '%u:%a' -- "$PROJECT_DIR/secrets")" == "0:700" ]] \
  || fail 'Local secrets directory is unsafe.'
install -o root -g root -m 0600 -- "$LOCAL_PREFLIGHT" "$PREFLIGHT_TARGET"

recovery_ssh=(
  -F /dev/null
  -i "$PROJECT_DIR/secrets/ha-recovery-ed25519"
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o ClearAllForwardings=yes
  -o RequestTTY=no
  -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=$KNOWN_HOSTS"
  -o GlobalKnownHostsFile=/dev/null
  -o "HostKeyAlias=$HOST_ALIAS"
  -o UpdateHostKeys=no
  -o VerifyHostKeyDNS=no
  -o ConnectTimeout=10
  -o ConnectionAttempts=1
  -o ControlMaster=no
  -o ControlPath=none
  -o LogLevel=ERROR
)
recovery_result=$(ssh "${recovery_ssh[@]}" \
  "homebutler-recovery@$REMOTE_HOST" status)
[[ "$recovery_result" == 'status=healthy_no_action' ]] \
  || fail 'Recovery key proof did not return healthy_no_action.'

printf '%s\n' \
  'Recovery bootstrap completed. Passwords were not stored.' \
  'Read-only HA Container upgrade preflight stored privately.' \
  'Recovery key proof passed: healthy_no_action; Home Assistant was not restarted.'
