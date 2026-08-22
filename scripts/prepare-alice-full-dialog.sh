#!/usr/bin/env bash
set -euo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C.UTF-8

readonly PROJECT_DIR="/root/Jarvis/home-butler"
readonly RUNTIME_DIR="/opt/home-butler"
readonly SECRETS_DIR="$PROJECT_DIR/secrets"
readonly SERVICE_USER="homebutler"
readonly SERVICE_GROUP="homebutler"
readonly SERVICE_HOME="/home/homebutler"
readonly STATE_DIR="$SERVICE_HOME/.local/state/home-butler/alice"
readonly CLAIM_FILE="$STATE_DIR/claim.json"
readonly MODE_FILE="$STATE_DIR/mode"
readonly SECRET_FILE="$SECRETS_DIR/alice-skill-secret"
readonly NEXT_SECRET_FILE="$SECRETS_DIR/alice-skill-secret-next"
readonly SKILL_FILE="$SECRETS_DIR/alice-skill-id"
readonly OWNERS_FILE="$SECRETS_DIR/alice-owner-ids"
readonly URL_FILE="$SECRETS_DIR/alice-webhook-url.txt"
readonly NGROK_SOURCE="/root/.config/ngrok/ngrok.yml"
readonly NGROK_TARGET="$SERVICE_HOME/.config/ngrok/ngrok.yml"
readonly PUBLIC_ORIGIN="https://dancing-hull-numerous.ngrok-free.dev"
readonly PENDING_SKILL_ID="PENDING_PRIVATE_SKILL"

fail() {
  printf '%s\n' "$1" >&2
  exit 2
}

