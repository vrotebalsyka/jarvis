#!/usr/bin/env bash
# Managed by Home Butler. Install only on the Ubuntu host that runs HA Container.
set -euo pipefail

export PATH=/usr/bin:/bin
export LC_ALL=C
umask 077

readonly STATE_DIR="/var/lib/homebutler-recovery-state"
readonly LAST_ACTION="$STATE_DIR/last_action_epoch"
readonly LOCK_FILE="/run/lock/homebutler-ha-recovery.lock"
readonly MAINTENANCE_LOCK="/run/homebutler-ha-maintenance.lock"
readonly COOLDOWN_SECONDS=21600
readonly HEALTH_URL="http://127.0.0.1:8123/"

result() {
  /usr/bin/logger -t homebutler-ha-recovery -- "$1" || true
  printf 'status=%s\n' "$1"
  exit "${2:-0}"
}

exec 9>"$LOCK_FILE"
/usr/bin/flock -n 9 || result cooldown 3
[[ ! -e "$MAINTENANCE_LOCK" ]] || result maintenance 3

[[ -x /usr/bin/docker && -x /usr/bin/curl ]] || result docker_unavailable 6
/usr/bin/docker info >/dev/null 2>&1 || result docker_unavailable 6

mapfile -t container_ids < <(
  /usr/bin/docker container ls --all --quiet --no-trunc \
    --filter label=io.hass.type=core
)
(( ${#container_ids[@]} == 1 )) || result identity_invalid 5
readonly container_id="${container_ids[0]}"
[[ "$container_id" =~ ^[a-f0-9]{64}$ ]] || result identity_invalid 5

image=$(/usr/bin/docker inspect --format '{{.Config.Image}}' "$container_id") \
  || result identity_invalid 5
case "$image" in
  ghcr.io/home-assistant/home-assistant:*|homeassistant/home-assistant:*) ;;
  *) result identity_invalid 5 ;;
esac
config_mount=$(
  /usr/bin/docker inspect --format \
    '{{range .Mounts}}{{if eq .Destination "/config"}}present{{end}}{{end}}' \
    "$container_id"
) || result identity_invalid 5
[[ "$config_mount" == "present" ]] || result identity_invalid 5

for _attempt in 1 2 3; do
  if /usr/bin/curl --fail --silent --show-error --output /dev/null \
      --connect-timeout 3 --max-time 5 "$HEALTH_URL"; then
    result healthy_no_action 0
  fi
  /bin/sleep 10
done

now=$(/usr/bin/date +%s)
if [[ -f "$LAST_ACTION" && ! -L "$LAST_ACTION" ]]; then
  last=$(<"$LAST_ACTION")
  [[ "$last" =~ ^[0-9]{1,12}$ ]] || result identity_invalid 5
  (( now - last >= COOLDOWN_SECONDS )) || result cooldown 3
fi

before_started=$(
  /usr/bin/docker inspect --format '{{.State.StartedAt}}' "$container_id"
) || result identity_invalid 5
state=$(/usr/bin/docker inspect --format '{{.State.Status}}' "$container_id") \
  || result identity_invalid 5

temporary=$(/usr/bin/mktemp "$STATE_DIR/.last-action.XXXXXX")
printf '%s\n' "$now" >"$temporary"
/bin/chmod 0600 "$temporary"
/bin/mv -f -- "$temporary" "$LAST_ACTION"

case "$state" in
  exited|created)
    /usr/bin/docker container start "$container_id" >/dev/null \
      || result restart_failed 4
    ;;
  running|restarting)
    /usr/bin/docker container restart --timeout 240 "$container_id" >/dev/null \
      || result restart_failed 4
    ;;
  *) result restart_failed 4 ;;
esac

for _attempt in $(seq 1 60); do
  running=$(
    /usr/bin/docker inspect --format '{{.State.Running}}' "$container_id" 2>/dev/null
  ) || running=false
  after_started=$(
    /usr/bin/docker inspect --format '{{.State.StartedAt}}' "$container_id" 2>/dev/null
  ) || after_started=""
  if [[ "$running" == "true" && "$after_started" != "$before_started" ]] \
      && /usr/bin/curl --fail --silent --show-error --output /dev/null \
        --connect-timeout 3 --max-time 5 "$HEALTH_URL"; then
    result restarted_verified 0
  fi
  /bin/sleep 5
done

result restart_failed 4
