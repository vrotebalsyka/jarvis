#!/usr/bin/env python3
"""Build the one private Home Assistant physical-device inventory."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

try:
    import websocket  # type: ignore[import-not-found]
except ImportError:  # deployment preflight reports this dependency
    websocket = None


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_read as ha_read  # noqa: E402
import safe_attribute_sanitizer as attribute_sanitizer  # noqa: E402


INVENTORY_SCHEMA_VERSION = 4
MAX_INVENTORY_BYTES = 8 * 1_048_576
MAX_MESSAGE_BYTES = 4 * 1_048_576
MAX_COMMAND_MESSAGES = 64
DEFAULT_INVENTORY_PATH = Path(
    "/home/homebutler/.local/state/home-butler/inventory.json"
)
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
PLATFORM_RE = re.compile(r"^[a-z0-9_]{1,64}$")
PHYSICAL_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
SAFE_ATTRIBUTE_KEYS = frozenset({
    "device_class", "unit_of_measurement", "state_class", "options",
})


class InventoryError(RuntimeError):
    """A secret-free inventory failure."""


def inventory_path() -> Path:
    raw = os.environ.get("HOME_BUTLER_INVENTORY_FILE", "")
    return Path(raw) if raw else DEFAULT_INVENTORY_PATH


def _json_message(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise InventoryError("invalid Home Assistant websocket message")
    encoded = raw.encode("utf-8", errors="strict")
    if not encoded or len(encoded) > MAX_MESSAGE_BYTES:
        raise InventoryError("invalid Home Assistant websocket message")
    try:
        parsed = ha_read.strict_json_loads(encoded)
    except ha_read.AdapterError as error:
        raise InventoryError("invalid Home Assistant websocket message") from error
    if not isinstance(parsed, dict):
        raise InventoryError("invalid Home Assistant websocket message")
    return parsed


def _connect(config: ha_read.AdapterConfig) -> Any:
    if websocket is None:
        raise InventoryError("websocket client is unavailable")
    scheme = "wss" if config.scheme == "https" else "ws"
    try:
        return websocket.create_connection(
            f"{scheme}://{config.host}:{config.port}/api/websocket",
            timeout=10,
            suppress_origin=True,
            http_proxy_host=None,
            http_proxy_port=None,
            http_no_proxy=[config.host],
        )
    except Exception as error:
        raise InventoryError("Home Assistant websocket is unreachable") from error


def _authenticate(socket: Any, token: str) -> None:
    required = _json_message(socket.recv())
    if required.get("type") != "auth_required":
        raise InventoryError("Home Assistant authentication protocol failed")
    socket.send(json.dumps(
        {"type": "auth", "access_token": token},
        ensure_ascii=True, separators=(",", ":"),
    ))
    if _json_message(socket.recv()).get("type") != "auth_ok":
        raise InventoryError("Home Assistant authentication failed")


def _command(socket: Any, identifier: int, command_type: str) -> Any:
    if command_type not in {
        "config/entity_registry/list",
        "config/device_registry/list",
        "config/area_registry/list",
    }:
        raise InventoryError("inventory command is not allowed")
    socket.send(json.dumps(
        {"id": identifier, "type": command_type},
        ensure_ascii=True, separators=(",", ":"),
    ))
    for _attempt in range(MAX_COMMAND_MESSAGES):
        response = _json_message(socket.recv())
        if response.get("id") != identifier:
            continue
        if response.get("type") != "result" or response.get("success") is not True:
            raise InventoryError("Home Assistant inventory command failed")
        return response.get("result")
    raise InventoryError("Home Assistant inventory response is missing")


def _safe_name(value: Any) -> str | None:
    return ha_read.sanitize_friendly_name(value)


def _safe_names(value: Any, *, limit: int = 32) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        return []
    result: list[str] = []
    for item in value:
        safe = _safe_name(item)
        if safe is not None and safe not in result:
            result.append(safe)
    return result


def _safe_id(value: Any) -> str | None:
    return value if isinstance(value, str) and ID_RE.fullmatch(value) else None


def _physical_hash(device_id: str | None, entity_id: str) -> str:
    seed = f"device\0{device_id}" if device_id is not None else f"entity\0{entity_id}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _component(entity_id: str, friendly_name: str | None, attributes: Mapping[str, Any]) -> str:
    device_class = attributes.get("device_class")
    if isinstance(device_class, str) and PLATFORM_RE.fullmatch(device_class):
        return device_class
    text = " ".join((entity_id.split(".", 1)[1], friendly_name or "")).casefold()
    if (
        ("main_brush" in text or "main brush" in text or "основн" in text)
        and ("brush" in text or "щетк" in text or "щётк" in text)
    ):
        return "main_brush"
    if (
        ("side_brush" in text or "side brush" in text or "боков" in text)
        and ("brush" in text or "щетк" in text or "щётк" in text)
    ):
        return "side_brush"
    concepts = (
        ("filter", ("filter", "фильтр")),
        ("battery", ("battery", "батар", "заряд")),
        ("humidity", ("humidity", "влажност")),
        ("temperature", ("temperature", "температур")),
        ("presence", ("presence", "occupancy", "motion", "присутств", "движен")),
        ("child_lock", ("child_lock", "child lock", "блокиров")),
        ("night_mode", ("night", "ночн")),
        ("remaining_time", ("remaining", "остал")),
        ("power", ("power", "питани")),
    )
    for concept, markers in concepts:
        if any(marker in text for marker in markers):
            return concept
    return "main" if entity_id.split(".", 1)[0] in {
        "light", "switch", "vacuum", "humidifier", "media_player", "camera"
    } else "state"


def _semantic_attributes(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    candidate = {key: raw.get(key) for key in SAFE_ATTRIBUTE_KEYS if key in raw}
    sanitized = attribute_sanitizer.sanitize_attributes(candidate)
    result: dict[str, Any] = {}
    for key, value in sanitized.items():
        text = attribute_sanitizer.untrusted_text(value)
        if text is not None:
            result[key] = text
        elif isinstance(value, list):
            options = [
                item for item in (
                    attribute_sanitizer.untrusted_text(candidate) for candidate in value
                ) if item is not None
            ]
            result[key] = options[:128]
    return result


def _raw_state_index(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > ha_read.MAX_LISTED_ENTITIES:
        raise InventoryError("Home Assistant state catalogue is invalid")
    result: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise InventoryError("Home Assistant state catalogue is invalid")
        try:
            entity_id = ha_read._validate_entity_id(item.get("entity_id"))
        except ha_read.AdapterError:
            continue
        result[entity_id] = item
    return result


def collect_inventory(
    config: ha_read.AdapterConfig,
    *,
    connector: Callable[[ha_read.AdapterConfig], Any] = _connect,
    snapshot_reader: Callable[[str], tuple[dict[str, Any], int]] = ha_read.execute_safely,
    raw_state_reader: Callable[[ha_read.AdapterConfig, str], Any] = ha_read.request_json,
) -> dict[str, Any]:
    """Read registries and current states; never subscribe or call a service."""

    socket = connector(config)
    try:
        _authenticate(socket, config.token)
        registry_entities = _command(socket, 10, "config/entity_registry/list")
        registry_devices = _command(socket, 11, "config/device_registry/list")
        registry_areas = _command(socket, 12, "config/area_registry/list")
    finally:
        try:
            socket.close()
        except Exception:
            pass
    if (
        not isinstance(registry_entities, list)
        or not isinstance(registry_devices, list)
        or not isinstance(registry_areas, list)
        or len(registry_entities) > 4096
        or len(registry_devices) > 4096
        or len(registry_areas) > 1024
    ):
        raise InventoryError("Home Assistant registry response is invalid")
    snapshot, exit_code = snapshot_reader("snapshot")
    snapshot_entities = snapshot.get("entities") if isinstance(snapshot, dict) else None
    if exit_code != 0 or not isinstance(snapshot_entities, list):
        raise InventoryError("Home Assistant inventory snapshot failed")
    sanitized_states = {
        item["entity_id"]: item for item in snapshot_entities
        if isinstance(item, dict) and isinstance(item.get("entity_id"), str)
    }
    raw_states = _raw_state_index(raw_state_reader(config, "/api/states"))

    areas_by_id: dict[str, dict[str, Any]] = {}
    for item in registry_areas:
        if not isinstance(item, dict):
            continue
        area_id = _safe_id(item.get("area_id") or item.get("id"))
        name = _safe_name(item.get("name"))
        if area_id is None or name is None:
            continue
        areas_by_id[area_id] = {
            "area_id": area_id, "name": name, "aliases": _safe_names(item.get("aliases")),
        }

    devices_by_id: dict[str, dict[str, Any]] = {}
    for item in registry_devices:
        if not isinstance(item, dict):
            continue
        device_id = _safe_id(item.get("id"))
        if device_id is not None:
            devices_by_id[device_id] = item

    entities: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in registry_entities:
        if not isinstance(item, dict):
            continue
        try:
            entity_id = ha_read._validate_entity_id(item.get("entity_id"))
        except ha_read.AdapterError:
            continue
        state = sanitized_states.get(entity_id)
        if state is None:
            continue
        device_id = _safe_id(item.get("device_id"))
        # Registry entities without a device are helpers, automations or
        # integration services. They are not physical-device identities.
        if device_id is None:
            continue
        device = devices_by_id.get(device_id or "", {})
        raw_state = raw_states.get(entity_id, {})
        raw_attributes = raw_state.get("attributes")
        raw_attributes = raw_attributes if isinstance(raw_attributes, dict) else {}
        friendly_name = (
            _safe_name(raw_attributes.get("friendly_name"))
            or _safe_name(item.get("name"))
            or _safe_name(item.get("original_name"))
            or entity_id.split(".", 1)[1].replace("_", " ")[:100]
        )
        area_id = _safe_id(item.get("area_id")) or _safe_id(device.get("area_id"))
        area = areas_by_id.get(area_id or "")
        platform = item.get("platform")
        domain = entity_id.split(".", 1)[0]
        physical_hash = _physical_hash(device_id, entity_id)
        semantic = _semantic_attributes(raw_attributes)
        entity = {
            "entity_id": entity_id,
            "domain": domain,
            "friendly_name": friendly_name,
            "original_name": _safe_name(item.get("original_name")),
            "entity_aliases": _safe_names(item.get("aliases")),
            "area_name": area.get("name") if area else None,
            "area_aliases": area.get("aliases", []) if area else [],
            "platform": platform if isinstance(platform, str) and PLATFORM_RE.fullmatch(platform) else None,
            "physical_device_hash": physical_hash,
            "component": _component(entity_id, friendly_name, raw_attributes),
            "semantic_role": "measurement" if state.get("state_kind") == "number" else "state",
            "capability": "observe",
            "semantic_attributes": semantic,
            "state_kind": state.get("state_kind"),
            "state_value": state.get("state_value"),
            "source_last_updated_at": state.get("source_last_updated_at"),
        }
        entities.append(entity)
        grouped.setdefault(physical_hash, []).append(entity)

    physical_devices: list[dict[str, Any]] = []
    for physical_hash, members in grouped.items():
        first_entity_id = str(members[0]["entity_id"])
        registry_item = next(
            (
                device for device_id, device in devices_by_id.items()
                if _physical_hash(device_id, first_entity_id) == physical_hash
            ),
            {},
        )
        display_name = (
            _safe_name(registry_item.get("name_by_user"))
            or _safe_name(registry_item.get("name"))
            or _safe_name(registry_item.get("original_name"))
            or min(
                (str(item["friendly_name"]) for item in members),
                key=lambda value: (len(value), value.casefold()),
            )
        )
        aliases: list[str] = []
        for value in (
            registry_item.get("name_by_user"), registry_item.get("name"),
            registry_item.get("original_name"),
        ):
            safe = _safe_name(value)
            if safe is not None and safe != display_name and safe not in aliases:
                aliases.append(safe)
        for member in members:
            for value in [member.get("friendly_name"), *member.get("entity_aliases", [])]:
                safe = _safe_name(value)
                if safe is not None and safe != display_name and safe not in aliases:
                    aliases.append(safe)
        area_names = sorted({
            str(item["area_name"]) for item in members if isinstance(item.get("area_name"), str)
        })
        area_aliases = sorted({
            str(value) for item in members for value in item.get("area_aliases", [])
            if isinstance(value, str)
        })
        physical_devices.append({
            "physical_device_hash": physical_hash,
            "display_name": display_name,
            "name": _safe_name(registry_item.get("name")),
            "name_by_user": _safe_name(registry_item.get("name_by_user")),
            "original_name": _safe_name(registry_item.get("original_name")),
            "aliases": aliases[:64],
            "area_names": area_names,
            "area_aliases": area_aliases,
            "manufacturers": [value for value in [_safe_name(registry_item.get("manufacturer"))] if value],
            "models": [value for value in [_safe_name(registry_item.get("model"))] if value],
            "entity_ids": sorted(str(item["entity_id"]) for item in members),
            "available_entity_count": sum(item.get("state_kind") not in {"unavailable", "redacted"} for item in members),
            "unavailable_entity_count": sum(item.get("state_kind") == "unavailable" for item in members),
        })

    entities.sort(key=lambda item: str(item["entity_id"]))
    physical_devices.sort(key=lambda item: (
        str(item.get("display_name") or "").casefold(), str(item["physical_device_hash"])
    ))
    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "source": "Home Assistant registries and sanitized current states",
        "observed_at": snapshot.get("observed_at"),
        "entity_count": len(entities),
        "physical_device_count": len(physical_devices),
        "areas": sorted(areas_by_id.values(), key=lambda item: str(item["name"]).casefold()),
        "entities": entities,
        "physical_devices": physical_devices,
    }


def migrate_inventory_document(document: dict[str, Any]) -> dict[str, Any]:
    """Accept only graph fields and discard legacy recovery/network overlays."""

    version = document.get("schema_version")
    entities = document.get("entities")
    devices = document.get("physical_devices")
    areas = document.get("areas", [])
    if (
        version not in {3, INVENTORY_SCHEMA_VERSION}
        or not isinstance(entities, list) or len(entities) > 4096
        or not isinstance(devices, list) or len(devices) > 4096
        or not isinstance(areas, list) or len(areas) > 1024
    ):
        raise InventoryError("inventory schema is unsupported")
    for item in devices:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("physical_device_hash"), str)
            or PHYSICAL_HASH_RE.fullmatch(item["physical_device_hash"]) is None
        ):
            raise InventoryError("inventory schema is invalid")
    migrated = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "source": document.get("source", "Home Assistant inventory"),
        "observed_at": document.get("observed_at"),
        "entity_count": len(entities),
        "physical_device_count": len(devices),
        "areas": areas,
        "entities": entities,
        "physical_devices": devices,
    }
    if version != INVENTORY_SCHEMA_VERSION:
        migrated["migrated_from_schema"] = version
    return migrated


def _validate_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise InventoryError("inventory directory is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise InventoryError("inventory directory is unsafe")


def _atomic_write(path: Path, payload: bytes) -> None:
    _validate_directory(path.parent)
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        not stat.S_ISREG(existing.st_mode)
        or existing.st_uid != os.geteuid()
        or existing.st_nlink != 1
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
        inventory = collect_inventory(ha_read.load_config())
        target = inventory_path()
        _atomic_write(
            target,
            json.dumps(
                inventory, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode("ascii") + b"\n",
        )
        print(json.dumps({
            "schema_version": 1,
            "entity_count": inventory["entity_count"],
            "physical_device_count": inventory["physical_device_count"],
            "stored": True,
        }, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 0
    except (InventoryError, ha_read.AdapterError, OSError):
        print("HOME_ASSISTANT_INVENTORY_FAILED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
