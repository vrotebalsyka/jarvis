---
name: diagnose-mqtt
description: "Diagnose MQTT availability using Home Assistant observations or a bounded read-only TCP reachability check. Use when MQTT-backed entities are unavailable or stale, without publishing messages, using administrator credentials, or restarting the broker."
metadata:
  mode: read-only
  risk: low
  requires_approval: false
---

# Diagnose MQTT

## Diagnose safely

1. Prefer current Home Assistant diagnostic states when the read-only adapter is
   configured.
2. Otherwise perform only a bounded TCP check against the configured MQTT host
   and port.
3. Compare entity freshness and availability before attributing the problem to
   the broker.
4. Classify the result as `not_configured`, `ha_data_stale`, `host_unreachable`,
   `port_closed`, `broker_reachable`, or `unknown`.

Report observations and missing facts in Russian. Do not connect with
administrative credentials, subscribe, publish, retain, acknowledge, restart,
reconfigure, or inspect arbitrary broker topics.
