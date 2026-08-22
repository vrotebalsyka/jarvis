#!/usr/bin/env bash
# Fixed, fail-closed Home Assistant Core Compose upgrade with image rollback.
set -Eeuo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C
umask 077

readonly EXPECTED_CURRENT_VERSION="2026.5.2"
readonly TARGET_VERSION="2026.7.4"
readonly ROLLBACK_IMAGE="homebutler-ha-rollback:${EXPECTED_CURRENT_VERSION}"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly PREFLIGHT_SCRIPT="$SCRIPT_DIR/ha-container-upgrade-preflight.py"
readonly UPGRADE_LOCK="/run/lock/homebutler-ha-core-upgrade.lock"
readonly MAINTENANCE_LOCK="/run/homebutler-ha-maintenance.lock"
readonly MODE="${1:-upgrade}"
readonly MINIMUM_FREE_BYTES=8589934592

fail() {
  printf 'status=%s\n' "$1" >&2
  exit 2
}

(( $# <= 1 )) || fail invalid_arguments
[[ "$MODE" == "upgrade" || "$MODE" == "--rollback" ]] \
  || fail invalid_arguments
(( EUID == 0 )) || fail root_required
grep -Fqx 'ID=ubuntu' /etc/os-release || fail target_not_ubuntu
for command in curl docker flock python3 sha256sum; do
  command -v "$command" >/dev/null || fail missing_host_dependency
done
[[ -f "$PREFLIGHT_SCRIPT" && ! -L "$PREFLIGHT_SCRIPT" ]] \
  || fail preflight_script_invalid
[[ ! -e "$MAINTENANCE_LOCK" && ! -L "$MAINTENANCE_LOCK" ]] \
  || fail maintenance_already_active

exec 8>"$UPGRADE_LOCK"
flock -n 8 || fail upgrade_already_active
temporary=$(mktemp /tmp/homebutler-ha-upgrade.XXXXXXXX)
[[ "$temporary" =~ ^/tmp/homebutler-ha-upgrade\.[A-Za-z0-9]{8}$ ]] \
  || fail temporary_path_invalid
install -o root -g root -m 0600 /dev/null "$MAINTENANCE_LOCK"

cleanup() {
  status=$?
  if [[ "$temporary" =~ ^/tmp/homebutler-ha-upgrade\.[A-Za-z0-9]{8}$ ]]; then
    rm -f -- "$temporary"
  fi
  if [[ -f "$MAINTENANCE_LOCK" && ! -L "$MAINTENANCE_LOCK" \
    && "$(stat -c '%u:%a' -- "$MAINTENANCE_LOCK")" == "0:600" ]]; then
    rm -f -- "$MAINTENANCE_LOCK"
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM HUP

python3 "$PREFLIGHT_SCRIPT" >"$temporary"
mapfile -d '' -t topology < <(python3 - "$temporary" <<'PY'
import json, os, re, sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="ascii"))
compose = data.get("compose")
required = {
    "schema_version", "container_identity_hash", "container_name", "image",
    "image_identity_hash", "network_mode", "restart_policy",
    "config_mount_type", "config_mount_source", "docker_server_version",
    "architecture", "config_free_bytes", "compose", "upgrade_method",
    "environment_exported", "read_only",
}
if set(data) != required or data.get("schema_version") != 1:
    raise SystemExit(2)
if data.get("upgrade_method") != "docker_compose":
    raise SystemExit(2)
if data.get("architecture") != "x86_64" or data.get("network_mode") != "host":
    raise SystemExit(2)
if data.get("restart_policy") != "unless-stopped" or data.get("config_mount_type") != "bind":
    raise SystemExit(2)
if data.get("environment_exported") is not False or data.get("read_only") is not True:
    raise SystemExit(2)
if not isinstance(data.get("config_free_bytes"), int) or data["config_free_bytes"] < 8589934592:
    raise SystemExit(2)
if not isinstance(compose, dict) or not all(
    compose.get(key) is True for key in ("detected", "available", "files_safe")
):
    raise SystemExit(2)
project = compose.get("project")
service = compose.get("service")
working_dir = compose.get("working_dir")
files = compose.get("config_files")
image = data.get("image")
if not isinstance(project, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", project):
    raise SystemExit(2)
if not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", service):
    raise SystemExit(2)
if not isinstance(working_dir, str) or not working_dir.startswith("/"):
    raise SystemExit(2)
if not isinstance(files, list) or not 1 <= len(files) <= 8:
    raise SystemExit(2)
if not all(isinstance(item, str) and item.startswith("/") and os.path.normpath(item) == item for item in files):
    raise SystemExit(2)
if image != "ghcr.io/home-assistant/home-assistant:stable":
    raise SystemExit(2)
items = [data["container_name"], image, project, service, working_dir, *files]
sys.stdout.buffer.write(b"\0".join(item.encode("utf-8") for item in items) + b"\0")
PY
)
(( ${#topology[@]} >= 6 )) || fail compose_topology_invalid
container_name="${topology[0]}"
configured_image="${topology[1]}"
compose_project="${topology[2]}"
compose_service="${topology[3]}"
compose_working_dir="${topology[4]}"
compose_files=("${topology[@]:5}")

compose_command=(
  /usr/bin/docker compose
  --project-name "$compose_project"
  --project-directory "$compose_working_dir"
)
for compose_file in "${compose_files[@]}"; do
  compose_command+=(-f "$compose_file")
done

mapfile -t container_ids < <(
  docker container ls --all --quiet --no-trunc --filter label=io.hass.type=core
)
(( ${#container_ids[@]} == 1 )) || fail container_identity_invalid
container_id="${container_ids[0]}"
[[ "$container_id" =~ ^[a-f0-9]{64}$ ]] || fail container_identity_invalid
[[ "$(docker inspect --format '{{.Name}}' "$container_id")" == "/$container_name" ]] \
  || fail container_identity_invalid
old_image_id=$(docker inspect --format '{{.Image}}' "$container_id")
[[ "$old_image_id" =~ ^sha256:[a-f0-9]{64}$ ]] || fail current_image_invalid
[[ "$(docker image inspect --format '{{.Id}}' "$configured_image")" == "$old_image_id" ]] \
  || fail configured_image_mismatch
current_version=$(docker exec "$container_id" python -m homeassistant --version 2>/dev/null | tr -d '\r\n')

wait_for_version() {
  expected_version="$1"
  expected_image_id="$2"
  for _attempt in $(seq 1 120); do
    mapfile -t running_ids < <(
      docker container ls --all --quiet --no-trunc --filter label=io.hass.type=core
    )
    if (( ${#running_ids[@]} == 1 )) && [[ "${running_ids[0]}" =~ ^[a-f0-9]{64}$ ]]; then
      candidate_id="${running_ids[0]}"
      running=$(docker inspect --format '{{.State.Running}}' "$candidate_id" 2>/dev/null || true)
      candidate_image=$(docker inspect --format '{{.Image}}' "$candidate_id" 2>/dev/null || true)
      if [[ "$running" == "true" && "$candidate_image" == "$expected_image_id" ]] \
        && curl --fail --silent --show-error --output /dev/null \
          --connect-timeout 3 --max-time 5 http://127.0.0.1:8123/; then
        observed_version=$(docker exec "$candidate_id" python -m homeassistant --version 2>/dev/null | tr -d '\r\n' || true)
        [[ "$observed_version" == "$expected_version" ]] && return 0
      fi
    fi
    sleep 5
  done
  return 1
}

rollback_to_old() {
  docker image tag "$old_image_id" "$configured_image" >/dev/null 2>&1 || return 1
  "${compose_command[@]}" up --detach --no-deps --force-recreate --pull never \
    "$compose_service" >/dev/null 2>&1 || return 1
  wait_for_version "$EXPECTED_CURRENT_VERSION" "$old_image_id"
}

rollback_armed=0
on_error() {
  original_status=$?
  trap - ERR
  set +e
  if (( rollback_armed == 1 )); then
    if rollback_to_old; then
      printf '%s\n' 'status=upgrade_failed_rolled_back'
      exit 4
    fi
    printf '%s\n' 'status=upgrade_failed_rollback_failed'
    exit 5
  fi
  printf '%s\n' 'status=upgrade_preflight_failed'
  exit "$original_status"
}
trap on_error ERR

if [[ "$MODE" == "--rollback" ]]; then
  [[ "$current_version" == "$TARGET_VERSION" ]] || fail rollback_source_version_invalid
  rollback_id=$(docker image inspect --format '{{.Id}}' "$ROLLBACK_IMAGE")
  [[ "$rollback_id" =~ ^sha256:[a-f0-9]{64}$ ]] || fail rollback_image_invalid
  rollback_version=$(
    docker run --rm --network none --cap-drop ALL \
      --security-opt no-new-privileges --entrypoint python "$ROLLBACK_IMAGE" \
      -m homeassistant --version 2>/dev/null | tr -d '\r\n'
  )
  [[ "$rollback_version" == "$EXPECTED_CURRENT_VERSION" ]] \
    || fail rollback_image_version_invalid
  old_image_id="$rollback_id"
  rollback_to_old || fail rollback_failed
  trap - ERR
  printf '%s\n' 'status=rollback_completed core=2026.5.2'
  exit 0
fi

[[ "$current_version" == "$EXPECTED_CURRENT_VERSION" ]] \
  || fail current_core_version_invalid
readonly TARGET_IMAGE="ghcr.io/home-assistant/home-assistant:${TARGET_VERSION}"
printf '%s\n' 'stage=pulling_verified_target'
docker image pull "$TARGET_IMAGE" >/dev/null
target_image_id=$(docker image inspect --format '{{.Id}}' "$TARGET_IMAGE")
target_arch=$(docker image inspect --format '{{.Architecture}}' "$TARGET_IMAGE")
target_os=$(docker image inspect --format '{{.Os}}' "$TARGET_IMAGE")
[[ "$target_image_id" =~ ^sha256:[a-f0-9]{64}$ \
  && "$target_arch" == "amd64" && "$target_os" == "linux" ]] \
  || fail target_image_invalid
target_version=$(
  docker run --rm --network none --cap-drop ALL \
    --security-opt no-new-privileges --entrypoint python "$TARGET_IMAGE" \
    -m homeassistant --version 2>/dev/null | tr -d '\r\n'
)
[[ "$target_version" == "$TARGET_VERSION" ]] || fail target_image_version_invalid

if docker image inspect "$ROLLBACK_IMAGE" >/dev/null 2>&1; then
  [[ "$(docker image inspect --format '{{.Id}}' "$ROLLBACK_IMAGE")" == "$old_image_id" ]] \
    || fail rollback_tag_conflict
else
  docker image tag "$old_image_id" "$ROLLBACK_IMAGE"
fi
docker image tag "$target_image_id" "$configured_image"
rollback_armed=1
printf '%s\n' 'stage=recreating_home_assistant'
"${compose_command[@]}" up --detach --no-deps --force-recreate --pull never \
  "$compose_service" >/dev/null
wait_for_version "$TARGET_VERSION" "$target_image_id"
rollback_armed=0
trap - ERR
printf '%s\n' 'status=upgrade_completed core_from=2026.5.2 core_to=2026.7.4'
