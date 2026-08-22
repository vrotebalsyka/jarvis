#!/usr/bin/env bash
set -euo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C.UTF-8

readonly REQUEST_TIMEOUT_SECONDS=5
readonly MAX_RESPONSE_BYTES=1048576
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"

read_cpu_totals() {
  local label user nice system idle iowait irq softirq steal guest guest_nice
  read -r label user nice system idle iowait irq softirq steal guest guest_nice < /proc/stat
  printf '%s %s\n' \
    "$((user + nice + system + idle + iowait + irq + softirq + steal))" \
    "$((idle + iowait))"
}

parse_temperatures_json() {
  jq -ce '[
    to_entries[] as $chip
    | $chip.value | to_entries[]
    | select(.value | type == "object") as $feature
    | $feature.value | to_entries[]
    | select((.key | test("^temp[0-9]+_input$")) and (.value | type == "number"))
    | {chip: $chip.key, sensor: $feature.key, celsius: .value}
  ]'
}

parse_ollama_models_json() {
  jq -ce '
    if type == "object" and has("models") and (.models | type) == "array"
      and all(.models[]; type == "object"
        and ((.name // .model // null) | type) == "string"
        and (.size | type) == "number" and (.size_vram | type) == "number"
        and (.context_length | type) == "number" and (.expires_at | type) == "string")
    then [.models[] | {name: (.name // .model), size_bytes: .size,
      size_vram_bytes: .size_vram, context_length: .context_length,
      expires_at: .expires_at}]
    else error("invalid Ollama models response") end'
}

collect_home_assistant_json() {
  local project_dir="$1"
  local adapter_path="$2"
  local result='{"configured":false,"status":"not_configured"}'
  local material_present=false
  local adapter_output parsed_adapter
  if [[ -f "$project_dir/config/home-assistant.env" ]]; then
    material_present=true
    result='{"configured":true,"status":"api_unavailable"}'
  fi
  if [[ -x "$adapter_path" ]]; then
    if adapter_output="$(
      timeout 12 "$adapter_path" health 2>/dev/null |
        head -c "$((MAX_RESPONSE_BYTES + 1))"
    )" && (( ${#adapter_output} <= MAX_RESPONSE_BYTES )) && parsed_adapter="$(
      jq -ce '
        if type == "object"
          and .schema_version == 1
          and (.configured | type) == "boolean"
          and (.status | IN("not_configured", "dns_failure", "host_unreachable",
            "port_closed", "unauthorized", "api_unavailable", "stale_data", "healthy"))
          and (.observed_at | type) == "string"
        then {configured, status}
        else error("invalid Home Assistant adapter response") end
      ' <<<"$adapter_output" 2>/dev/null
    )" && [[ -n "$parsed_adapter" ]]; then
      if [[ "$material_present" == false ]] \
        || [[ "$(jq -r '.configured' <<<"$parsed_adapter")" == true ]]; then
        result="$parsed_adapter"
      fi
    fi
  fi
  printf '%s\n' "$result"
}

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  return 0
fi

readonly OLLAMA_BASE_URL="$(python3 "$SCRIPT_DIR/ollama_endpoint.py")"
readonly OLLAMA_VERSION_URL="$OLLAMA_BASE_URL/api/version"
readonly OLLAMA_PS_URL="$OLLAMA_BASE_URL/api/ps"

cpu_before="$(read_cpu_totals)"
sleep 0.2
cpu_after="$(read_cpu_totals)"
read -r total_before idle_before <<<"$cpu_before"
read -r total_after idle_after <<<"$cpu_after"
cpu_percent="$(awk -v total="$((total_after - total_before))" \
  -v idle="$((idle_after - idle_before))" \
  'BEGIN { if (total <= 0) print "0.0"; else printf "%.1f", 100 * (total - idle) / total }')"

mem_total_kib="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
mem_available_kib="$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)"
swap_total_kib="$(awk '/^SwapTotal:/ {print $2}' /proc/meminfo)"
swap_free_kib="$(awk '/^SwapFree:/ {print $2}' /proc/meminfo)"

memory_used_percent="$(awk -v total="$mem_total_kib" -v available="$mem_available_kib" \
  'BEGIN { if (total <= 0) print "0.0"; else printf "%.1f", 100 * (total - available) / total }')"
swap_used_percent="$(awk -v total="$swap_total_kib" -v free="$swap_free_kib" \
  'BEGIN { if (total <= 0) print "0.0"; else printf "%.1f", 100 * (total - free) / total }')"

disks_json='[]'
disk_command_status=0
if disk_output="$(
  timeout "$REQUEST_TIMEOUT_SECONDS" df -PT -B1 -l 2>/dev/null |
    head -c "$((MAX_RESPONSE_BYTES + 1))"
)"; then
  disk_command_status=0
else
  disk_command_status=$?
fi
if (( disk_command_status == 0 )) \
  && (( $(printf '%s' "$disk_output" | wc -c) <= MAX_RESPONSE_BYTES )); then
  disks_json="$(
    awk 'NR > 1 && $2 !~ /^(tmpfs|devtmpfs|squashfs|overlay|9p|drvfs|cgroup|cgroup2|proc|sysfs|rootfs)$/ && $2 !~ /^fuse\./ {
      sub(/%$/, "", $6)
      printf "%s\t%s\t%s\t%s\t%s\t%s\n", $1, $2, $3, $4, $5, $6
    }' <<<"$disk_output" |
      jq -Rsc '
        split("\n")
        | map(select(length > 0) | split("\t"))
        | map({filesystem: .[0], type: .[1], total_bytes: (.[2] | tonumber), used_bytes: (.[3] | tonumber), available_bytes: (.[4] | tonumber), used_percent: (.[5] | tonumber)})
      '
  )"
