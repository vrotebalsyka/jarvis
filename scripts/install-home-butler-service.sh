#!/usr/bin/env bash
set -euo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C.UTF-8

readonly SERVICE_USER="homebutler"
readonly SERVICE_GROUP="homebutler"
readonly SERVICE_HOME="/home/homebutler"
readonly PROJECT_DIR="/root/Jarvis/home-butler"
readonly RUNTIME_DIR="/opt/home-butler"
readonly UNIT_SOURCE_DIR="$PROJECT_DIR/config/systemd"
readonly UNIT_TARGET_DIR="/etc/systemd/system"
readonly MANAGED_MARKER="# Managed by $PROJECT_DIR/scripts/install-home-butler-service.sh"
readonly RUNTIME_MARKER="$RUNTIME_DIR/.runtime-0.19.1"
readonly PYTHON_SOURCE="$PROJECT_DIR/runtime-home/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu"
readonly RECOVERY_KEY_SOURCE="$PROJECT_DIR/secrets/ha-recovery-ed25519"
readonly ACTION_TIMERS_MODE="${HOME_BUTLER_INSTALL_ACTION_TIMERS_MODE:-staged}"

fail() {
  printf '%s\n' "$1" >&2
  exit 2
}

if (( EUID != 0 )); then
  fail 'This installer must run as root.'
fi
[[ "$ACTION_TIMERS_MODE" == "enabled" || "$ACTION_TIMERS_MODE" == "staged" ]] \
  || fail 'HOME_BUTLER_INSTALL_ACTION_TIMERS_MODE must be enabled or staged.'
if [[ ! -x "$PROJECT_DIR/scripts/run-hermes-gateway.sh" \
  || ! -x "$PROJECT_DIR/scripts/local-health-check.sh" \
  || ! -x "$PROJECT_DIR/hermes-agent/venv/bin/python" \
  || ! -x "$PYTHON_SOURCE/bin/python3.11" ]]; then
  fail 'Required project runtime is unavailable.'
fi

