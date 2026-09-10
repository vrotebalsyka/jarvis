#!/usr/bin/env bash
# One-off owner-authorized operational pause, not a production feature.
set -euo pipefail
units=(
 home-butler-alice-finalize.path home-butler-alice-rotation-finalize.path
 home-butler-alice-health.timer home-butler-inventory.timer
 home-butler-alice-finalize.service home-butler-alice-health.service
 home-butler-alice-rotation-finalize.service home-butler-alice-skill.service
 home-butler-alice-tunnel.service home-butler-inventory.service
 home-butler-local-chat.service ollama.service
)
backup=/var/lib/home-butler/owner-pause-20260910/units
for unit in "${units[@]}"; do
 test -f "/etc/systemd/system/$unit"
 test ! -L "/etc/systemd/system/$unit"
 test ! -e "$backup/$unit"
done
systemctl disable --now "${units[@]}"
install -d -m 0700 "$backup"
for unit in "${units[@]}"; do
 mv -- "/etc/systemd/system/$unit" "$backup/$unit"
done
systemctl mask "${units[@]}"
systemctl daemon-reload
systemctl show "${units[@]}" -p Id -p ActiveState -p SubState -p UnitFileState