fi

temperatures_json='[]'
temperature_probe_status='unavailable'
if command -v sensors >/dev/null 2>&1; then
  temperature_probe_status='error'
  sensor_command_status=0
  if ! sensor_output="$(
    timeout "$REQUEST_TIMEOUT_SECONDS" sensors -j 2>/dev/null |
      head -c "$((MAX_RESPONSE_BYTES + 1))"
  )"; then
    sensor_command_status=1
  fi
  if (( $(printf '%s' "$sensor_output" | wc -c) <= MAX_RESPONSE_BYTES )); then
    if parsed_temperatures="$(
      parse_temperatures_json <<<"$sensor_output" 2>/dev/null
    )"; then
      temperatures_json="$parsed_temperatures"
      if (( sensor_command_status == 0 )); then
        temperature_probe_status='ok'
      elif [[ "$parsed_temperatures" == '[]' ]]; then
        temperature_probe_status='unavailable'
      fi
    fi
  fi
fi

failed_units_json='[]'
systemd_probe_status='unavailable'
if command -v systemctl >/dev/null 2>&1; then
  systemd_probe_status='error'
  if failed_units_output="$(
    timeout "$REQUEST_TIMEOUT_SECONDS" systemctl --failed --no-legend --plain 2>/dev/null |
      head -c "$((MAX_RESPONSE_BYTES + 1))"
  )" && (( ${#failed_units_output} <= MAX_RESPONSE_BYTES )); then
    failed_units_json="$(
      awk 'NF {print $1}' <<<"$failed_units_output" |
        jq -Rsc 'split("\n") | map(select(length > 0))'
    )"
    systemd_probe_status='ok'
  fi
fi

ollama_reachable=false
ollama_version=''
ollama_models_json='[]'
ollama_version_probe_status='unreachable'
ollama_models_probe_status='not_run'
if version_output="$(
  timeout "$REQUEST_TIMEOUT_SECONDS" curl -q --noproxy '*' \
    --silent --show-error --fail \
    --max-filesize "$MAX_RESPONSE_BYTES" "$OLLAMA_VERSION_URL" 2>/dev/null |
    head -c "$((MAX_RESPONSE_BYTES + 1))"
)"; then
  version_request_status=0
else
  version_request_status=$?
fi
if (( $(printf '%s' "$version_output" | wc -c) > MAX_RESPONSE_BYTES )) \
  || [[ "$version_request_status" =~ ^(22|23|63)$ ]]; then
  ollama_reachable=true
  ollama_version_probe_status='invalid_response'
