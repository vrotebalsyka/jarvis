#!/usr/bin/env bash
{ set +x; } 2>/dev/null
set -euo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C.UTF-8
umask 077

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
readonly SECRETS_DIR="$PROJECT_DIR/secrets"
readonly TOKEN_FILE="$SECRETS_DIR/home-assistant.token"

token=''
confirmed_token=''
risk_acceptance=''
temporary_file=''
cleanup() {
  token=''
  confirmed_token=''
  risk_acceptance=''
  if [[ -n "$temporary_file" && "$temporary_file" == "$SECRETS_DIR"/.home-assistant.token.* ]]; then
    rm -f -- "$temporary_file"
  fi
}
trap cleanup EXIT HUP INT TERM

if [[ ! -t 0 || ! -t 2 ]]; then
  printf '%s\n' 'Home Assistant secret setup requires an interactive terminal.' >&2
  exit 2
fi
if [[ -L "$SECRETS_DIR" ]]; then
  printf '%s\n' 'Home Assistant secret setup failed.' >&2
  exit 2
fi
if [[ ! -d "$SECRETS_DIR" ]]; then
  mkdir -m 700 -- "$SECRETS_DIR"
fi
chmod 700 -- "$SECRETS_DIR"
if [[ -e "$TOKEN_FILE" || -L "$TOKEN_FILE" ]]; then
  printf '%s\n' 'Home Assistant secret is already configured; refusing overwrite.' >&2
  exit 2
fi

printf '%s\n' \
  'Warning: local HTTP has no TLS; a hostile LAN device could steal this HA token.' >&2
printf '%s\n' \
  'Use a token from a dedicated non-admin Home Assistant user and rotate it if exposed.' >&2
IFS= read -r -p 'Type ACCEPT LOCAL HTTP RISK to continue: ' risk_acceptance
if [[ "$risk_acceptance" != 'ACCEPT LOCAL HTTP RISK' ]]; then
  printf '%s\n' 'Home Assistant secret setup cancelled.' >&2
  exit 2
fi
IFS= read -r -s -p 'Home Assistant token: ' token
printf '\n' >&2
IFS= read -r -s -p 'Repeat token: ' confirmed_token
printf '\n' >&2
if [[ "$token" != "$confirmed_token" \
  || ${#token} -lt 20 \
  || ${#token} -gt 4096 \
  || ! "$token" =~ ^[A-Za-z0-9._~-]+$ ]]; then
  printf '%s\n' 'Home Assistant secret setup failed.' >&2
  exit 2
fi

temporary_file="$(mktemp --tmpdir="$SECRETS_DIR" '.home-assistant.token.XXXXXX')"
printf '%s' "$token" >"$temporary_file"
chmod 600 -- "$temporary_file"
if ! ln -T -- "$temporary_file" "$TOKEN_FILE"; then
  printf '%s\n' 'Home Assistant secret setup failed; target already exists.' >&2
  exit 2
fi
rm -f -- "$temporary_file"
temporary_file=''
printf '%s\n' 'Home Assistant secret configured.'
