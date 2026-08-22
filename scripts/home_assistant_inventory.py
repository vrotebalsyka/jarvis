#!/usr/bin/env python3
"""Build a private sanitized HA integration and LAN identity inventory."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import sqlite3
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_read as ha_read  # noqa: E402
import incident_monitor  # noqa: E402


INVENTORY_NAME = "inventory.json"
PLATFORM_RE = re.compile(r"^[a-z0-9_]{1,64}$")
DEVICE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
ENTRY_ID_RE = re.compile(r"^(?:[A-Z0-9]{26}|[a-f0-9]{32})$")
MAC_RE = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")
LOCAL_NETWORK = ipaddress.ip_network("192.168.1.0/24")
MAX_COMMAND_MESSAGES = 64
MAX_DIAGNOSTICS_BYTES = 8 * 1_048_576
MAX_INVENTORY_BYTES = 8 * 1_048_576
TUYA_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
TUYA_REGISTRY_PLATFORMS = {"tuya", "tuya_local", "localtuya"}
XIAOMI_IDENTIFIER_RE = re.compile(
    r"^((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})-[A-Za-z0-9_.-]{3,64}$"
)
VERSION_RE = re.compile(r"^v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})$")
LOCAL_TUYA_UPDATE_ENTITY_ID = "update.local_tuya_update"
TUYA_LOCAL_UPDATE_ENTITY_ID = "update.tuya_local_update"
XIAOMI_MIOT_UPDATE_ENTITY_ID = "update.xiaomi_miot_update"
REVIEWED_LOCAL_TUYA_VERSION = "v5.2.5"
TUYA_LOCAL_IP_REPAIR_VERSION = "2026.7.2"
TUYA_LOCAL_MINIMUM_CORE_VERSION = "2026.6.0"
REVIEWED_XIAOMI_MIOT_VERSION = "v1.1.4"
RECENT_BACKUP_SECONDS = 24 * 60 * 60
RESTRICTED_DEVICE_DOMAINS = {
    "alarm_control_panel", "climate", "lock", "valve", "water_heater",
}
RESTRICTED_DEVICE_PLATFORMS = {"midea_ac_lan"}
RECOVERY_MODES = {
    "localtuya": ("local_rebind_reload", True),
    "tuya_local": ("entry_reload", True),
    "midea_ac_lan": ("idle_entry_reload", True),
    "yandex_station": ("cloud_backoff_entry_reload", True),
    "yandex_smart_home": ("cloud_backoff_entry_reload", True),
    "xiaomi_miot": ("permissioned_entry_reload", False),
    "tuya": ("cloud_backoff", False),
}


class InventoryError(RuntimeError):
    """Secret-free inventory failure."""


def _version_tuple(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = VERSION_RE.fullmatch(value)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def _update_versions(raw_states: Any, entity_id: str) -> tuple[str | None, str | None]:
    if not isinstance(raw_states, list):
        return None, None
    matches = [
        item for item in raw_states
        if isinstance(item, dict) and item.get("entity_id") == entity_id
    ]
    if len(matches) != 1:
        return None, None
    attributes = matches[0].get("attributes")
    if not isinstance(attributes, dict):
        return None, None
    installed = attributes.get("installed_version")
    latest = attributes.get("latest_version")
    if _version_tuple(installed) is None or _version_tuple(latest) is None:
        return None, None
    return installed, latest


def _integration_capabilities(
    raw_states: Any,
    core_config: Any,
) -> dict[str, object]:
    """Return reviewed, private capability facts without integration secrets."""
    core_version = core_config.get("version") if isinstance(core_config, dict) else None
    if _version_tuple(core_version) is None:
        core_version = None

    local_installed, local_latest = _update_versions(
        raw_states, LOCAL_TUYA_UPDATE_ENTITY_ID
    )
    local_reviewed = local_installed == REVIEWED_LOCAL_TUYA_VERSION
    localtuya = {
        "installed_version": local_installed,
        "latest_version": local_latest,
        "ip_recovery_mode": (
            "stable_id_udp_auto_update" if local_reviewed else "review_required"
        ),
        "review_status": "reviewed" if local_reviewed else "review_required",
    }

    tuya_installed, tuya_latest = _update_versions(
        raw_states, TUYA_LOCAL_UPDATE_ENTITY_ID
    )
    installed_tuple = _version_tuple(tuya_installed)
    repair_tuple = _version_tuple(TUYA_LOCAL_IP_REPAIR_VERSION)
    core_tuple = _version_tuple(core_version)
    minimum_core_tuple = _version_tuple(TUYA_LOCAL_MINIMUM_CORE_VERSION)
    automatic_repair = (
        installed_tuple is not None
        and repair_tuple is not None
        and installed_tuple >= repair_tuple
    )
    if automatic_repair:
        upgrade_status = "automatic_ip_recovery_available"
    elif (
        tuya_latest == TUYA_LOCAL_IP_REPAIR_VERSION
        and core_tuple is not None
        and minimum_core_tuple is not None
        and core_tuple < minimum_core_tuple
    ):
        upgrade_status = "core_upgrade_required"
    elif tuya_latest == TUYA_LOCAL_IP_REPAIR_VERSION and core_tuple is not None:
        upgrade_status = "backup_required_before_update"
    else:
        upgrade_status = "review_required"
    tuya_local = {
        "installed_version": tuya_installed,
        "latest_version": tuya_latest,
        "core_version": core_version,
        "automatic_ip_recovery": automatic_repair,
        "reviewed_target_version": TUYA_LOCAL_IP_REPAIR_VERSION,
        "minimum_core_version": TUYA_LOCAL_MINIMUM_CORE_VERSION,
        "upgrade_status": upgrade_status,
    }

    xiaomi_installed, xiaomi_latest = _update_versions(
        raw_states, XIAOMI_MIOT_UPDATE_ENTITY_ID
    )
    xiaomi_reviewed = xiaomi_installed == REVIEWED_XIAOMI_MIOT_VERSION
    xiaomi_miot = {
        "installed_version": xiaomi_installed,
        "latest_version": xiaomi_latest,
        "reviewed_version": REVIEWED_XIAOMI_MIOT_VERSION,
        "bounded_config_entry_reload": xiaomi_reviewed,
        "review_status": "reviewed" if xiaomi_reviewed else "review_required",
        "automatic_recovery_enabled": False,
    }
    return {
        "localtuya": localtuya,
        "tuya_local": tuya_local,
        "xiaomi_miot": xiaomi_miot,
    }


def _backup_readiness(
    backup_info: Any,
    core_version: str | None,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Reduce admin-only backup metadata to a secret-free upgrade preflight."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not isinstance(backup_info, dict):
        return {"status": "unavailable", "restore_tested": False}
    agent_errors = backup_info.get("agent_errors")
    backups = backup_info.get("backups")
    if (
        backup_info.get("state") != "idle"
        or not isinstance(agent_errors, dict)
        or agent_errors
        or not isinstance(backups, list)
        or len(backups) > 1_024
    ):
        return {"status": "not_ready", "restore_tested": False}
    valid: list[tuple[datetime, dict[str, Any]]] = []
    for item in backups:
        if not isinstance(item, dict):
            continue
        try:
            date = datetime.fromisoformat(str(item.get("date")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if date.tzinfo is None:
            continue
        date = date.astimezone(timezone.utc)
        failed_lists = (
            item.get("failed_agent_ids", []),
            item.get("failed_addons", []),
            item.get("failed_folders", []),
        )
        if (
            date > current
            or item.get("homeassistant_version") != core_version
            or item.get("homeassistant_included") is not True
            or item.get("database_included") is not True
            or any(not isinstance(value, list) or value for value in failed_lists)
        ):
            continue
        valid.append((date, item))
    if not valid:
        return {"status": "missing_complete_backup", "restore_tested": False}
    latest, _item = max(valid, key=lambda pair: pair[0])
    age_seconds = int((current - latest).total_seconds())
    return {
        "status": (
            "recent_complete_backup"
            if age_seconds <= RECENT_BACKUP_SECONDS
            else "stale_complete_backup"
        ),
        "latest_backup_at": latest.isoformat(timespec="seconds"),
        "core_version": core_version,
        "age_seconds": age_seconds,
        "restore_tested": False,
    }


def _command(socket: Any, identifier: int, command_type: str) -> Any:
    socket.send(
        json.dumps(
            {"id": identifier, "type": command_type},
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )
    for _attempt in range(MAX_COMMAND_MESSAGES):
        response = incident_monitor._message(socket.recv())
        if response.get("id") != identifier:
            continue
        if response.get("type") != "result" or response.get("success") is not True:
            raise InventoryError("Home Assistant inventory command failed")
        return response.get("result")
    raise InventoryError("Home Assistant inventory response is missing")


def _valid_entry_id(value: Any) -> str | None:
    return value if isinstance(value, str) and ENTRY_ID_RE.fullmatch(value) else None


def _vendor_kind(value: Any) -> str:
    text = value.casefold() if isinstance(value, str) else ""
    if "tuya" in text:
        return "tuya"
    if "midea" in text:
        return "midea"
    if "xiaomi" in text:
        return "xiaomi"
    if "espressif" in text:
        return "espressif"
    return "other"


def _registry_mac(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.replace("-", ":").upper()
    return normalized if MAC_RE.fullmatch(normalized) else None


def _device_registry_macs(connections: Any) -> set[str]:
    if not isinstance(connections, list) or len(connections) > 64:
        return set()
    result: set[str] = set()
    for pair in connections:
        if not isinstance(pair, list) or len(pair) != 2:
            continue
        if str(pair[0]).casefold() != "mac":
            continue
        mac = _registry_mac(pair[1])
        if mac is not None:
            result.add(mac)
    return result


def _build_device_network_bindings(
    device_macs: dict[str, set[str]],
    device_physical_hashes: dict[str, str],
    device_entries: dict[str, list[str]],
    network: list[dict[str, str]],
    previous_bindings: Any,
) -> list[dict[str, object]]:
    """Bind any unambiguous HA registry MAC to the private LAN scanner."""
    current_by_mac = {item["mac"]: item["ip"] for item in network}
    previous_by_hash: dict[str, dict[str, object]] = {}
    if isinstance(previous_bindings, list):
        for item in previous_bindings:
            if not isinstance(item, dict):
                continue
            physical_hash = item.get("physical_device_hash")
            if isinstance(physical_hash, str) and re.fullmatch(
                r"[a-f0-9]{64}", physical_hash
            ):
                previous_by_hash[physical_hash] = item

    grouped: dict[str, dict[str, object]] = {}
    for device_id, macs in device_macs.items():
        physical_hash = device_physical_hashes.get(device_id)
        if physical_hash is None:
            continue
        item = grouped.setdefault(
            physical_hash,
            {"device_ids": set(), "config_entry_ids": set(), "macs": set()},
        )
        item["device_ids"].add(device_id)  # type: ignore[union-attr]
        item["config_entry_ids"].update(  # type: ignore[union-attr]
            device_entries.get(device_id, [])
        )
        item["macs"].update(macs)  # type: ignore[union-attr]

    bindings: list[dict[str, object]] = []
    for physical_hash, item in grouped.items():
        macs = item["macs"]
        if not isinstance(macs, set) or len(macs) != 1:
            continue
        mac = next(iter(macs))
        observed_ip = current_by_mac.get(mac)
        previous = previous_by_hash.get(physical_hash, {})
        previous_ip = previous.get("observed_ip") or previous.get("previous_ip")
        previous_misses = previous.get("network_miss_count", 0)
        if not isinstance(previous_misses, int) or isinstance(previous_misses, bool):
            previous_misses = 0
        status = "not_observed"
        network_miss_count = min(1000, previous_misses + 1)
        if observed_ip is not None:
            status = (
                "ip_changed"
                if isinstance(previous_ip, str) and previous_ip != observed_ip
                else "stable"
            )
            network_miss_count = 0
        bindings.append(
            {
                "physical_device_hash": physical_hash,
                "device_ids": sorted(item["device_ids"]),
                "config_entry_ids": sorted(item["config_entry_ids"]),
                "mac": mac,
                "observed_ip": observed_ip,
                "previous_ip": previous_ip if isinstance(previous_ip, str) else None,
                "status": status,
                "network_miss_count": network_miss_count,
            }
        )
    return sorted(bindings, key=lambda item: str(item["physical_device_hash"]))


def _merge_identity_network_bindings(
    bindings: list[dict[str, object]],
    identity_bindings: list[dict[str, object]],
    device_physical_hashes: dict[str, str],
    device_entries: dict[str, list[str]],
) -> list[dict[str, object]]:
    """Add integration-proven IP/MAC identities to the physical-device map."""
    merged = {str(item["physical_device_hash"]): dict(item) for item in bindings}
    for identity in identity_bindings:
        device_id = identity.get("device_id")
        mac = identity.get("mac")
        if (
            not isinstance(device_id, str)
            or device_id not in device_physical_hashes
            or not isinstance(mac, str)
            or MAC_RE.fullmatch(mac) is None
        ):
            continue
        physical_hash = device_physical_hashes[device_id]
        existing = merged.get(physical_hash)
        if existing is not None and existing.get("mac") != mac:
            # Multiple hardware identities for one HA aggregate are ambiguous.
            continue
        observed_ip = identity.get("observed_ip")
        configured_ip = identity.get("configured_ip")
        previous_ip = (
            existing.get("previous_ip") if existing is not None else configured_ip
        )
        status = identity.get("status")
        if status not in {"stable", "ip_changed", "not_observed"}:
            status = "stable" if isinstance(observed_ip, str) else "not_observed"
        network_miss_count = identity.get("network_miss_count")
        if not isinstance(network_miss_count, int) or isinstance(network_miss_count, bool):
            network_miss_count = (
                existing.get("network_miss_count", 0)
                if existing is not None else 0
            )
        if not isinstance(network_miss_count, int) or isinstance(network_miss_count, bool):
            network_miss_count = 0
        entry_ids = set(device_entries.get(device_id, []))
        identity_entry = identity.get("config_entry_id")
        if isinstance(identity_entry, str):
            entry_ids.add(identity_entry)
        device_ids = {device_id}
        if existing is not None:
            device_ids.update(
                value for value in existing.get("device_ids", [])
                if isinstance(value, str)
            )
            entry_ids.update(
                value for value in existing.get("config_entry_ids", [])
                if isinstance(value, str)
            )
        merged[physical_hash] = {
            "physical_device_hash": physical_hash,
            "device_ids": sorted(device_ids),
            "config_entry_ids": sorted(entry_ids),
            "mac": mac,
            "observed_ip": observed_ip if isinstance(observed_ip, str) else None,
            "previous_ip": previous_ip if isinstance(previous_ip, str) else None,
            "status": status,
            "network_miss_count": network_miss_count,
        }
    return sorted(merged.values(), key=lambda item: str(item["physical_device_hash"]))


def _device_safety_class(entity_ids: list[str], platforms: set[str]) -> str:
    domains = {entity_id.split(".", 1)[0] for entity_id in entity_ids}
    if domains & RESTRICTED_DEVICE_DOMAINS or platforms & RESTRICTED_DEVICE_PLATFORMS:
        return "restricted"
    if "light" in domains:
        return "light"
    if "switch" in domains:
        return "ordinary_relay"
    if domains and domains <= {"sensor", "binary_sensor", "number", "select", "update"}:
        return "sensor"
    return "unknown"


def _common_display_name(entities: list[dict[str, object]]) -> str:
    names = [
        str(item["friendly_name"])
        for item in entities
        if isinstance(item.get("friendly_name"), str)
    ]
    if names:
        tokenized = [name.split() for name in names]
        shared: list[str] = []
        for values in zip(*tokenized):
            if len({value.casefold() for value in values}) != 1:
                break
            shared.append(values[0])
        if shared:
            return " ".join(shared)[:100]
        return min(names, key=lambda value: (len(value), value.casefold()))[:100]
    entity_ids = [str(item["entity_id"]) for item in entities]
    return incident_monitor._device_display_name(entity_ids)


def _integration_profiles(
    entries: dict[str, dict[str, object]], entity_platforms: set[str]
) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for item in entries.values():
        grouped.setdefault(str(item["domain"]), []).append(item)
    for platform in entity_platforms:
        grouped.setdefault(platform, [])
    profiles: list[dict[str, object]] = []
    for domain, domain_entries in sorted(grouped.items()):
        recovery_mode, automatic = RECOVERY_MODES.get(
            domain, ("diagnose_only", False)
        )
        loaded = sum(item.get("state") == "loaded" for item in domain_entries)
        unloadable = sum(item.get("supports_unload") is True for item in domain_entries)
        profiles.append(
            {
                "domain": domain,
                "entry_count": len(domain_entries),
                "loaded_entry_count": loaded,
                "unloadable_entry_count": unloadable,
                "recovery_mode": recovery_mode,
                "automatic_recovery_allowed": bool(
                    automatic
                    and domain_entries
                    and unloadable == len(domain_entries)
                ),
            }
        )
    return profiles


def _physical_devices(
    entities: list[dict[str, object]],
    entries: dict[str, dict[str, object]],
    network_bindings: list[dict[str, object]],
) -> list[dict[str, object]]:
    network_by_hash = {
        str(item["physical_device_hash"]): item for item in network_bindings
    }
    groups: dict[str, list[dict[str, object]]] = {}
    for item in entities:
        physical_hash = item.get("physical_device_hash")
        if isinstance(physical_hash, str):
            groups.setdefault(physical_hash, []).append(item)
    result: list[dict[str, object]] = []
    for physical_hash, members in sorted(groups.items()):
        entity_ids = sorted(str(item["entity_id"]) for item in members)
        platforms = {str(item["platform"]) for item in members}
        entry_ids = sorted({
            str(entry_id)
            for item in members
            for entry_id in item.get("config_entry_ids", [])
            if isinstance(entry_id, str)
        })
        config_domains = sorted({
            str(entries[entry_id]["domain"])
            for entry_id in entry_ids if entry_id in entries
        })
        kinds = [str(item.get("state_kind", "absent")) for item in members]
        network = network_by_hash.get(physical_hash)
        result.append(
            {
                "physical_device_hash": physical_hash,
                "display_name": _common_display_name(members),
                "entity_ids": entity_ids,
                "entity_count": len(entity_ids),
                "available_entity_count": sum(
                    kind not in {"unavailable", "absent", "redacted"}
                    for kind in kinds
                ),
                "unavailable_entity_count": sum(
                    kind in {"unavailable", "absent"} for kind in kinds
                ),
                "platforms": sorted(platforms),
                "config_entry_ids": entry_ids,
                "config_domains": config_domains,
                "safety_class": _device_safety_class(entity_ids, platforms),
                "network_status": (
                    str(network["status"]) if network is not None else "unknown"
                ),
                "network_miss_count": (
                    int(network.get("network_miss_count", 0))
                    if network is not None else 0
                ),
            }
        )
    return result


def _network_devices(raw_states: Any) -> list[dict[str, str]]:
    if not isinstance(raw_states, list):
        raise InventoryError("network scanner state is invalid")
    scanner = next(
        (
            item for item in raw_states
            if isinstance(item, dict) and item.get("entity_id") == "sensor.network_scanner"
        ),
        None,
    )
    if scanner is None:
        return []
    attributes = scanner.get("attributes")
    devices = attributes.get("devices") if isinstance(attributes, dict) else None
    if not isinstance(devices, list) or len(devices) > 1_024:
        raise InventoryError("network scanner state is invalid")
    sanitized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in devices:
        if not isinstance(item, dict):
            raise InventoryError("network scanner state is invalid")
        ip_text = item.get("ip")
        mac_text = item.get("mac")
        if not isinstance(ip_text, str) or not isinstance(mac_text, str):
            continue
        try:
            address = ipaddress.ip_address(ip_text)
        except ValueError:
            continue
        normalized_mac = mac_text.upper()
        if address not in LOCAL_NETWORK or not MAC_RE.fullmatch(normalized_mac):
            continue
        identity = (str(address), normalized_mac)
        if identity in seen:
            continue
        seen.add(identity)
        sanitized.append(
            {
                "ip": str(address),
                "mac": normalized_mac,
                "vendor_kind": _vendor_kind(item.get("vendor")),
            }
        )
    return sorted(sanitized, key=lambda item: ipaddress.ip_address(item["ip"]))


def _identity_hash(platform: str, identifier: str) -> str:
    if platform not in {"localtuya", "tuya_local"} or not TUYA_ID_RE.fullmatch(identifier):
        raise InventoryError("Tuya identity is invalid")
    return hashlib.sha256(f"{platform}\0{identifier}".encode("ascii")).hexdigest()


def _registry_identity_hash(platform: str, identifier: str) -> str:
    """Normalize integration-owned registry identifiers before hashing."""
    if platform == "localtuya" and identifier.startswith("local_"):
        identifier = identifier.removeprefix("local_")
    return _identity_hash(platform, identifier)


def _normalized_tuya_identifier(platform: Any, identifier: Any) -> str | None:
    """Return one provider-independent Tuya identity without persisting it."""
    if (
        platform not in TUYA_REGISTRY_PLATFORMS
        or not isinstance(identifier, str)
    ):
        return None
    normalized = (
        identifier.removeprefix("local_")
        if platform == "localtuya" and identifier.startswith("local_")
        else identifier
    )
    return normalized if TUYA_ID_RE.fullmatch(normalized) else None


def _physical_device_hash(device_id: str, identifiers: Any) -> str:
    """Collapse duplicate HA registry devices that prove one Tuya identity."""
    if not DEVICE_ID_RE.fullmatch(device_id):
        raise InventoryError("device identity is invalid")
    tuya_hashes: set[str] = set()
    if isinstance(identifiers, list):
        for pair in identifiers:
            if not isinstance(pair, list) or len(pair) != 2:
                continue
            normalized = _normalized_tuya_identifier(pair[0], pair[1])
            if normalized is not None:
                tuya_hashes.add(
                    hashlib.sha256(
                        f"tuya-device\0{normalized}".encode("ascii")
                    ).hexdigest()
                )
    if len(tuya_hashes) == 1:
        return next(iter(tuya_hashes))
    return hashlib.sha256(f"ha-device\0{device_id}".encode("ascii")).hexdigest()


def _xiaomi_identity(identifier: Any) -> tuple[str, str] | None:
    """Reduce a Xiaomi registry identifier to a hash and normalized MAC."""
    if not isinstance(identifier, str):
        return None
    match = XIAOMI_IDENTIFIER_RE.fullmatch(identifier)
    if match is None:
        return None
    return (
        hashlib.sha256(f"xiaomi_miot\0{identifier}".encode("ascii")).hexdigest(),
        match.group(1).upper(),
    )


def _read_config_entry_diagnostics(
    config: ha_read.AdapterConfig,
    entry_id: str,
    *,
    connection_factory: Callable[[ha_read.AdapterConfig], http.client.HTTPConnection] = ha_read._default_connection,
) -> Any:
    valid_entry_id = _valid_entry_id(entry_id)
    if valid_entry_id is None:
        raise InventoryError("diagnostics entry is invalid")
    connection: http.client.HTTPConnection | None = None
    try:
        connection = connection_factory(config)
        connection.request(
            "GET",
            f"/api/diagnostics/config_entry/{valid_entry_id}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {config.token}",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        raw = response.read(MAX_DIAGNOSTICS_BYTES + 1)
        if response.status != 200 or len(raw) > MAX_DIAGNOSTICS_BYTES:
            raise InventoryError("Home Assistant diagnostics failed")
        document = ha_read.strict_json_loads(raw)
        if not isinstance(document, dict):
            raise InventoryError("Home Assistant diagnostics is invalid")
        return document
    except InventoryError:
        raise
    except (OSError, socket.timeout, TimeoutError, http.client.HTTPException, ha_read.AdapterError) as error:
        raise InventoryError("Home Assistant diagnostics failed") from error
    finally:
        if connection is not None:
            try:
                connection.close()
            except (OSError, http.client.HTTPException):
                pass


def _read_core_config(
    config: ha_read.AdapterConfig,
    *,
    connection_factory: Callable[[ha_read.AdapterConfig], http.client.HTTPConnection] = ha_read._default_connection,
) -> Any:
    """Read only the exact HA config endpoint for the private version preflight."""
    connection: http.client.HTTPConnection | None = None
    try:
        connection = connection_factory(config)
        connection.request(
            "GET",
            "/api/config",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {config.token}",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        raw = response.read(MAX_DIAGNOSTICS_BYTES + 1)
        if response.status != 200 or len(raw) > MAX_DIAGNOSTICS_BYTES:
            raise InventoryError("Home Assistant core config failed")
        document = ha_read.strict_json_loads(raw)
        if not isinstance(document, dict):
            raise InventoryError("Home Assistant core config is invalid")
        return document
    except InventoryError:
        raise
    except (OSError, socket.timeout, TimeoutError, http.client.HTTPException, ha_read.AdapterError) as error:
        raise InventoryError("Home Assistant core config failed") from error
    finally:
        if connection is not None:
            try:
                connection.close()
            except (OSError, http.client.HTTPException):
                pass


def _localtuya_diagnostic_hosts(document: Any) -> dict[str, str]:
    data = document.get("data") if isinstance(document, dict) else None
    devices = data.get("devices") if isinstance(data, dict) else None
    if not isinstance(devices, dict) or len(devices) > 1_024:
        return {}
    result: dict[str, str] = {}
    for raw_identifier, details in devices.items():
        if not isinstance(raw_identifier, str) or not TUYA_ID_RE.fullmatch(raw_identifier):
            continue
        host = details.get("host") if isinstance(details, dict) else None
        if not isinstance(host, str):
            continue
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            continue
        if address not in LOCAL_NETWORK:
            continue
        result[_identity_hash("localtuya", raw_identifier)] = str(address)
    return result


def _previous_mac_by_identity(bindings: Any) -> dict[str, str]:
    if not isinstance(bindings, list):
        return {}
    result: dict[str, str] = {}
    for item in bindings:
        if not isinstance(item, dict):
            continue
        identity_hash = item.get("identity_hash")
        mac = item.get("mac")
        if (
            isinstance(identity_hash, str)
            and re.fullmatch(r"[a-f0-9]{64}", identity_hash)
            and isinstance(mac, str)
            and MAC_RE.fullmatch(mac)
        ):
            result[identity_hash] = mac
    return result


def _build_identity_bindings(
    diagnostic_hosts: dict[str, tuple[str, str]],
    identity_devices: dict[str, str],
    network: list[dict[str, str]],
    previous_bindings: Any,
) -> list[dict[str, object]]:
    network_by_ip = {item["ip"]: item for item in network}
    network_by_mac = {item["mac"]: item for item in network}
    previous_macs = _previous_mac_by_identity(previous_bindings)
    previous_by_identity = {
        str(item["identity_hash"]): item
        for item in previous_bindings
        if isinstance(previous_bindings, list)
        and isinstance(item, dict)
        and isinstance(item.get("identity_hash"), str)
    } if isinstance(previous_bindings, list) else {}
    bindings: list[dict[str, object]] = []
    for identity_hash, (entry_id, configured_ip) in sorted(diagnostic_hosts.items()):
        device_id = identity_devices.get(identity_hash)
        if device_id is None:
            continue
        configured_network_device = network_by_ip.get(configured_ip)
        mac = (
            configured_network_device.get("mac")
            if configured_network_device is not None
            else previous_macs.get(identity_hash)
        )
        observed_ip = (
            network_by_mac[mac]["ip"]
            if isinstance(mac, str) and mac in network_by_mac
            else None
        )
        status = (
            "stable" if observed_ip == configured_ip
            else "ip_changed" if observed_ip is not None
            else "not_observed"
        )
        previous = previous_by_identity.get(identity_hash, {})
        previous_misses = previous.get("network_miss_count", 0)
        if not isinstance(previous_misses, int) or isinstance(previous_misses, bool):
            previous_misses = 0
        network_miss_count = (
            0 if observed_ip is not None else min(1000, previous_misses + 1)
        )
        bindings.append(
            {
                "identity_hash": identity_hash,
                "platform": "localtuya",
                "device_id": device_id,
                "config_entry_id": entry_id,
                "configured_ip": configured_ip,
                "observed_ip": observed_ip,
                "mac": mac,
                "status": status,
                "network_miss_count": network_miss_count,
            }
        )
    return bindings


def _build_xiaomi_bindings(
    identities: dict[str, dict[str, str]],
    entities: list[dict[str, object]],
    network: list[dict[str, str]],
    previous_bindings: Any,
) -> list[dict[str, object]]:
    """Track Xiaomi DHCP drift by registry MAC without exposing identifiers."""
    network_by_mac = {item["mac"]: item for item in network}
    available_devices = {
        str(item["device_id"])
        for item in entities
        if (
            item.get("platform") == "xiaomi_miot"
            and isinstance(item.get("device_id"), str)
            and item.get("state_kind")
            not in {"unavailable", "redacted", "absent"}
        )
    }
    prior_by_identity = {
        str(item["identity_hash"]): item
        for item in previous_bindings
        if (
            isinstance(previous_bindings, list)
            and isinstance(item, dict)
            and item.get("platform") == "xiaomi_miot"
            and isinstance(item.get("identity_hash"), str)
        )
    } if isinstance(previous_bindings, list) else {}
    result: list[dict[str, object]] = []
    for identity_hash, details in sorted(identities.items()):
        mac = details["mac"]
        observed = network_by_mac.get(mac)
        observed_ip = observed.get("ip") if observed is not None else None
        prior = prior_by_identity.get(identity_hash)
        configured_ip = prior.get("configured_ip") if isinstance(prior, dict) else None
        if isinstance(configured_ip, str):
            try:
                configured_address = ipaddress.ip_address(configured_ip)
            except ValueError:
                configured_ip = None
            else:
                if configured_address not in LOCAL_NETWORK:
                    configured_ip = None
        if configured_ip is None:
            if observed_ip is None:
                continue
            configured_ip = observed_ip
        if observed_ip is None:
            status = "not_observed"
        elif observed_ip == configured_ip:
            status = "stable"
        elif details["device_id"] in available_devices:
            configured_ip = observed_ip
            status = "stable"
        else:
            status = "ip_changed"
        previous_misses = (
            prior.get("network_miss_count", 0) if isinstance(prior, dict) else 0
        )
        if not isinstance(previous_misses, int) or isinstance(previous_misses, bool):
            previous_misses = 0
        network_miss_count = (
            0 if observed_ip is not None else min(1000, previous_misses + 1)
        )
        result.append({
            "identity_hash": identity_hash,
            "platform": "xiaomi_miot",
            "device_id": details["device_id"],
            "config_entry_id": details["config_entry_id"],
            "configured_ip": configured_ip,
            "observed_ip": observed_ip,
            "mac": mac,
            "status": status,
            "network_miss_count": network_miss_count,
        })
    return result


def _complete_identity_devices(
    diagnostic_hosts: dict[str, tuple[str, str]],
    identity_devices: dict[str, str],
    entities: list[dict[str, object]],
) -> dict[str, str]:
    """Complete LocalTuya identity links only for unambiguous config entries."""
    completed = dict(identity_devices)
    devices_by_entry: dict[str, set[str]] = {}
    for entity in entities:
        if entity.get("platform") != "localtuya":
            continue
        device_id = entity.get("device_id")
        entry_ids = entity.get("config_entry_ids")
        if not isinstance(device_id, str) or not isinstance(entry_ids, list):
            continue
        for entry_id in entry_ids:
            if isinstance(entry_id, str):
                devices_by_entry.setdefault(entry_id, set()).add(device_id)

    identities_by_entry: dict[str, set[str]] = {}
    for identity_hash, (entry_id, _host) in diagnostic_hosts.items():
        identities_by_entry.setdefault(entry_id, set()).add(identity_hash)

    for entry_id, identities in identities_by_entry.items():
        devices = devices_by_entry.get(entry_id, set())
        if len(identities) != 1 or len(devices) != 1:
            continue
        completed.setdefault(next(iter(identities)), next(iter(devices)))
    return completed


def collect_inventory(
    config: ha_read.AdapterConfig,
    *,
    connector: Callable[[ha_read.AdapterConfig], Any] = incident_monitor._connect,
    snapshot_reader: Callable[[str], tuple[dict[str, Any], int]] = ha_read.execute_safely,
    raw_state_reader: Callable[[ha_read.AdapterConfig, str], Any] = ha_read.request_json,
    diagnostics_reader: Callable[[ha_read.AdapterConfig, str], Any] = _read_config_entry_diagnostics,
    core_config_reader: Callable[[ha_read.AdapterConfig], Any] = _read_core_config,
    previous_bindings: Any = None,
    previous_device_network_bindings: Any = None,
) -> dict[str, object]:
    socket = connector(config)
    try:
        incident_monitor.authenticate(socket, config.token)
        display = _command(socket, 10, "config/entity_registry/list_for_display")
        devices = _command(socket, 11, "config/device_registry/list")
        config_entries = _command(socket, 12, "config_entries/get")
        backup_info = _command(socket, 13, "backup/info")
    finally:
        try:
            socket.close()
        except Exception:
            pass
    display_entities = display.get("entities") if isinstance(display, dict) else None
    if not isinstance(display_entities, list) or not isinstance(devices, list) or not isinstance(config_entries, list):
        raise InventoryError("Home Assistant inventory response is invalid")
    snapshot, exit_code = snapshot_reader("snapshot")
    snapshot_entities = snapshot.get("entities") if isinstance(snapshot, dict) else None
    if exit_code != 0 or not isinstance(snapshot_entities, list):
        raise InventoryError("Home Assistant inventory snapshot failed")
    snapshot_index = {
        item.get("entity_id"): item
        for item in snapshot_entities
        if isinstance(item, dict) and isinstance(item.get("entity_id"), str)
    }

    entries: dict[str, dict[str, object]] = {}
    for item in config_entries:
        if not isinstance(item, dict):
            raise InventoryError("Home Assistant config entry is invalid")
        entry_id = _valid_entry_id(item.get("entry_id"))
        domain = item.get("domain")
        if entry_id is None or not isinstance(domain, str) or not PLATFORM_RE.fullmatch(domain):
            continue
        state = item.get("state") if item.get("state") in {
            "loaded", "setup_error", "setup_retry", "not_loaded", "failed_unload",
            "migration_error", "setup_in_progress",
        } else "other"
        entries[entry_id] = {
            "entry_id": entry_id,
            "domain": domain,
            "state": state,
            "supports_reconfigure": item.get("supports_reconfigure") is True,
            "supports_options": item.get("supports_options") is True,
            "supports_unload": item.get("supports_unload") is True,
        }

    device_entries: dict[str, list[str]] = {}
    device_physical_hashes: dict[str, str] = {}
    device_macs: dict[str, set[str]] = {}
    identity_devices: dict[str, str] = {}
    xiaomi_identities: dict[str, dict[str, str]] = {}
    for item in devices:
        if not isinstance(item, dict):
            raise InventoryError("Home Assistant device registry is invalid")
        device_id = item.get("id")
        if not isinstance(device_id, str) or not DEVICE_ID_RE.fullmatch(device_id):
            continue
        linked_set: set[str] = set()
        raw_links = item.get("config_entries")
        if isinstance(raw_links, list):
            for raw_entry_id in raw_links:
                entry_id = _valid_entry_id(raw_entry_id)
                if entry_id is not None and entry_id in entries:
                    linked_set.add(entry_id)
        linked = sorted(linked_set)
        device_entries[device_id] = linked
        raw_identifiers = item.get("identifiers")
        device_physical_hashes[device_id] = _physical_device_hash(
            device_id, raw_identifiers
        )
        device_macs[device_id] = _device_registry_macs(item.get("connections"))
        if isinstance(raw_identifiers, list):
            xiaomi_matches: list[tuple[str, str]] = []
            for pair in raw_identifiers:
                if not isinstance(pair, list) or len(pair) != 2:
                    continue
                platform, identifier = pair
                if (
                    platform in {"localtuya", "tuya_local"}
                    and isinstance(identifier, str)
                    and TUYA_ID_RE.fullmatch(identifier)
                ):
                    identity_devices[_registry_identity_hash(platform, identifier)] = device_id
                if platform == "xiaomi_miot":
                    xiaomi_identity = _xiaomi_identity(identifier)
                    if xiaomi_identity is not None:
                        xiaomi_matches.append(xiaomi_identity)
            xiaomi_entries = [
                entry_id for entry_id in linked
                if entries[entry_id].get("domain") == "xiaomi_miot"
            ]
            if len(xiaomi_entries) == 1 and len(xiaomi_matches) == 1:
                identity_hash, mac = xiaomi_matches[0]
                if identity_hash in xiaomi_identities:
                    raise InventoryError("Xiaomi registry identity is ambiguous")
                xiaomi_identities[identity_hash] = {
                    "device_id": device_id,
                    "config_entry_id": xiaomi_entries[0],
                    "mac": mac,
                }

    entities: list[dict[str, object]] = []
    for item in display_entities:
        if not isinstance(item, dict):
            raise InventoryError("Home Assistant entity registry is invalid")
        entity_id = item.get("ei")
        platform = item.get("pl")
        device_id = item.get("di")
        try:
            normalized_id = ha_read._validate_entity_id(entity_id)
        except ha_read.AdapterError:
            continue
        if not isinstance(platform, str) or not PLATFORM_RE.fullmatch(platform):
            continue
        valid_device_id = device_id if isinstance(device_id, str) and DEVICE_ID_RE.fullmatch(device_id) else None
        snapshot_item = snapshot_index.get(normalized_id, {})
        entities.append(
            {
                "entity_id": normalized_id,
                "platform": platform,
                "device_id": valid_device_id,
                "physical_device_hash": (
                    device_physical_hashes.get(valid_device_id)
                    if valid_device_id
                    else None
                ),
                "config_entry_ids": device_entries.get(valid_device_id, []) if valid_device_id else [],
                "state_kind": snapshot_item.get("state_kind", "absent"),
                "source_last_updated_at": snapshot_item.get(
                    "source_last_updated_at"
                ),
            }
        )

    raw_states = raw_state_reader(config, "/api/states")
    raw_state_index = {
        item.get("entity_id"): item
        for item in raw_states
        if isinstance(item, dict) and isinstance(item.get("entity_id"), str)
    } if isinstance(raw_states, list) else {}
    for entity in entities:
        raw = raw_state_index.get(entity["entity_id"], {})
        attributes = raw.get("attributes") if isinstance(raw, dict) else None
        entity["friendly_name"] = ha_read.sanitize_friendly_name(
            attributes.get("friendly_name")
            if isinstance(attributes, dict) else None
        )
    network = _network_devices(raw_states)
    device_network_bindings = _build_device_network_bindings(
        device_macs,
        device_physical_hashes,
        device_entries,
        network,
        previous_device_network_bindings,
    )
    core_config = core_config_reader(config)
    integration_capabilities = _integration_capabilities(raw_states, core_config)
    core_version = integration_capabilities["tuya_local"]["core_version"]
    backup_readiness = _backup_readiness(backup_info, core_version)
    diagnostic_hosts: dict[str, tuple[str, str]] = {}
    for entry_id, entry in entries.items():
        if entry.get("domain") != "localtuya":
            continue
        try:
            hosts = _localtuya_diagnostic_hosts(diagnostics_reader(config, entry_id))
        except InventoryError:
            continue
        for identity_hash, host in hosts.items():
            if identity_hash in diagnostic_hosts:
                raise InventoryError("LocalTuya diagnostic identity is ambiguous")
            diagnostic_hosts[identity_hash] = (entry_id, host)
    identity_devices = _complete_identity_devices(
        diagnostic_hosts, identity_devices, entities
    )
    bindings = _build_identity_bindings(
        diagnostic_hosts, identity_devices, network, previous_bindings
    )
    bindings.extend(
        _build_xiaomi_bindings(
            xiaomi_identities, entities, network, previous_bindings
        )
    )
    bindings.sort(key=lambda item: (str(item["platform"]), str(item["identity_hash"])))
    device_network_bindings = _merge_identity_network_bindings(
        device_network_bindings,
        bindings,
        device_physical_hashes,
        device_entries,
    )
    integration_profiles = _integration_profiles(
        entries, {str(item["platform"]) for item in entities}
    )
    physical_devices = _physical_devices(
        entities, entries, device_network_bindings
    )
    return {
        "schema_version": 2,
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entity_count": len(entities),
        "physical_device_count": len(set(device_physical_hashes.values())),
        "config_entry_count": len(entries),
        "network_device_count": len(network),
        "identity_binding_count": len(bindings),
        "device_network_binding_count": len(device_network_bindings),
        "ip_changed_count": sum(item["status"] == "ip_changed" for item in bindings),
        "integration_capabilities": integration_capabilities,
        "integration_profiles": integration_profiles,
        "backup_readiness": backup_readiness,
        "entities": sorted(entities, key=lambda item: str(item["entity_id"])),
        "config_entries": sorted(entries.values(), key=lambda item: str(item["entry_id"])),
        "network_devices": network,
        "identity_bindings": bindings,
        "device_network_bindings": device_network_bindings,
        "physical_devices": physical_devices,
    }


def _load_previous_section(
    path: Path, section: str
) -> list[dict[str, object]]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return []
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
        or metadata.st_size <= 0
        or metadata.st_size > MAX_INVENTORY_BYTES
    ):
        raise InventoryError("previous inventory is unsafe")
    try:
        document = ha_read.strict_json_loads(path.read_bytes())
    except (OSError, ha_read.AdapterError) as error:
        raise InventoryError("previous inventory is invalid") from error
    bindings = document.get(section) if isinstance(document, dict) else None
    return bindings if isinstance(bindings, list) else []


def _load_previous_bindings(path: Path) -> list[dict[str, object]]:
    return _load_previous_section(path, "identity_bindings")


def _load_previous_device_network_bindings(
    path: Path,
) -> list[dict[str, object]]:
    return _load_previous_section(path, "device_network_bindings")


def _atomic_write(path: Path, payload: bytes) -> None:
    incident_monitor._validate_directory(path.parent)
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        not stat.S_ISREG(existing.st_mode)
        or existing.st_uid != os.geteuid()
        or existing.st_mode & 0o077
    ):
        raise InventoryError("inventory target is unsafe")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".inventory.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as error:
        raise InventoryError("inventory write failed") from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    try:
        state_dir = incident_monitor._state_dir()
        target = state_dir / INVENTORY_NAME
        inventory = collect_inventory(
            ha_read.load_config(),
            previous_bindings=_load_previous_bindings(target),
            previous_device_network_bindings=(
                _load_previous_device_network_bindings(target)
            ),
        )
        _atomic_write(
            target,
            json.dumps(inventory, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n",
        )
        store = incident_monitor.IncidentStore(
            state_dir / incident_monitor.DATABASE_NAME
        )
        try:
            observed_epoch = int(time.time())
            mapped_entities = store.replace_entity_device_map(
                inventory["entities"], observed_epoch
            )
            drift_journal = store.record_network_bindings(
                inventory["identity_bindings"], observed_epoch
            )
            device_network_journal = store.record_device_network_bindings(
                inventory["device_network_bindings"], observed_epoch
            )
        finally:
            store.close()
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "entity_count": inventory["entity_count"],
                    "physical_device_count": inventory["physical_device_count"],
                    "mapped_entity_count": mapped_entities,
                    "config_entry_count": inventory["config_entry_count"],
                    "network_device_count": inventory["network_device_count"],
                    "identity_binding_count": inventory["identity_binding_count"],
                    "device_network_binding_count": inventory[
                        "device_network_binding_count"
                    ],
                    "ip_changed_count": inventory["ip_changed_count"],
                    "drift_events": drift_journal["events"],
                    "device_network_events": device_network_journal["events"],
                    "stored": True,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except (
        InventoryError,
        incident_monitor.MonitorError,
        ha_read.AdapterError,
        OSError,
        sqlite3.Error,
    ):
        print("HOME_ASSISTANT_INVENTORY_FAILED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
