#!/usr/bin/env python3
"""Assess every inventoried HA physical device and integration without actions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import time
from pathlib import Path
from typing import Any, Callable


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_inventory as ha_inventory  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402
import incident_monitor  # noqa: E402


CURSOR_NAME = "device_health_v1"
MAX_INVENTORY_BYTES = 8 * 1_048_576
INTEGRATION_CONFIRM_AFTER_SECONDS = 15
INTEGRATION_DISPLAY_NAMES = {
    "homeassistant": "Home Assistant",
    "localtuya": "LocalTuya",
    "midea_ac_lan": "Midea AC LAN",
    "tuya": "Tuya",
    "tuya_local": "Tuya Local",
    "xiaomi_miot": "Xiaomi Miot",
    "yandex_smart_home": "Яндекс Умный дом",
    "yandex_station": "Яндекс Станция",
}


class DeviceHealthError(RuntimeError):
    """Secret-free universal health assessment failure."""


def load_inventory(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise DeviceHealthError("device inventory is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
        or not 0 < metadata.st_size <= MAX_INVENTORY_BYTES
    ):
        raise DeviceHealthError("device inventory is unsafe")
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            raw = os.read(descriptor, MAX_INVENTORY_BYTES + 1)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise DeviceHealthError("device inventory is unavailable") from error
    if len(raw) > MAX_INVENTORY_BYTES:
        raise DeviceHealthError("device inventory is invalid")
    try:
        document = ha_read.strict_json_loads(raw)
    except ha_read.AdapterError as error:
        raise DeviceHealthError("device inventory is invalid") from error
    if not isinstance(document, dict):
        raise DeviceHealthError("device inventory is invalid")
    return document


def _profile_map(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    profiles = inventory.get("integration_profiles")
    if not isinstance(profiles, list) or len(profiles) > 512:
        raise DeviceHealthError("integration profiles are unavailable")
    result: dict[str, dict[str, Any]] = {}
    for item in profiles:
        if not isinstance(item, dict):
            raise DeviceHealthError("integration profiles are invalid")
        domain = item.get("domain")
        if not isinstance(domain, str) or not re.fullmatch(
            r"[a-z0-9_]{1,64}", domain
        ):
            raise DeviceHealthError("integration profiles are invalid")
        result[domain] = item
    return result


def read_live_integration_counts(
    config: ha_read.AdapterConfig,
    *,
    connector: Callable[[ha_read.AdapterConfig], Any] = incident_monitor._connect,
    command: Callable[[Any, int, str], Any] = ha_inventory._command,
) -> dict[str, tuple[int, int]]:
    """Read only sanitized config-entry counts; never persist entry identifiers."""
    socket = connector(config)
    try:
        incident_monitor.authenticate(socket, config.token)
        try:
            entries = command(socket, 40, "config_entries/get")
        except ha_inventory.InventoryError as error:
            raise DeviceHealthError("integration state response is unavailable") from error
    finally:
        try:
            socket.close()
        except Exception:
            pass
    if not isinstance(entries, list) or len(entries) > 4096:
        raise DeviceHealthError("integration state response is invalid")
    counts: dict[str, list[int]] = {}
    for item in entries:
        if not isinstance(item, dict):
            raise DeviceHealthError("integration state response is invalid")
        domain = item.get("domain")
        state = item.get("state")
        if (
            not isinstance(domain, str)
            or re.fullmatch(r"[a-z0-9_]{1,64}", domain) is None
            or not isinstance(state, str)
        ):
            raise DeviceHealthError("integration state response is invalid")
        values = counts.setdefault(domain, [0, 0])
        values[0] += 1
        values[1] += int(state == "loaded")
    return {domain: (values[0], values[1]) for domain, values in counts.items()}


def _apply_live_integration_counts(
    profiles: dict[str, dict[str, Any]],
    counts: dict[str, tuple[int, int]],
) -> dict[str, dict[str, Any]]:
    result = {domain: dict(profile) for domain, profile in profiles.items()}
    for domain in set(result) | set(counts):
        if re.fullmatch(r"[a-z0-9_]{1,64}", domain) is None:
            raise DeviceHealthError("integration profiles are invalid")
        entry_count, loaded_count = counts.get(domain, (0, 0))
        if entry_count < 0 or not 0 <= loaded_count <= entry_count:
            raise DeviceHealthError("integration state response is invalid")
        profile = result.setdefault(domain, {"domain": domain})
        profile["entry_count"] = entry_count
        profile["loaded_entry_count"] = loaded_count
    return result


def _integration_display_name(domain: str) -> str:
    return INTEGRATION_DISPLAY_NAMES.get(
        domain, " ".join(part.capitalize() for part in domain.split("_"))
    )


def _snapshot_index(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entities = snapshot.get("entities")
    if not isinstance(entities, list) or len(entities) > 4096:
        raise DeviceHealthError("Home Assistant health snapshot failed")
    result: dict[str, dict[str, Any]] = {}
    for item in entities:
        if not isinstance(item, dict):
            raise DeviceHealthError("Home Assistant health snapshot failed")
        entity_id = item.get("entity_id")
        try:
            normalized = ha_read._validate_entity_id(entity_id)
        except ha_read.AdapterError as error:
            raise DeviceHealthError("Home Assistant health snapshot failed") from error
        result[normalized] = item
    return result


def assess_device(
    device: dict[str, Any],
    states: dict[str, dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
) -> dict[str, object]:
    physical_hash = device.get("physical_device_hash")
    display_name = ha_read.sanitize_friendly_name(device.get("display_name"))
    entity_ids = device.get("entity_ids")
    config_domains = device.get("config_domains")
    safety_class = device.get("safety_class")
    network_status = device.get("network_status")
    network_miss_count = device.get("network_miss_count", 0)
    if (
        not isinstance(physical_hash, str)
        or not re.fullmatch(r"[a-f0-9]{64}", physical_hash)
        or display_name is None
        or not isinstance(entity_ids, list)
        or not 1 <= len(entity_ids) <= 512
        or not isinstance(config_domains, list)
        or safety_class not in incident_monitor.SAFETY_CLASSES
        or network_status not in {
            "stable", "ip_changed", "not_observed", "unknown"
        }
        or not isinstance(network_miss_count, int)
        or isinstance(network_miss_count, bool)
        or not 0 <= network_miss_count <= 1000
    ):
        raise DeviceHealthError("physical device inventory is invalid")
    normalized_ids = [ha_read._validate_entity_id(value) for value in entity_ids]
    kinds = [
        str(states.get(entity_id, {}).get("state_kind", "unavailable"))
        for entity_id in normalized_ids
    ]
    available = sum(kind not in {"unavailable", "redacted"} for kind in kinds)
    unavailable = sum(kind == "unavailable" for kind in kinds)
    domains = [
        value for value in config_domains
        if isinstance(value, str) and value in profiles
    ]
    integration_unhealthy = any(
        int(profiles[domain].get("entry_count", 0)) >
        int(profiles[domain].get("loaded_entry_count", 0))
        for domain in domains
    )
    platforms = device.get("platforms")
    platform_set = {
        value for value in platforms if isinstance(value, str)
    } if isinstance(platforms, list) else set()
    network_outage_confirmed = (
        network_status == "not_observed"
        and network_miss_count >= 3
        and available == 0
        and unavailable > 0
    )
    if integration_unhealthy:
        health_status = "degraded"
        cause_code = "integration_not_loaded"
        confidence = "confirmed"
    elif network_outage_confirmed:
        health_status = "offline"
        cause_code = "device_not_observed_on_lan"
        confidence = "confirmed"
    elif network_status == "ip_changed" and available == 0:
        health_status = "degraded"
        cause_code = "confirmed_ip_change"
        confidence = "confirmed"
    elif available == 0 and unavailable:
        health_status = "degraded"
        cause_code = (
            "tuya_integration_unavailable"
            if platform_set & {"tuya", "tuya_local", "localtuya"}
            else "integration_unavailable"
        )
        confidence = "probable"
    elif unavailable:
        health_status = "partial"
        cause_code = "partial_entity_unavailable"
        confidence = "confirmed"
    elif network_status == "not_observed":
        # Network Scanner is a sampling signal, not proof of an outage. If HA
        # still answers, keep the device healthy and only retain the miss as
        # diagnostic context for the model and incident journal.
        health_status = "healthy"
        cause_code = "unknown"
        confidence = "unknown"
    elif network_status == "ip_changed":
        health_status = "partial"
        cause_code = "confirmed_ip_change"
        confidence = "confirmed"
    else:
        health_status = "healthy"
        cause_code = "unknown"
        confidence = "unknown"
    return {
        "physical_device_hash": physical_hash,
        "display_name": display_name,
        "health_status": health_status,
        "cause_code": cause_code,
        "cause_confidence": confidence,
        "safety_class": safety_class,
        "network_status": network_status,
        "network_miss_count": network_miss_count,
        "network_outage_confirmed": network_outage_confirmed,
        "entity_count": len(normalized_ids),
        "available_entity_count": available,
        "unavailable_entity_count": unavailable,
        "representative_subject": normalized_ids[0],
    }


def run_once(
    store: incident_monitor.IncidentStore,
    inventory: dict[str, Any],
    *,
    observed_epoch: int,
    snapshot_reader: Callable[[str], tuple[dict[str, Any], int]] = ha_read.execute_safely,
    integration_reader: Callable[[], dict[str, tuple[int, int]]] | None = None,
) -> dict[str, int]:
    if observed_epoch < 0:
        raise DeviceHealthError("invalid device health time")
    snapshot, exit_code = snapshot_reader("snapshot")
    if exit_code != 0:
        raise DeviceHealthError("Home Assistant health snapshot failed")
    states = _snapshot_index(snapshot)
    profiles = _profile_map(inventory)
    if integration_reader is not None:
        profiles = _apply_live_integration_counts(profiles, integration_reader())
    devices = inventory.get("physical_devices")
    if not isinstance(devices, list) or len(devices) > 4096:
        raise DeviceHealthError("physical device inventory is invalid")
    baseline = not store.diagnostic_cursor_exists(CURSOR_NAME)
    counts = {
        "devices": 0, "healthy": 0, "partial": 0,
        "degraded": 0, "offline": 0, "changed": 0,
        "integration_incidents": 0,
    }
    for device in devices:
        if not isinstance(device, dict):
            raise DeviceHealthError("physical device inventory is invalid")
        assessment = assess_device(device, states, profiles)
        status = str(assessment["health_status"])
        counts["devices"] += 1
        counts[status] += 1
        event_type = store.record_device_health(
            physical_device_hash=str(assessment["physical_device_hash"]),
            display_name=str(assessment["display_name"]),
            health_status=status,
            cause_code=str(assessment["cause_code"]),
            cause_confidence=str(assessment["cause_confidence"]),
            safety_class=str(assessment["safety_class"]),
            network_status=str(assessment["network_status"]),
            entity_count=int(assessment["entity_count"]),
            available_entity_count=int(assessment["available_entity_count"]),
            unavailable_entity_count=int(assessment["unavailable_entity_count"]),
            observed_epoch=observed_epoch,
            baseline=baseline,
        )
        counts["changed"] += int(event_type in {"changed", "recovered"})
        incident_network_status = str(assessment["network_status"])
        if (
            incident_network_status == "not_observed"
            and assessment["network_outage_confirmed"] is not True
        ):
            incident_network_status = "unknown"
        network_event = store.observe_network_device_incident(
            physical_device_hash=str(assessment["physical_device_hash"]),
            representative_subject=str(assessment["representative_subject"]),
            display_name=str(assessment["display_name"]),
            network_status=incident_network_status,
            cause_code=(
                str(assessment["cause_code"])
                if assessment["cause_code"] in {
                    "device_not_observed_on_lan", "confirmed_ip_change"
                }
                else "unknown"
            ),
            cause_confidence=str(assessment["cause_confidence"]),
            safety_class=str(assessment["safety_class"]),
            observed_epoch=observed_epoch,
            baseline=baseline,
        )
        counts["changed"] += int(network_event in {"confirmed", "resolved"})
        if status != "healthy":
            store.diagnose_device_incident_for_subject(
                str(assessment["representative_subject"]),
                cause_code=str(assessment["cause_code"]),
                cause_confidence=str(assessment["cause_confidence"]),
                evidence_code="universal_device_health",
            )

    for domain, profile in profiles.items():
        entry_count = int(profile.get("entry_count", 0))
        loaded_count = int(profile.get("loaded_entry_count", 0))
        integration_event = store.record_integration_health(
            domain=domain,
            entry_count=entry_count,
            loaded_entry_count=loaded_count,
            observed_epoch=observed_epoch,
            baseline=baseline,
        )
        if not entry_count or loaded_count == entry_count:
            for candidate in store.operational_incident_candidates():
                if (
                    candidate.get("source_type") == "integration"
                    and candidate.get("automation_entity_id") == domain
                    and candidate.get("cause_code") == "integration_not_loaded"
                ):
                    store.resolve_operational_incident(
                        int(candidate["incident_id"]),
                        observed_epoch,
                        "integration_healthy",
                    )
            continue
        failure_epoch = store.confirmed_integration_failure_epoch(
            domain,
            observed_epoch,
            confirm_after_seconds=INTEGRATION_CONFIRM_AFTER_SECONDS,
        )
        if failure_epoch is None:
            continue
        event_seed = json.dumps(
            {
                "domain": domain,
                "event": "confirmed_degraded",
                "observed_epoch": failure_epoch,
                "status": "degraded",
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        event_hash = hashlib.sha256(event_seed.encode("ascii")).hexdigest()
        failure = store.record_operational_failure(
            event_hash=event_hash,
            source_type="integration",
            source_ref=domain,
            observed_epoch=failure_epoch,
            error_code="integration_not_loaded",
            cause_code="integration_not_loaded",
            cause_confidence="confirmed",
            action_code="integration.health",
            target_entity_id=None,
            display_name=_integration_display_name(domain),
            evidence_code="config_entry_state",
            baseline=baseline,
        )
        counts["integration_incidents"] += int(
            failure["incident_id"] is not None
        )
    store.mark_diagnostic_cursor(CURSOR_NAME, observed_epoch)
    return counts


def main() -> int:
    state_dir = incident_monitor._state_dir()
    try:
        incident_monitor._validate_directory(state_dir)
        store = incident_monitor.IncidentStore(
            state_dir / incident_monitor.DATABASE_NAME
        )
        try:
            result = run_once(
                store,
                load_inventory(state_dir / ha_inventory.INVENTORY_NAME),
                observed_epoch=int(time.time()),
                integration_reader=lambda: read_live_integration_counts(
                    ha_read.load_config()
                ),
            )
        finally:
            store.close()
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 0
    except (
        DeviceHealthError,
        incident_monitor.MonitorError,
        ha_read.AdapterError,
        OSError,
        sqlite3.Error,
    ):
        print("HOME_ASSISTANT_DEVICE_HEALTH_FAILED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