elif (( version_request_status == 0 )); then
  ollama_reachable=true
  ollama_version_probe_status='invalid_response'
  if parsed_version="$(
    jq -er '
      if type == "object"
        and ((.version // null) | type) == "string"
        and ((.version // "") | length) > 0
      then .version
      else empty
      end
    ' <<<"$version_output" 2>/dev/null
  )"; then
    ollama_version="$parsed_version"
    ollama_version_probe_status='ok'
  fi

  ollama_models_probe_status='request_failed'
  ps_request_status=0
  if ! ps_output="$(
    timeout "$REQUEST_TIMEOUT_SECONDS" curl -q --noproxy '*' \
      --silent --show-error --fail \
      --max-filesize "$MAX_RESPONSE_BYTES" "$OLLAMA_PS_URL" 2>/dev/null |
      head -c "$((MAX_RESPONSE_BYTES + 1))"
  )"; then
    ps_request_status=1
  fi
  if (( $(printf '%s' "$ps_output" | wc -c) > MAX_RESPONSE_BYTES )); then
    ollama_models_probe_status='invalid_response'
  elif (( ps_request_status == 0 )); then
    ollama_models_probe_status='invalid_response'
    if parsed_models="$(
      parse_ollama_models_json <<<"$ps_output" 2>/dev/null
    )"; then
      ollama_models_json="$parsed_models"
      ollama_models_probe_status='ok'
    fi
  fi
fi

hermes_installed=false
if command -v hermes >/dev/null 2>&1 \
  || [[ -x /root/.local/bin/hermes ]] \
  || [[ -x "$PROJECT_DIR/hermes-agent/hermes" ]]; then
  hermes_installed=true
fi

hermes_gateway_configured=false
hermes_gateway_scope=''
if [[ -f /etc/systemd/system/home-butler.service ]]; then
  hermes_gateway_configured=true
  hermes_gateway_scope='system'
elif [[ -f /root/.config/systemd/user/home-butler.service ]] \
  || [[ -f /etc/systemd/user/home-butler.service ]]; then
  hermes_gateway_configured=true
  hermes_gateway_scope='user'
fi

hermes_gateway_running=false
hermes_gateway_probe_status='not_configured'
if [[ "$hermes_gateway_configured" == true ]]; then
  hermes_gateway_probe_status='error'
  gateway_state=''
  gateway_command=(systemctl is-active home-butler.service)
  if [[ "$hermes_gateway_scope" == 'user' ]]; then
    gateway_command=(systemctl --user is-active home-butler.service)
  fi
  if gateway_state="$(timeout "$REQUEST_TIMEOUT_SECONDS" \
    "${gateway_command[@]}" 2>/dev/null)"; then
    if [[ "$gateway_state" == 'active' ]]; then
      hermes_gateway_running=true
      hermes_gateway_probe_status='ok'
    fi
  elif [[ "$gateway_state" == 'inactive' \
    || "$gateway_state" == 'failed' \
    || "$gateway_state" == 'activating' \
    || "$gateway_state" == 'deactivating' ]]; then
    hermes_gateway_probe_status='ok'
  fi
fi

hermes_status='not_configured'
if [[ "$hermes_installed" == false ]]; then
  hermes_status='not_installed'
elif [[ "$hermes_gateway_configured" == true && "$hermes_gateway_probe_status" != 'ok' ]]; then
  hermes_status='unknown'
elif [[ "$hermes_gateway_configured" == true && "$hermes_gateway_running" == true ]]; then
  hermes_status='running'
elif [[ "$hermes_gateway_configured" == true ]]; then
  hermes_status='stopped'
fi

home_assistant_json="$(
  collect_home_assistant_json "$PROJECT_DIR" "$SCRIPT_DIR/home_assistant_read.py"
)"

jq -n \
  --arg observed_at "$(date --iso-8601=seconds)" \
  --argjson cpu_load_percent "$cpu_percent" \
  --argjson memory_used_percent "$memory_used_percent" \
  --argjson swap_used_percent "$swap_used_percent" \
  --argjson disks "$disks_json" \
  --argjson temperatures "$temperatures_json" \
  --argjson failed_systemd_units "$failed_units_json" \
  --arg temperature_probe_status "$temperature_probe_status" \
  --arg systemd_probe_status "$systemd_probe_status" \
  --arg ollama_version_probe_status "$ollama_version_probe_status" \
  --arg ollama_models_probe_status "$ollama_models_probe_status" \
  --arg hermes_gateway_probe_status "$hermes_gateway_probe_status" \
  --argjson ollama_reachable "$ollama_reachable" \
  --arg ollama_version "$ollama_version" \
  --argjson ollama_models "$ollama_models_json" \
  --argjson hermes_installed "$hermes_installed" \
  --argjson hermes_gateway_configured "$hermes_gateway_configured" \
  --argjson hermes_gateway_running "$hermes_gateway_running" \
  --arg hermes_status "$hermes_status" \
  --argjson home_assistant "$home_assistant_json" \
  '{
    schema_version: 1,
    observed_at: $observed_at,
    host: {
      cpu_load_percent: $cpu_load_percent,
      memory_used_percent: $memory_used_percent,
      swap_used_percent: $swap_used_percent
    },
    disks: $disks,
    temperatures: $temperatures,
    failed_systemd_units: $failed_systemd_units,
    probes: {
      temperatures: $temperature_probe_status,
      systemd: $systemd_probe_status,
      ollama_version: $ollama_version_probe_status,
      ollama_models: $ollama_models_probe_status,
      hermes_gateway: $hermes_gateway_probe_status
    },
    ollama: {
      reachable: $ollama_reachable,
      version: (if $ollama_version == "" then null else $ollama_version end),
      model_loaded: (($ollama_models | length) > 0),
      loaded_models: $ollama_models
    },
    hermes: {
      installed: $hermes_installed,
      gateway_configured: $hermes_gateway_configured,
      gateway_running: $hermes_gateway_running,
      status: $hermes_status
    },
    home_assistant: $home_assistant
  }'