[[ "${EUID:-$(id -u)}" == 0 ]] || fail 'Run this setup as root.'
[[ $# == 1 && ( "$1" == "--prepare" || "$1" == "--finalize" ) ]] \
  || fail 'Usage: prepare-alice-full-dialog.sh --prepare|--finalize'
readonly MODE="$1"

for required in \
  "$PROJECT_DIR/scripts/alice_skill_gateway.py" \
  "$PROJECT_DIR/scripts/alice_skill_health.py" \
  "$PROJECT_DIR/scripts/alice_claim_finalizer.py" \
  "$PROJECT_DIR/scripts/rotate-alice-webhook.py" \
  "$PROJECT_DIR/scripts/owner_chat.py" \
  "$PROJECT_DIR/scripts/model_ha_proof.py" \
  "$PROJECT_DIR/config/systemd/home-butler-alice-skill.service" \
  "$PROJECT_DIR/config/systemd/home-butler-alice-tunnel.service" \
  "$PROJECT_DIR/config/systemd/home-butler-alice-health.service" \
  "$PROJECT_DIR/config/systemd/home-butler-alice-health.timer" \
  "$PROJECT_DIR/config/systemd/home-butler-alice-finalize.service" \
  "$PROJECT_DIR/config/systemd/home-butler-alice-finalize.path" \
  "$PROJECT_DIR/config/systemd/home-butler-alice-rotation-finalize.service" \
  "$PROJECT_DIR/config/systemd/home-butler-alice-rotation-finalize.path" \
  /usr/local/bin/ngrok; do
  [[ -f "$required" && ! -L "$required" ]] || fail "Missing or unsafe file: $required"
done
[[ -d "$RUNTIME_DIR/scripts" && ! -L "$RUNTIME_DIR/scripts" ]] \
  || fail 'The isolated Home Butler runtime is unavailable.'
[[ -f "$NGROK_SOURCE" && ! -L "$NGROK_SOURCE" \
  && "$(stat -c '%F:%u:%g:%a:%h' -- "$NGROK_SOURCE")" == \
    'regular file:0:0:600:1' ]] \
  || fail 'The existing ngrok credential file is missing or unsafe.'

readonly SERVICE_UID="$(id -u "$SERVICE_USER")"
readonly SERVICE_GID="$(id -g "$SERVICE_GROUP")"
[[ "$SERVICE_UID" =~ ^[0-9]+$ && "$SERVICE_UID" != 0 \
  && "$SERVICE_GID" =~ ^[0-9]+$ ]] \
  || fail 'The unprivileged Home Butler account is unavailable.'

install -d -o root -g root -m 0700 -- "$SECRETS_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0700 -- \
  "$SERVICE_HOME/.config" "$SERVICE_HOME/.config/ngrok" "$STATE_DIR"

write_private_root_file() {
  local target="$1" value="$2" temporary
  if [[ -e "$target" || -L "$target" ]]; then
    [[ -f "$target" && ! -L "$target" \
      && "$(stat -c '%F:%u:%g:%a:%h' -- "$target")" == \
        'regular file:0:0:600:1' ]] \
      || fail "Unsafe private target: $target"
  fi
  temporary="$(mktemp "$SECRETS_DIR/.alice-credential.XXXXXX")"
  chmod 0600 -- "$temporary"
  printf '%s\n' "$value" > "$temporary"
  chown root:root -- "$temporary"
  mv -fT -- "$temporary" "$target"
}

write_private_service_file() {
  local value="$1" temporary
  temporary="$(mktemp "$STATE_DIR/.alice-mode.XXXXXX")"
  chmod 0600 -- "$temporary"
  printf '%s\n' "$value" > "$temporary"
  chown "$SERVICE_USER:$SERVICE_GROUP" -- "$temporary"
  mv -fT -- "$temporary" "$MODE_FILE"
}

if [[ ! -e "$SECRET_FILE" && ! -L "$SECRET_FILE" ]]; then
  secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  [[ "$secret" =~ ^[A-Za-z0-9_-]{32,128}$ ]] \
    || fail 'Secure webhook secret generation failed.'
  write_private_root_file "$SECRET_FILE" "$secret"
fi
[[ -f "$SECRET_FILE" && ! -L "$SECRET_FILE" \
  && "$(stat -c '%F:%u:%g:%a:%h' -- "$SECRET_FILE")" == \
    'regular file:0:0:600:1' ]] \
  || fail 'The webhook secret is unsafe.'
secret="$(<"$SECRET_FILE")"
[[ "$secret" =~ ^[A-Za-z0-9_-]{32,128}$ ]] \
  || fail 'The webhook secret is malformed.'
if [[ ! -e "$NEXT_SECRET_FILE" && ! -L "$NEXT_SECRET_FILE" ]]; then
  write_private_root_file "$NEXT_SECRET_FILE" "$secret"
fi
[[ -f "$NEXT_SECRET_FILE" && ! -L "$NEXT_SECRET_FILE" \
  && "$(stat -c '%F:%u:%g:%a:%h' -- "$NEXT_SECRET_FILE")" == \
    'regular file:0:0:600:1' ]] \
  || fail 'The next webhook secret is unsafe.'
next_secret="$(<"$NEXT_SECRET_FILE")"
[[ "$next_secret" =~ ^[A-Za-z0-9_-]{32,128}$ ]] \
  || fail 'The next webhook secret is malformed.'

if [[ ! -e "$SKILL_FILE" && ! -L "$SKILL_FILE" ]]; then
  write_private_root_file "$SKILL_FILE" "$PENDING_SKILL_ID"
fi
if [[ ! -e "$OWNERS_FILE" && ! -L "$OWNERS_FILE" ]]; then
  write_private_root_file "$OWNERS_FILE" '-'
fi

if [[ "$MODE" == "--finalize" ]]; then
  [[ -f "$CLAIM_FILE" && ! -L "$CLAIM_FILE" \
    && "$(stat -c '%F:%u:%g:%a:%h' -- "$CLAIM_FILE")" == \
      "regular file:$SERVICE_UID:$SERVICE_GID:600:1" ]] \
    || fail 'No safe Yandex provisioning claim has arrived yet.'
  mapfile -t claim < <(
    python3 - "$CLAIM_FILE" <<'PY'
import json
import re
import sys

document = json.load(open(sys.argv[1], encoding="ascii"))
skill_id = document.get("skill_id")
user_id = document.get("user_id")
pattern = re.compile(r"[A-Za-z0-9._:-]{8,256}\Z")
if not isinstance(skill_id, str) or skill_id == "PENDING_PRIVATE_SKILL" or not pattern.fullmatch(skill_id):
    raise SystemExit(2)
if user_id is not None and (not isinstance(user_id, str) or not pattern.fullmatch(user_id)):
    raise SystemExit(2)
print(skill_id)
print(user_id or "-")
PY
  ) || fail 'The Yandex provisioning claim is malformed.'
  [[ "${#claim[@]}" == 2 ]] || fail 'The Yandex provisioning claim is incomplete.'
  write_private_root_file "$SKILL_FILE" "${claim[0]}"
  write_private_root_file "$OWNERS_FILE" "${claim[1]}"
fi

write_private_root_file "$URL_FILE" "$PUBLIC_ORIGIN/alice/$next_secret"
unset secret next_secret
if [[ "$MODE" == "--prepare" ]]; then
  write_private_service_file pending
else
  write_private_service_file pinned
fi

install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 -- \
  "$NGROK_SOURCE" "$NGROK_TARGET"
for source in \
  alice_skill_gateway.py alice_skill_health.py alice_claim_finalizer.py rotate-alice-webhook.py \
  owner_chat.py model_ha_proof.py; do
  install -o root -g root -m 0755 -- \
    "$PROJECT_DIR/scripts/$source" "$RUNTIME_DIR/scripts/$source"
done
for unit in \
  home-butler-alice-skill.service home-butler-alice-tunnel.service \
  home-butler-alice-health.service home-butler-alice-health.timer \
  home-butler-alice-finalize.service home-butler-alice-finalize.path \
  home-butler-alice-rotation-finalize.service \
  home-butler-alice-rotation-finalize.path; do
  install -o root -g root -m 0644 -- \
    "$PROJECT_DIR/config/systemd/$unit" "/etc/systemd/system/$unit"
done

systemd-analyze verify \
  /etc/systemd/system/home-butler-alice-skill.service \
  /etc/systemd/system/home-butler-alice-tunnel.service \
  /etc/systemd/system/home-butler-alice-health.service \
  /etc/systemd/system/home-butler-alice-health.timer \
  /etc/systemd/system/home-butler-alice-finalize.service \
  /etc/systemd/system/home-butler-alice-finalize.path \
  /etc/systemd/system/home-butler-alice-rotation-finalize.service \
  /etc/systemd/system/home-butler-alice-rotation-finalize.path
systemctl daemon-reload
systemctl enable --now home-butler-alice-skill.service
if [[ "$MODE" == "--finalize" ]]; then
  systemctl restart home-butler-alice-skill.service
fi
systemctl enable --now home-butler-alice-tunnel.service
systemctl enable --now home-butler-alice-health.timer
systemctl enable --now home-butler-alice-finalize.path
systemctl enable --now home-butler-alice-rotation-finalize.path

systemctl is-enabled --quiet \
  home-butler-alice-skill.service home-butler-alice-tunnel.service \
  home-butler-alice-health.timer \
  home-butler-alice-finalize.path home-butler-alice-rotation-finalize.path
systemctl is-active --quiet \
  home-butler-alice-skill.service home-butler-alice-tunnel.service \
  home-butler-alice-health.timer \
  home-butler-alice-finalize.path home-butler-alice-rotation-finalize.path

if [[ "$MODE" == "--prepare" ]]; then
  printf '%s\n' \
    'Alice provisioning endpoint is active.' \
    'The first valid private skill request will be pinned automatically.' \
    "The private webhook URL is stored in $URL_FILE"
else
  printf '%s\n' \
    'Alice full-dialog identity is pinned.' \
    'The GPU-backed gateway and HTTPS tunnel are active.'
fi
