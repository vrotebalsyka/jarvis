#!/usr/bin/env bash
# Install or repair the public-key-only recovery identity on the Ubuntu HA host.
# This script validates Docker/HA and never restarts Home Assistant.
set -euo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C
umask 077

readonly ACCOUNT="homebutler-recovery"
readonly ACCOUNT_HOME="/var/lib/homebutler-recovery"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly HOST_COMMAND_SOURCE="$SCRIPT_DIR/ha-recovery-host-command.sh"
readonly GATE_SOURCE="$SCRIPT_DIR/ha-recovery-ssh-gate.sh"
readonly PREFLIGHT_SOURCE="$SCRIPT_DIR/ha-container-upgrade-preflight.py"
readonly PREFLIGHT_OUTPUT="$SCRIPT_DIR/ha-host-upgrade-preflight.json"
readonly HOST_COMMAND_TARGET="/usr/local/sbin/homebutler-recover-root"
readonly GATE_TARGET="/usr/local/libexec/homebutler-recovery-gate"
readonly AUTHORIZED_KEYS_DIR="/etc/ssh/authorized_keys"
readonly AUTHORIZED_KEYS_FILE="$AUTHORIZED_KEYS_DIR/$ACCOUNT"
readonly SSHD_DROPIN="/etc/ssh/sshd_config.d/60-homebutler-recovery.conf"
readonly SUDOERS_FILE="/etc/sudoers.d/homebutler-recovery"
readonly STATE_DIR="/var/lib/homebutler-recovery-state"
readonly PUBLIC_KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJF7UF+6d3HwDyWKdM9MZSAU1djT7qDpv5mnFrRIPujf home-butler-recovery-2026-08-03'
readonly KEY_OPTIONS='restrict,from="192.168.1.0/24",command="/usr/local/libexec/homebutler-recovery-gate"'
readonly MODE="${1:-install}"

fail() {
  printf '%s\n' "$1" >&2
  exit 2
}