assert_real_directory() {
  local path="$1" owner="$2"
  [[ -d "$path" && ! -L "$path" ]] \
    || fail "Unsafe or missing directory: $path"
  [[ "$(stat -c '%F:%u' -- "$path")" == "directory:$owner" ]] \
    || fail "Unexpected directory owner or type: $path"
  (( (8#$(stat -c '%a' -- "$path") & 8#022) == 0 )) \
    || fail "Directory is writable by group or others: $path"
}

# Validate every privileged parent before the first mutation.
for root_directory in /home /opt /etc /etc/systemd /etc/systemd/system; do
  assert_real_directory "$root_directory" 0
done
[[ -f "$RECOVERY_KEY_SOURCE" && ! -L "$RECOVERY_KEY_SOURCE" \
  && "$(stat -c '%F:%u:%g:%a:%h' -- "$RECOVERY_KEY_SOURCE")" == \
    "regular file:0:0:600:1" ]] \
  || fail 'The out-of-band recovery private key is missing or unsafe.'
if systemctl is-active --quiet home-butler.service 2>/dev/null; then
  fail 'Refusing to modify an active Home Butler runtime.'
fi

if id "$SERVICE_USER" >/dev/null 2>&1; then
  IFS=: read -r account _ uid gid _ account_home account_shell < <(
    getent passwd "$SERVICE_USER"
  )
  [[ "$account" == "$SERVICE_USER" && "$uid" =~ ^[0-9]+$ && "$uid" != 0 \
    && "$gid" =~ ^[0-9]+$ && "$account_home" == "$SERVICE_HOME" \
    && "$account_shell" == "/usr/sbin/nologin" ]] \
    || fail 'Existing homebutler account has unexpected properties.'
  IFS=: read -r group_name _ group_gid group_members < <(
    getent group "$SERVICE_GROUP"
  )
  [[ "$group_name" == "$SERVICE_GROUP" && "$group_gid" == "$gid" \
    && -z "$group_members" ]] \
    || fail 'Existing homebutler group has unexpected properties.'
else
  getent group "$SERVICE_GROUP" >/dev/null 2>&1 \
    && fail 'A group named homebutler exists without the service account.'
  useradd --system --user-group --create-home --home-dir "$SERVICE_HOME" \
    --shell /usr/sbin/nologin "$SERVICE_USER"
fi

readonly SERVICE_UID="$(id -u "$SERVICE_USER")"
readonly SERVICE_GID="$(id -g "$SERVICE_USER")"
(( SERVICE_UID > 0 )) || fail 'The service UID must be unprivileged.'
[[ "$(getent group "$SERVICE_GID" | cut -d: -f1)" == "$SERVICE_GROUP" ]] \
  || fail 'The service primary group is invalid.'
[[ "$(id -G "$SERVICE_USER")" == "$SERVICE_GID" ]] \
  || fail 'The service account has unexpected supplementary groups.'

ensure_service_directory() {
  local path="$1"
  if [[ -e "$path" || -L "$path" ]]; then
    [[ -d "$path" && ! -L "$path" ]] \
      || fail "Refusing unsafe service directory: $path"
    [[ "$(stat -c '%F:%u:%g' -- "$path")" == "directory:$SERVICE_UID:$SERVICE_GID" ]] \
      || fail "Unexpected service directory owner: $path"
    chmod 0700 -- "$path"
  else
    install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0700 -- "$path"
  fi
}

ensure_service_directory "$SERVICE_HOME"
ensure_service_directory "$SERVICE_HOME/.hermes"
for directory in cache cron logs memories sessions skills; do
  ensure_service_directory "$SERVICE_HOME/.hermes/$directory"
done
ensure_service_directory "$SERVICE_HOME/.local"
ensure_service_directory "$SERVICE_HOME/.local/state"
ensure_service_directory "$SERVICE_HOME/.local/state/home-butler"
ensure_service_directory "$SERVICE_HOME/.local/state/home-butler/incidents"
ensure_service_directory "$SERVICE_HOME/.local/state/home-butler/memory"
ensure_service_directory "$SERVICE_HOME/.local/state/home-butler/scheduler"
ensure_service_directory "$SERVICE_HOME/.local/share"
ensure_service_directory "$SERVICE_HOME/.local/share/home-butler"
ensure_service_directory "$SERVICE_HOME/.local/share/home-butler/model-workspace"
ensure_service_directory "$SERVICE_HOME/.config"
ensure_service_directory "$SERVICE_HOME/.config/home-butler"

ensure_service_file() {
  local source="$1" target="$2"
  if [[ -e "$target" || -L "$target" ]]; then
    [[ -f "$target" && ! -L "$target" ]] \
      || fail "Refusing unsafe service file: $target"
    [[ "$(stat -c '%F:%u:%g' -- "$target")" == \
      "regular file:$SERVICE_UID:$SERVICE_GID" ]] \
      || fail "Unexpected service file owner: $target"
  fi
  install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 -- "$source" "$target"
}

ensure_service_seed_file() {
  local source="$1" target="$2"
  if [[ -e "$target" || -L "$target" ]]; then
    [[ -f "$target" && ! -L "$target" \
      && "$(stat -c '%F:%u:%g:%a:%h' -- "$target")" == \
        "regular file:$SERVICE_UID:$SERVICE_GID:600:1" \
      && "$(stat -c '%s' -- "$target")" -le 65536 ]] \
      || fail "Unsafe owner-editable service file: $target"
    return
  fi
  install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 -- "$source" "$target"
}

ensure_service_seed_file \
  "$PROJECT_DIR/config/HOME-BUTLER-INSTRUCTIONS.md" \
  "$SERVICE_HOME/.config/home-butler/HOME-BUTLER-INSTRUCTIONS.md"

if [[ -e "$RUNTIME_DIR" || -L "$RUNTIME_DIR" ]]; then
  assert_real_directory "$RUNTIME_DIR" 0
  [[ -f "$RUNTIME_MARKER" && ! -L "$RUNTIME_MARKER" \
    && "$(stat -c '%u:%g:%a' -- "$RUNTIME_MARKER")" == "0:0:444" ]] \
    || fail 'Existing /opt/home-butler is incomplete or unmanaged.'
else
  install -d -o root -g root -m 0755 -- "$RUNTIME_DIR"
  install -d -o root -g root -m 0755 -- \
    "$RUNTIME_DIR/hermes-agent" \
    "$RUNTIME_DIR/runtime-home/.local/share/uv/python"

  tar -C "$PROJECT_DIR/hermes-agent" \
    --exclude='./.git' --exclude='./node_modules' --exclude='./venv' \
    --exclude='./apps' --exclude='./tests' --exclude='./tests-js' \
    --exclude='./website' -cf - . \
    | tar -C "$RUNTIME_DIR/hermes-agent" -xf -
  cp -a -- "$PROJECT_DIR/hermes-agent/venv" "$RUNTIME_DIR/hermes-agent/venv"
  cp -a -- "$PYTHON_SOURCE" \
    "$RUNTIME_DIR/runtime-home/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu"
  ln -s -- cpython-3.11.15-linux-x86_64-gnu \
    "$RUNTIME_DIR/runtime-home/.local/share/uv/python/cpython-3.11-linux-x86_64-gnu"
  rm -- "$RUNTIME_DIR/hermes-agent/venv/bin/python"
  ln -s -- \
    "$RUNTIME_DIR/runtime-home/.local/share/uv/python/cpython-3.11-linux-x86_64-gnu/bin/python3.11" \
    "$RUNTIME_DIR/hermes-agent/venv/bin/python"

  while IFS= read -r -d '' runtime_file; do
    if grep -IqF '/root/Jarvis/home-butler' -- "$runtime_file"; then
      sed -i 's#/root/Jarvis/home-butler#/opt/home-butler#g' -- "$runtime_file"
    fi
  done < <(find "$RUNTIME_DIR/hermes-agent/venv" -type f -print0)

  chown -R root:root -- "$RUNTIME_DIR"
  chmod -R go-w -- "$RUNTIME_DIR"
  printf '%s\n' 'Hermes Agent 0.19.1 isolated runtime' > "$RUNTIME_MARKER"
  chmod 0444 -- "$RUNTIME_MARKER"
fi

for runtime_directory in scripts config hermes skills; do
  if [[ -e "$RUNTIME_DIR/$runtime_directory" || -L "$RUNTIME_DIR/$runtime_directory" ]]; then
    assert_real_directory "$RUNTIME_DIR/$runtime_directory" 0
  else
    install -d -o root -g root -m 0755 -- "$RUNTIME_DIR/$runtime_directory"
  fi
done

install_root_file() {
  local mode="$1" source="$2" target="$3"
  if [[ -e "$target" || -L "$target" ]]; then
    [[ -f "$target" && ! -L "$target" \
      && "$(stat -c '%F:%u:%g:%h' -- "$target")" == "regular file:0:0:1" ]] \
      || fail "Refusing unsafe runtime file: $target"
  fi
  install -o root -g root -m "$mode" -- "$source" "$target"
}

managed_runtime_scripts=()
for executable in \
  run-hermes-gateway.sh local-health-check.sh health_report.py heartbeat.py \
  incident_monitor.py incident_notifier.py home_assistant_notify.py \
  entity_freshness.py device_health.py system_log_diagnostics.py \
  ha_model_study.py ha_full_entity_report.py ha_device_knowledge.py \
  device_onboarding.py \
  diagnostic_monitor.py \
  windows_gpu_supervisor.py \
  daily_voice_report.py operations_supervisor.py home_stress_test.py incident_timeline.py \
  alice_voice_bridge.py alice_skill_gateway.py alice_skill_health.py \
  alice_claim_finalizer.py alice_tailscale_funnel.py rotate-alice-webhook.py owner_chat.py \
  yandex_station_reminder.py persistent_scheduler.py scheduler_natural.py \
  windows_wake_sync.py \
  update-home-butler-lan-forward.sh \
  model_workspace.py safe_maintenance.py maintenance_worker.py \
  memory_store.py behavior_preferences.py context_builder.py turn_observability.py \
  capability_catalog.py bounded_ha_agent.py \
  device_learning.py \
  local_chat_gateway.py \
  startup_self_check.py startup_voice_status.py \
  qualification_status.py \
  dialogue_qualification.py \
  incident_status.py \
  home_assistant_inventory.py safe_attribute_sanitizer.py \
  automation_diagnostics.py recovery_planner.py recovery_playbook_registry.py \
  recovery_playbook_executor.py automation_recovery.py \
  integration_recovery.py \
  home_assistant_recovery.py \
  home_assistant_core_recovery.py \
  out_of_band_recovery.py \
  home_assistant_read.py home_assistant_control.py home_assistant_mcp.py \
  ha_entity_query.py ollama_endpoint.py \
  model_runtime_policy.py model_ha_proof.py model_ha_control.py verify-runtime-policy.py; do
  managed_runtime_scripts+=("$executable")
  install_root_file 0755 \
    "$PROJECT_DIR/scripts/$executable" "$RUNTIME_DIR/scripts/$executable"
done
install_root_file 0644 \
  "$PROJECT_DIR/scripts/health_report_core.py" \
  "$RUNTIME_DIR/scripts/health_report_core.py"
managed_runtime_scripts+=("health_report_core.py")

# Runtime code is a closed managed set. Operator backups belong in
# /var/backups/home-butler, never beside importable/executable production code.
declare -A managed_runtime_script_names=()
for runtime_script_name in "${managed_runtime_scripts[@]}"; do
  managed_runtime_script_names["$runtime_script_name"]=1
done
while IFS= read -r -d '' runtime_script_path; do
  runtime_script_name="$(basename -- "$runtime_script_path")"
  [[ -n "${managed_runtime_script_names[$runtime_script_name]+managed}" ]] \
    || fail "Refusing unmanaged runtime script: $runtime_script_path"
done < <(find "$RUNTIME_DIR/scripts" -maxdepth 1 -type f -print0)

install_root_file 0644 \
  "$PROJECT_DIR/hermes/.env" "$RUNTIME_DIR/hermes/.env"
install_root_file 0644 \
  "$PROJECT_DIR/hermes/config.yaml" "$RUNTIME_DIR/hermes/config.yaml"
sed -i 's#/root/Jarvis/home-butler#/opt/home-butler#g' \
  "$RUNTIME_DIR/hermes/config.yaml"
install_root_file 0644 \
  "$PROJECT_DIR/SOUL.md" "$RUNTIME_DIR/hermes/SOUL.md"
install_root_file 0644 \
  "$PROJECT_DIR/hermes/.no-bundled-skills" "$RUNTIME_DIR/hermes/.no-bundled-skills"

# AGENTS.md is the project context Hermes actually injects. Include the full
# heartbeat, tool, and read-only skill policies in that loaded file, while also
# installing their source artifacts separately for operator inspection.
install_root_file 0644 "$PROJECT_DIR/AGENTS.md" "$RUNTIME_DIR/AGENTS.md"
{
  printf '\n\n# Runtime policy from HEARTBEAT.md\n\n'
  cat -- "$PROJECT_DIR/HEARTBEAT.md"
  printf '\n\n# Runtime policy from TOOLS.md\n\n'
  cat -- "$PROJECT_DIR/TOOLS.md"
  for skill_policy in "$PROJECT_DIR"/skills/*/SKILL.md; do
    [[ -f "$skill_policy" && ! -L "$skill_policy" ]] \
      || fail 'Project skill policy is missing or unsafe.'
    printf '\n\n# Runtime read-only skill: %s\n\n' \
      "$(basename -- "$(dirname -- "$skill_policy")")"
    cat -- "$skill_policy"
  done
} >> "$RUNTIME_DIR/AGENTS.md"
chown root:root -- "$RUNTIME_DIR/AGENTS.md"
chmod 0644 -- "$RUNTIME_DIR/AGENTS.md"
install_root_file 0644 "$PROJECT_DIR/HEARTBEAT.md" "$RUNTIME_DIR/HEARTBEAT.md"
install_root_file 0644 "$PROJECT_DIR/TOOLS.md" "$RUNTIME_DIR/TOOLS.md"

install_root_file 0644 \
  "$PROJECT_DIR/config/home-assistant.env" "$RUNTIME_DIR/config/home-assistant.env"
install_root_file 0644 \
  "$PROJECT_DIR/config/ha-recovery-known_hosts" \
  "$RUNTIME_DIR/config/ha-recovery-known_hosts"
sed -i \
  's#^HOME_ASSISTANT_TOKEN_FILE=.*#HOME_ASSISTANT_TOKEN_FILE=systemd-credential:home-assistant.token#' \
  "$RUNTIME_DIR/config/home-assistant.env"

if find "$RUNTIME_DIR/skills" -type l -print -quit | grep -q .; then
  fail 'Runtime skills tree contains an unexpected symbolic link.'
fi
if find "$PROJECT_DIR/skills" -type l -print -quit | grep -q .; then
  fail 'Project skills tree contains an unexpected symbolic link.'
fi
cp -a -- "$PROJECT_DIR/skills/." "$RUNTIME_DIR/skills/"
chown -R root:root -- "$RUNTIME_DIR/skills"
chmod -R go-w -- "$RUNTIME_DIR/skills"

# These are bind-mounted read-only from /opt by the gateway unit. The private
# service-owned copies also make HERMES_HOME valid for controlled diagnostics
# outside the service mount namespace.
ensure_service_file "$RUNTIME_DIR/hermes/config.yaml" "$SERVICE_HOME/.hermes/config.yaml"
ensure_service_file "$RUNTIME_DIR/hermes/SOUL.md" "$SERVICE_HOME/.hermes/SOUL.md"
ensure_service_file "$RUNTIME_DIR/hermes/.no-bundled-skills" \
  "$SERVICE_HOME/.hermes/.no-bundled-skills"

(
  cd "$RUNTIME_DIR"
  runuser -u "$SERVICE_USER" -- env -i \
    HOME="$SERVICE_HOME" HERMES_HOME="$SERVICE_HOME/.hermes" \
    PATH=/usr/local/bin:/usr/bin:/bin LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 \
    "$RUNTIME_DIR/hermes-agent/venv/bin/python" \
    "$RUNTIME_DIR/scripts/verify-runtime-policy.py"
) | grep -Fqx 'RUNTIME_POLICY_OK' \
  || fail 'Installed runtime policy was not loaded by Hermes.'

install_unit() {
  local name="$1"
  local source="$UNIT_SOURCE_DIR/$name"
  local target="$UNIT_TARGET_DIR/$name"
  if [[ -e "$target" || -L "$target" ]]; then
    [[ -f "$target" && ! -L "$target" \
      && "$(stat -c '%F:%u:%g:%h' -- "$target")" == "regular file:0:0:1" ]] \
      || fail "Refusing unsafe unit target: $target"
    head -n 1 -- "$target" | grep -Fqx "$MANAGED_MARKER" \
      || fail "Refusing to overwrite unmanaged unit: $target"
  fi
  install -o root -g root -m 0644 -- "$source" "$target"
}

install_unit home-butler.service
install_unit home-butler-heartbeat.service
install_unit home-butler-heartbeat.timer
install_unit home-butler-ha-proof.service
install_unit home-butler-device-learning@.service
install_unit home-butler-startup-ha-check.service
install_unit home-butler-startup-ha-check.timer
install_unit home-butler-startup-self-check.service
install_unit home-butler-startup-self-check.timer
install_unit home-butler-startup-voice-status.service
install_unit home-butler-startup-voice-status.timer
install_unit home-butler-dialogue-qualification.service
install_unit home-butler-dialogue-qualification.timer
install_unit home-butler-incident-monitor.service
install_unit home-butler-incident-notifier.service
install_unit home-butler-incident-notifier.timer
install_unit home-butler-daily-report.service
install_unit home-butler-daily-report.timer
install_unit home-butler-operations-supervisor.service
install_unit home-butler-operations-supervisor.timer
install_unit home-butler-inventory.service
install_unit home-butler-inventory.timer
install_unit home-butler-ha-device-knowledge.service
install_unit home-butler-ha-device-knowledge.timer
install_unit home-butler-device-onboarding.service
install_unit home-butler-device-onboarding.timer
install_unit home-butler-recovery.service
install_unit home-butler-recovery.timer
install_unit home-butler-automation-diagnostics.service
install_unit home-butler-automation-diagnostics.timer
install_unit home-butler-system-log-diagnostics.service
install_unit home-butler-system-log-diagnostics.timer
install_unit home-butler-device-health.service
install_unit home-butler-device-health.timer
install_unit home-butler-integration-recovery.service
install_unit home-butler-integration-recovery.timer
install_unit home-butler-model-study.service
install_unit home-butler-model-study.timer
install_unit home-butler-full-entity-report.service
install_unit home-butler-diagnostic-monitor.service
install_unit home-butler-diagnostic-monitor.timer
install_unit home-butler-automation-recovery.service
install_unit home-butler-automation-recovery.timer
install_unit home-butler-entity-freshness.service
install_unit home-butler-entity-freshness.timer
install_unit home-butler-core-recovery.service
install_unit home-butler-core-recovery.timer
install_unit home-butler-voice-intent.service
install_unit home-butler-alice-skill.service
install_unit home-butler-local-chat.service
install_unit home-butler-alice-tunnel.service
install_unit home-butler-alice-health.service
install_unit home-butler-alice-health.timer
install_unit home-butler-alice-finalize.service
install_unit home-butler-alice-finalize.path
install_unit home-butler-alice-rotation-finalize.service
install_unit home-butler-alice-rotation-finalize.path
install_unit home-butler-out-of-band-recovery.service
install_unit home-butler-out-of-band-recovery.timer

systemd-analyze verify \
  "$UNIT_TARGET_DIR/home-butler.service" \
  "$UNIT_TARGET_DIR/home-butler-heartbeat.service" \
  "$UNIT_TARGET_DIR/home-butler-heartbeat.timer" \
  "$UNIT_TARGET_DIR/home-butler-ha-proof.service" \
  "$UNIT_TARGET_DIR/home-butler-device-learning@.service" \
  "$UNIT_TARGET_DIR/home-butler-startup-ha-check.service" \
  "$UNIT_TARGET_DIR/home-butler-startup-ha-check.timer" \
  "$UNIT_TARGET_DIR/home-butler-startup-self-check.service" \
  "$UNIT_TARGET_DIR/home-butler-startup-self-check.timer" \
  "$UNIT_TARGET_DIR/home-butler-startup-voice-status.service" \
  "$UNIT_TARGET_DIR/home-butler-startup-voice-status.timer" \
  "$UNIT_TARGET_DIR/home-butler-dialogue-qualification.service" \
  "$UNIT_TARGET_DIR/home-butler-dialogue-qualification.timer" \
  "$UNIT_TARGET_DIR/home-butler-incident-monitor.service" \
  "$UNIT_TARGET_DIR/home-butler-incident-notifier.service" \
  "$UNIT_TARGET_DIR/home-butler-incident-notifier.timer" \
  "$UNIT_TARGET_DIR/home-butler-daily-report.service" \
  "$UNIT_TARGET_DIR/home-butler-daily-report.timer" \
  "$UNIT_TARGET_DIR/home-butler-operations-supervisor.service" \
  "$UNIT_TARGET_DIR/home-butler-operations-supervisor.timer" \
  "$UNIT_TARGET_DIR/home-butler-inventory.service" \
  "$UNIT_TARGET_DIR/home-butler-inventory.timer" \
  "$UNIT_TARGET_DIR/home-butler-recovery.service" \
  "$UNIT_TARGET_DIR/home-butler-recovery.timer" \
  "$UNIT_TARGET_DIR/home-butler-automation-diagnostics.service" \
  "$UNIT_TARGET_DIR/home-butler-automation-diagnostics.timer" \
  "$UNIT_TARGET_DIR/home-butler-system-log-diagnostics.service" \
  "$UNIT_TARGET_DIR/home-butler-system-log-diagnostics.timer" \
  "$UNIT_TARGET_DIR/home-butler-device-health.service" \
  "$UNIT_TARGET_DIR/home-butler-device-health.timer" \
  "$UNIT_TARGET_DIR/home-butler-integration-recovery.service" \
  "$UNIT_TARGET_DIR/home-butler-integration-recovery.timer" \
  "$UNIT_TARGET_DIR/home-butler-automation-recovery.service" \
  "$UNIT_TARGET_DIR/home-butler-automation-recovery.timer" \
  "$UNIT_TARGET_DIR/home-butler-entity-freshness.service" \
  "$UNIT_TARGET_DIR/home-butler-entity-freshness.timer" \
  "$UNIT_TARGET_DIR/home-butler-core-recovery.service" \
  "$UNIT_TARGET_DIR/home-butler-core-recovery.timer" \
  "$UNIT_TARGET_DIR/home-butler-voice-intent.service" \
  "$UNIT_TARGET_DIR/home-butler-alice-skill.service" \
  "$UNIT_TARGET_DIR/home-butler-local-chat.service" \
  "$UNIT_TARGET_DIR/home-butler-alice-tunnel.service" \
  "$UNIT_TARGET_DIR/home-butler-alice-health.service" \
  "$UNIT_TARGET_DIR/home-butler-alice-health.timer" \
  "$UNIT_TARGET_DIR/home-butler-alice-finalize.service" \
  "$UNIT_TARGET_DIR/home-butler-alice-finalize.path" \
  "$UNIT_TARGET_DIR/home-butler-alice-rotation-finalize.service" \
  "$UNIT_TARGET_DIR/home-butler-alice-rotation-finalize.path" \
  "$UNIT_TARGET_DIR/home-butler-out-of-band-recovery.service" \
  "$UNIT_TARGET_DIR/home-butler-out-of-band-recovery.timer"
systemctl daemon-reload
# The independent channel stays inert until the Ubuntu host bootstrap and a
# healthy end-to-end forced-command test have both succeeded.
systemctl disable --now home-butler-out-of-band-recovery.timer
[[ "$(systemctl is-enabled home-butler-out-of-band-recovery.timer 2>/dev/null)" == "disabled" ]] \
  || fail 'Out-of-band recovery timer must remain disabled before channel verification.'
systemctl is-active --quiet home-butler-out-of-band-recovery.service \
  && fail 'Out-of-band recovery service is unexpectedly active.'
# The legacy four-route Alice bridge depended on Yandex Smart Home scenarios.
# Keep its reviewed unit only as a manual rollback artifact; the target voice
# path is the single private full-dialog skill provisioned separately.
systemctl disable --now home-butler-voice-intent.service
[[ "$(systemctl is-enabled home-butler-voice-intent.service 2>/dev/null)" == "disabled" ]] \
  || fail 'Legacy scenario voice bridge must remain disabled.'
systemctl is-active --quiet home-butler-voice-intent.service \
  && fail 'Legacy scenario voice bridge is unexpectedly active.'
systemctl enable --now \
  home-butler.service home-butler-heartbeat.timer \
  home-butler-startup-ha-check.timer home-butler-incident-monitor.service \
  home-butler-startup-self-check.timer \
  home-butler-startup-voice-status.timer \
  home-butler-dialogue-qualification.timer \
  home-butler-incident-notifier.timer home-butler-inventory.timer \
  home-butler-ha-device-knowledge.timer home-butler-device-onboarding.timer \
  home-butler-daily-report.timer home-butler-automation-diagnostics.timer \
  home-butler-operations-supervisor.timer \
  home-butler-system-log-diagnostics.timer home-butler-device-health.timer \
  home-butler-model-study.timer \
  home-butler-diagnostic-monitor.timer \
  home-butler-entity-freshness.timer home-butler-local-chat.service
if [[ "$ACTION_TIMERS_MODE" == "enabled" ]]; then
  systemctl enable --now \
    home-butler-recovery.timer home-butler-core-recovery.timer \
    home-butler-automation-recovery.timer home-butler-integration-recovery.timer
else
  systemctl disable --now \
    home-butler-recovery.timer home-butler-core-recovery.timer \
    home-butler-automation-recovery.timer home-butler-integration-recovery.timer
fi
systemctl is-enabled --quiet \
  home-butler.service home-butler-heartbeat.timer \
  home-butler-startup-ha-check.timer home-butler-incident-monitor.service \
  home-butler-startup-self-check.timer \
  home-butler-startup-voice-status.timer \
  home-butler-dialogue-qualification.timer \
  home-butler-incident-notifier.timer home-butler-inventory.timer \
  home-butler-ha-device-knowledge.timer home-butler-device-onboarding.timer \
  home-butler-daily-report.timer home-butler-automation-diagnostics.timer \
  home-butler-operations-supervisor.timer \
  home-butler-system-log-diagnostics.timer home-butler-device-health.timer \
  home-butler-model-study.timer \
  home-butler-diagnostic-monitor.timer \
  home-butler-entity-freshness.timer home-butler-local-chat.service
if [[ "$ACTION_TIMERS_MODE" == "enabled" ]]; then
  systemctl is-enabled --quiet \
    home-butler-recovery.timer home-butler-core-recovery.timer \
    home-butler-automation-recovery.timer home-butler-integration-recovery.timer
else
  for action_timer in \
    home-butler-recovery.timer home-butler-core-recovery.timer \
    home-butler-automation-recovery.timer home-butler-integration-recovery.timer; do
    [[ "$(systemctl is-enabled "$action_timer" 2>/dev/null)" == "disabled" ]] \
      || fail "Action timer was not staged safely: $action_timer"
  done
fi
systemctl is-active --quiet \
  home-butler.service home-butler-heartbeat.timer home-butler-incident-monitor.service \
  home-butler-startup-voice-status.timer \
  home-butler-incident-notifier.timer home-butler-inventory.timer \
  home-butler-ha-device-knowledge.timer home-butler-device-onboarding.timer \
  home-butler-daily-report.timer home-butler-automation-diagnostics.timer \
  home-butler-operations-supervisor.timer \
  home-butler-system-log-diagnostics.timer home-butler-device-health.timer \
  home-butler-model-study.timer \
  home-butler-diagnostic-monitor.timer \
  home-butler-entity-freshness.timer home-butler-local-chat.service
if [[ "$ACTION_TIMERS_MODE" == "enabled" ]]; then
  systemctl is-active --quiet \
    home-butler-recovery.timer home-butler-core-recovery.timer \
    home-butler-automation-recovery.timer home-butler-integration-recovery.timer
fi

# Stage 14 requires one real manual heartbeat before reporting success.
systemctl start home-butler-heartbeat.service
systemctl is-active --quiet \
  home-butler.service home-butler-heartbeat.timer home-butler-incident-monitor.service \
  home-butler-startup-voice-status.timer \
  home-butler-incident-notifier.timer home-butler-inventory.timer \
  home-butler-ha-device-knowledge.timer home-butler-device-onboarding.timer \
  home-butler-daily-report.timer home-butler-automation-diagnostics.timer \
  home-butler-operations-supervisor.timer \
  home-butler-system-log-diagnostics.timer home-butler-device-health.timer \
  home-butler-model-study.timer \
  home-butler-diagnostic-monitor.timer \
  home-butler-entity-freshness.timer
if [[ "$ACTION_TIMERS_MODE" == "enabled" ]]; then
  systemctl is-active --quiet \
    home-butler-recovery.timer home-butler-core-recovery.timer \
    home-butler-automation-recovery.timer home-butler-integration-recovery.timer
fi
for state_file in heartbeat-state.json latest-snapshot.json latest-report.txt; do
  path="$SERVICE_HOME/.local/state/home-butler/$state_file"
  [[ -f "$path" && ! -L "$path" \
    && "$(stat -c '%u:%g:%a' -- "$path")" == "$SERVICE_UID:$SERVICE_GID:600" ]] \
    || fail "Heartbeat state verification failed: $state_file"
done

printf '%s\n' 'Home Butler service and heartbeat timer installed and verified.'
