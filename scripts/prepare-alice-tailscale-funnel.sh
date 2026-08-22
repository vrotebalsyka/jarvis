#!/usr/bin/env bash
# Interactive one-time migration from ngrok to a persistent Tailscale Funnel.
set -euo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C.UTF-8
umask 077

readonly PROJECT_DIR="/root/Jarvis/home-butler"
readonly FUNNEL_HELPER="$PROJECT_DIR/scripts/alice_tailscale_funnel.py"
readonly ROTATION_HELPER="$PROJECT_DIR/scripts/rotate-alice-webhook.py"
readonly CLIPBOARD_HELPER="$PROJECT_DIR/scripts/copy-alice-webhook-url.sh"
readonly ORIGIN_FILE="$PROJECT_DIR/secrets/alice-public-origin.txt"
readonly TARGET="http://127.0.0.1:8765"

fail() {
  printf '%s\n' "$1" >&2
  exit 2
}

(( EUID == 0 )) || fail 'Run this setup as root inside the Ubuntu WSL terminal.'
for path in "$FUNNEL_HELPER" "$ROTATION_HELPER" "$CLIPBOARD_HELPER"; do
  [[ -f "$path" && ! -L "$path" && "$(stat -c '%u:%g' -- "$path")" == "0:0" ]] \
    || fail 'A required root-owned helper is missing or unsafe.'
done
[[ -x /usr/bin/tailscale ]] || fail 'Tailscale is not installed.'
systemctl is-active --quiet tailscaled.service \
  || fail 'The Tailscale service is not active.'
systemctl is-active --quiet home-butler-alice-skill.service \
  || fail 'The local Alice gateway is not active.'
[[ "$(python3 "$ROTATION_HELPER" --status)" == 'alice_webhook_rotation=idle' ]] \
  || fail 'Finish or abort the existing Webhook rotation first.'

python3 "$FUNNEL_HELPER" --origin >/dev/null \
  || fail 'Authenticate Tailscale with: sudo tailscale up'

printf '%s\n' \
  'Tailscale may open a browser once to approve Funnel for this device.' \
  'No Home Assistant token or Alice secret is sent to Tailscale.'
/usr/bin/tailscale funnel --bg --yes "$TARGET"
python3 "$FUNNEL_HELPER" --ensure
python3 "$FUNNEL_HELPER" --write-origin "$ORIGIN_FILE"
python3 "$ROTATION_HELPER" --stage

systemctl reset-failed home-butler-alice-tunnel.service || true
systemctl enable --now home-butler-alice-tunnel.service
systemctl is-active --quiet home-butler-alice-tunnel.service \
  || fail 'The persistent Alice Funnel unit did not become active.'

bash "$CLIPBOARD_HELPER"
printf '%s\n' \
  'Tailscale Funnel is ready and will resume after WSL restarts.' \
  'Paste the clipboard value into the Yandex Dialogs Webhook field once.' \
  'The old secret remains accepted until the first authenticated request uses the new URL.'