(( $# <= 1 )) || fail 'Usage: bootstrap-ha-recovery-host.sh [--repair]'
[[ "$MODE" == "install" || "$MODE" == "--repair" ]] \
  || fail 'Usage: bootstrap-ha-recovery-host.sh [--repair]'
(( EUID == 0 )) || fail 'Run as root.'
grep -Fqx 'ID=ubuntu' /etc/os-release || fail 'This target is not Ubuntu.'
for command in chage cmp curl docker flock getent sshd visudo useradd userdel usermod systemctl; do
  command -v "$command" >/dev/null || fail "Missing required command: $command"
done
for source in "$HOST_COMMAND_SOURCE" "$GATE_SOURCE" "$PREFLIGHT_SOURCE"; do
  [[ -f "$source" && ! -L "$source" ]] || fail "Unsafe or missing source: $source"
done
[[ ! -e "$PREFLIGHT_OUTPUT" && ! -L "$PREFLIGHT_OUTPUT" ]] \
  || fail 'Upgrade preflight output already exists.'

mapfile -t candidates < <(
  docker container ls --all --quiet --no-trunc --filter label=io.hass.type=core
)
(( ${#candidates[@]} == 1 )) \
  || fail 'Expected exactly one Docker container labelled io.hass.type=core.'
candidate="${candidates[0]}"
[[ "$candidate" =~ ^[a-f0-9]{64}$ ]] || fail 'Invalid Home Assistant container ID.'
image=$(docker inspect --format '{{.Config.Image}}' "$candidate")
case "$image" in
  ghcr.io/home-assistant/home-assistant:*|homeassistant/home-assistant:*) ;;
  *) fail 'The labelled container is not a supported Home Assistant image.' ;;
esac
config_mount=$(
  docker inspect --format \
    '{{range .Mounts}}{{if eq .Destination "/config"}}present{{end}}{{end}}' \
    "$candidate"
)
[[ "$config_mount" == "present" ]] || fail 'Home Assistant /config mount not found.'

created=0
temporary=""
cleanup() {
  status=$?
  if [[ "$temporary" =~ ^/tmp/homebutler-recovery\.[A-Za-z0-9]{8}$ ]]; then
    rm -f -- "$temporary"
  fi
  if (( status != 0 && created == 1 )); then
    rm -f -- "$SSHD_DROPIN" "$SUDOERS_FILE" "$AUTHORIZED_KEYS_FILE" \
      "$GATE_TARGET" "$HOST_COMMAND_TARGET"
    rm -rf -- "$STATE_DIR"
    userdel -r "$ACCOUNT" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM HUP

validate_existing_identity() {
  passwd_record=$(getent passwd "$ACCOUNT") \
    || fail 'Recovery account is missing; refusing repair.'
  IFS=: read -r account_name _ account_uid _ _ account_home account_shell \
    <<<"$passwd_record"
  [[ "$account_name" == "$ACCOUNT" && "$account_uid" =~ ^[0-9]+$ \
    && "$account_uid" -gt 0 && "$account_uid" -lt 1000 \
    && "$account_home" == "$ACCOUNT_HOME" && "$account_shell" == "/bin/sh" ]] \
    || fail 'Recovery account identity changed; refusing repair.'
  mapfile -t account_groups < <(id -Gn "$ACCOUNT" | tr ' ' '\n')
  (( ${#account_groups[@]} == 1 )) \
    || fail 'Recovery account has supplementary groups; refusing repair.'
  for file in \
    "$HOST_COMMAND_TARGET" "$GATE_TARGET" "$AUTHORIZED_KEYS_FILE" \
    "$SSHD_DROPIN" "$SUDOERS_FILE"; do
    [[ -f "$file" && ! -L "$file" ]] \
      || fail "Installed recovery target is unsafe: $file"
  done
  [[ -d "$STATE_DIR" && ! -L "$STATE_DIR" ]] \
    || fail 'Installed recovery state directory is unsafe.'
}

install_exact_configuration() {
  # A literal '*' is not a valid password hash but, unlike a leading '!', does
  # not mark the Unix account as locked. OpenSSH still enforces public key only.
  usermod --password '*' "$ACCOUNT"
  chage --expiredate -1 --inactive -1 --mindays 0 --maxdays 99999 --warndays 7 \
    "$ACCOUNT"

  install -d -o root -g root -m 0755 -- /usr/local/libexec "$AUTHORIZED_KEYS_DIR"
  install -d -o root -g root -m 0700 -- "$STATE_DIR"
  install -o root -g root -m 0755 -- "$HOST_COMMAND_SOURCE" "$HOST_COMMAND_TARGET"
  install -o root -g root -m 0755 -- "$GATE_SOURCE" "$GATE_TARGET"

  temporary=$(mktemp /tmp/homebutler-recovery.XXXXXXXX)
  [[ "$temporary" =~ ^/tmp/homebutler-recovery\.[A-Za-z0-9]{8}$ ]] \
    || fail 'Temporary file validation failed.'
  printf '%s %s\n' "$KEY_OPTIONS" "$PUBLIC_KEY" >"$temporary"
  # OpenSSH opens AuthorizedKeysFile while temporarily running as the target
  # uid. The key is public and root remains its only writer, so 0644 is both
  # required for this /etc path and safe under StrictModes.
  install -o root -g root -m 0644 -- "$temporary" "$AUTHORIZED_KEYS_FILE"

  printf '%s\n' \
    'homebutler-recovery ALL=(root) NOPASSWD:NOSETENV: /usr/local/sbin/homebutler-recover-root ""' \
    >"$temporary"
  visudo -cf "$temporary" >/dev/null
  install -o root -g root -m 0440 -- "$temporary" "$SUDOERS_FILE"

  printf '%s\n' \
    'Match User homebutler-recovery' \
    '    AuthenticationMethods publickey' \
    '    PubkeyAuthentication yes' \
    '    PasswordAuthentication no' \
    '    KbdInteractiveAuthentication no' \
    '    AuthorizedKeysFile /etc/ssh/authorized_keys/homebutler-recovery' \
    '    ForceCommand /usr/local/libexec/homebutler-recovery-gate' \
    '    DisableForwarding yes' \
    '    PermitTTY no' \
    '    PermitTunnel no' \
    '    PermitUserRC no' \
    'Match all' >"$temporary"
  install -o root -g root -m 0644 -- "$temporary" "$SSHD_DROPIN"
  rm -f -- "$temporary"
  temporary=""

  shadow_record=$(getent shadow "$ACCOUNT") \
    || fail 'Recovery shadow entry is missing.'
  IFS=: read -r _ shadow_password _ <<<"$shadow_record"
  [[ "$shadow_password" == '*' ]] \
    || fail 'Recovery password sentinel is invalid.'
  [[ "$(stat -c '%u:%a' -- "$AUTHORIZED_KEYS_FILE")" == '0:644' ]] \
    || fail 'Recovery authorized_keys ownership or mode is invalid.'
  cmp -s -- "$HOST_COMMAND_SOURCE" "$HOST_COMMAND_TARGET" \
    || fail 'Installed recovery command differs from reviewed source.'
  cmp -s -- "$GATE_SOURCE" "$GATE_TARGET" \
    || fail 'Installed recovery gate differs from reviewed source.'
  visudo -cf "$SUDOERS_FILE" >/dev/null
  sshd -t

  effective=$(sshd -T -C user="$ACCOUNT",addr=192.168.1.175,host=homebutler-recovery-target)
  for expected in \
    'authenticationmethods publickey' \
    'pubkeyauthentication yes' \
    'passwordauthentication no' \
    'kbdinteractiveauthentication no' \
    'authorizedkeysfile /etc/ssh/authorized_keys/homebutler-recovery' \
    'forcecommand /usr/local/libexec/homebutler-recovery-gate' \
    'disableforwarding yes' \
    'permittty no' \
    'permittunnel no'; do
    grep -Fqx -- "$expected" <<<"$effective" \
      || fail 'Effective SSH recovery policy is unsafe.'
  done
  systemctl reload ssh.service
}

if [[ "$MODE" == "--repair" ]]; then
  validate_existing_identity
else
  id "$ACCOUNT" >/dev/null 2>&1 \
    && fail 'Recovery account already exists; use --repair.'
  for target in \
    "$HOST_COMMAND_TARGET" "$GATE_TARGET" "$AUTHORIZED_KEYS_FILE" \
    "$SSHD_DROPIN" "$SUDOERS_FILE" "$STATE_DIR"; do
    [[ ! -e "$target" && ! -L "$target" ]] \
      || fail "Target already exists: $target"
  done
  useradd --system --user-group --create-home --home-dir "$ACCOUNT_HOME" \
    --shell /bin/sh "$ACCOUNT"
  created=1
fi

install_exact_configuration
temporary=$(mktemp /tmp/homebutler-recovery.XXXXXXXX)
[[ "$temporary" =~ ^/tmp/homebutler-recovery\.[A-Za-z0-9]{8}$ ]] \
  || fail 'Temporary file validation failed.'
/usr/bin/python3 "$PREFLIGHT_SOURCE" >"$temporary"
/usr/bin/python3 -m json.tool "$temporary" >/dev/null
install -o root -g root -m 0644 -- "$temporary" "$PREFLIGHT_OUTPUT"
rm -f -- "$temporary"
temporary=""

trap - EXIT INT TERM HUP
if [[ "$MODE" == "--repair" ]]; then
  printf '%s\n' 'Home Butler recovery identity repaired.'
else
  printf '%s\n' 'Home Butler recovery identity installed.'
fi
printf '%s\n' \
  'Password authentication is disabled; the account has no usable password.' \
  'No Home Assistant restart was performed.' \
  'Read-only upgrade preflight completed.' \
  'Create /run/homebutler-ha-maintenance.lock before planned maintenance.'
