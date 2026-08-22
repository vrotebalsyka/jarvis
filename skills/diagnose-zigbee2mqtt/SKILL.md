---
name: diagnose-zigbee2mqtt
description: "Diagnose Zigbee2MQTT from read-only Home Assistant entity state, freshness, unavailable counts, MQTT reachability, and approved diagnostic sensors. Use when Zigbee devices are stale or unavailable without touching USB hardware or restarting Zigbee2MQTT."
metadata:
  mode: read-only
  risk: low
  requires_approval: false
---

# Diagnose Zigbee2MQTT

## Diagnose safely

1. Read only allowed Home Assistant entities and their timestamps.
2. Count unavailable and stale Zigbee-backed entities without exposing private
   attributes.
3. Use the MQTT diagnostic skill for broker reachability rather than publishing
   a probe.
4. Read only approved coordinator or bridge diagnostic sensors when present.
5. Distinguish `not_configured`, `ha_unavailable`, `mqtt_unreachable`,
   `bridge_stale`, `devices_partially_unavailable`, and `healthy`.

Report confirmed facts, observation time, affected components, and a read-only
next check. Never restart Zigbee2MQTT, reset or access the USB coordinator,
permit joins, re-pair devices, publish MQTT messages, or call HA services.
