#!/usr/bin/env python3
"""Build the single persistent HomeGraph from Home Assistant metadata only."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

try:
    import websocket  # type: ignore[import-not-found]
except ImportError:  # deployment preflight reports this dependency
    websocket = None


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_read as ha_read  # noqa: E402


INVENTORY_SCHEMA_VERSION = 5
MAX_INVENTORY_BYTES = 8 * 1_048_576
MAX_MESSAGE_BYTES = 4 * 1_048_576
MAX_COMMAND_MESSAGES = 64
DEFAULT_INVENTORY_PATH = Path("/home/homebutler/.local/state/home-butler/inventory.json")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
CODE_RE = re.compile(r"^[a-z0-9_.-]{1,128}$")
HASH_RE = re.compile(r"^[a-f0-9]{64}$")
FORBIDDEN_DYNAMIC_KEYS = frozenset({
    "state", "state_kind", "state_value", "availability", "available",
    "observed_at", "last_updated", "source_last_updated_at", "current",
    "available_entity_count", "unavailable_entity_count",
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
    if _json_message(socket.recv()).get("type") != "auth_required":
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


def _hash(kind: str, value: str) -> str:
    return hashlib.sha256(f"{kind}\0{value}".encode("utf-8")).hexdigest()


def _physical_hash(device_id: str | None, entity_id: str) -> str:
    """Compatibility helper; identity is exact registry ID or exact entity ID."""

    return _hash("device", device_id) if device_id is not None else _hash("entity", entity_id)


def _safe_text(value: Any, *, maximum: int = 160) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not 1 <= len(normalized) <= maximum or ha_read.SENSITIVE_TEXT_RE.search(normalized):
        return None
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        return None
    return normalized


def _safe_code(value: Any) -> str | None:
    return value if isinstance(value, str) and CODE_RE.fullmatch(value) else None


def _safe_id(value: Any) -> str | None:
    return value if isinstance(value, str) and ID_RE.fullmatch(value) else None


def _safe_names(value: Any, *, limit: int = 64) -> list[str]:
    if not isinstance(value, list) or len(value) > limit * 2:
        return []
    result: list[str] = []
    for item in value:
        safe = _safe_text(item)
        if safe is not None and safe not in result:
            result.append(safe)
    return result[:limit]


def _safe_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or abs(number) > 1_000_000_000_000:
        return None
    return int(number) if number.is_integer() else number


def _raw_state_index(raw_states: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_states, list) or len(raw_states) > ha_read.MAX_LISTED_ENTITIES:
        raise InventoryError("Home Assistant state catalogue is invalid")
    result: dict[str, dict[str, Any]] = {}
    for item in raw_states:
        if not isinstance(item, dict):
            raise InventoryError("Home Assistant state catalogue is invalid")
        try:
            entity_id = ha_read._validate_entity_id(item.get("entity_id"))
        except ha_read.AdapterError:
            continue
        if entity_id in result:
            raise InventoryError("Home Assistant state catalogue has duplicates")
        result[entity_id] = item
    return result


def _metadata_attributes(raw_state: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = raw_state.get("attributes") if isinstance(raw_state, Mapping) else None
    attributes = raw if isinstance(raw, Mapping) else {}
    result: dict[str, Any] = {}
    for key in ("friendly_name", "device_class", "state_class", "unit_of_measurement"):
        safe = _safe_text(attributes.get(key))
        if safe is not None:
            result[key] = safe
    supported = attributes.get("supported_features")
    if isinstance(supported, int) and not isinstance(supported, bool) and 0 <= supported <= 2**63 - 1:
        result["supported_features"] = supported
    options = _safe_names(attributes.get("options"), limit=128)
    if options:
        result["options"] = options
    for key in ("min", "max", "step"):
        number = _safe_number(attributes.get(key))
        if number is not None:
            result[key] = number
    return result


def _component(entity_id: str, metadata: Mapping[str, Any]) -> str:
    device_class = str(metadata.get("device_class") or "").casefold()
    if device_class in {"battery", "humidity", "temperature", "problem"}:
        return "error" if device_class == "problem" else device_class
    text = " ".join((
        entity_id.split(".", 1)[1],
        str(metadata.get("friendly_name") or ""),
        str(metadata.get("name") or ""),
        str(metadata.get("original_name") or ""),
        str(metadata.get("translation_key") or ""),
        device_class,
    )).casefold().replace("ё", "е")
    concepts = (
        ("main_brush", ("main_brush", "main brush", "основн"), ("brush", "щетк")),
        ("side_brush", ("side_brush", "side brush", "боков"), ("brush", "щетк")),
        ("child_lock", ("child_lock", "child lock", "блокиров"), ("",)),
        ("filter", ("filter", "фильтр"), ("",)),
        ("battery", ("battery", "батар", "заряд"), ("",)),
        ("humidity", ("humidity", "влажност"), ("",)),
        ("temperature", ("temperature", "температур"), ("",)),
        ("error", ("error", "problem", "ошиб", "неисправ"), ("",)),
        ("mode", ("mode", "режим"), ("",)),
        ("power", ("power", "питани"), ("",)),
    )
    for concept, primary, secondary in concepts:
        if any(marker in text for marker in primary) and any(marker in text for marker in secondary):
            return concept
    domain = entity_id.split(".", 1)[0]
    return "power" if domain in {"light", "switch", "fan", "humidifier"} else "status"


def _integration_ref(platform: str, config_entry_id: str | None) -> str:
    return _hash("integration", f"{platform}\0{config_entry_id or '-'}")


def build_inventory(
    registry_entities: Any,
    registry_devices: Any,
    registry_areas: Any,
    raw_states: Any,
) -> dict[str, Any]:
    """Build one graph. Raw states contribute metadata, never current values."""

    if (
        not isinstance(registry_entities, list) or len(registry_entities) > 4096
        or not isinstance(registry_devices, list) or len(registry_devices) > 4096
        or not isinstance(registry_areas, list) or len(registry_areas) > 1024
    ):
        raise InventoryError("Home Assistant registry response is invalid")
    states = _raw_state_index(raw_states)

    area_ids: dict[str, str] = {}
    area_nodes: dict[str, dict[str, Any]] = {}
    for raw in registry_areas:
        if not isinstance(raw, Mapping):
            continue
        area_id = _safe_id(raw.get("area_id") or raw.get("id"))
        name = _safe_text(raw.get("name"))
        if area_id is None or name is None:
            continue
        ref = _hash("area", area_id)
        area_ids[area_id] = ref
        area_nodes[ref] = {
            "area_ref": ref, "name": name, "aliases": _safe_names(raw.get("aliases")),
            "entity_refs": [], "target_refs": [],
        }

    devices_by_id: dict[str, Mapping[str, Any]] = {}
    physical_nodes: dict[str, dict[str, Any]] = {}
    for raw in registry_devices:
        if not isinstance(raw, Mapping):
            continue
        device_id = _safe_id(raw.get("id"))
        if device_id is None:
            continue
        devices_by_id[device_id] = raw
        ref = _hash("device", device_id)
        names = [
            _safe_text(raw.get("name_by_user")), _safe_text(raw.get("name")),
            _safe_text(raw.get("original_name")),
        ]
        display_name = next((value for value in names if value), None) or "Устройство"
        aliases = [value for value in names if value and value != display_name]
        area_id = _safe_id(raw.get("area_id"))
        area_ref = area_ids.get(area_id or "")
        platforms = []
        config_entries = raw.get("config_entries")
        if isinstance(config_entries, list):
            platforms = [value for value in (_safe_id(item) for item in config_entries) if value]
        physical_nodes[ref] = {
            "target_ref": ref, "kind": "physical", "display_name": display_name,
            "names": [value for value in names if value], "aliases": list(dict.fromkeys(aliases)),
            "manufacturer": _safe_text(raw.get("manufacturer")),
            "model": _safe_text(raw.get("model")),
            "area_refs": [area_ref] if area_ref else [], "entity_refs": [],
            "integration_refs": [], "strong_identity": "device_registry_id_hash",
            "config_bindings": [_hash("config_entry", value) for value in platforms],
        }

    entities: list[dict[str, Any]] = []
    logical_nodes: dict[str, dict[str, Any]] = {}
    integrations: dict[str, dict[str, Any]] = {}
    registered_ids: set[str] = set()

    def add_integration(platform: str | None, config_id: str | None, entity_ref: str, target_ref: str) -> list[str]:
        if platform is None:
            return []
        ref = _integration_ref(platform, config_id)
        node = integrations.setdefault(ref, {
            "integration_ref": ref, "platform": platform,
            "config_binding": _hash("config_entry", config_id) if config_id else None,
            "entity_refs": [], "target_refs": [],
        })
        if entity_ref not in node["entity_refs"]:
            node["entity_refs"].append(entity_ref)
        if target_ref not in node["target_refs"]:
            node["target_refs"].append(target_ref)
        return [ref]

    def append_entity(raw: Mapping[str, Any] | None, entity_id: str, state_only: bool) -> None:
        registry_id = _safe_id(raw.get("id")) if raw is not None else None
        entity_ref = _hash("entity_registry" if registry_id else "state_entity", registry_id or entity_id)
        device_id = _safe_id(raw.get("device_id")) if raw is not None else None
        device = devices_by_id.get(device_id or "", {})
        if device_id:
            target_ref = _hash("device", device_id)
            if target_ref not in physical_nodes:
                physical_nodes[target_ref] = {
                    "target_ref": target_ref, "kind": "physical", "display_name": "Устройство",
                    "names": [], "aliases": [], "manufacturer": None, "model": None,
                    "area_refs": [], "entity_refs": [], "integration_refs": [],
                    "strong_identity": "device_registry_id_hash", "config_bindings": [],
                }
            node = physical_nodes[target_ref]
        else:
            target_ref = _hash("logical_entity", registry_id or entity_id)
            node = logical_nodes.setdefault(target_ref, {
                "target_ref": target_ref, "kind": "logical", "display_name": "Сущность",
                "names": [], "aliases": [], "area_refs": [], "entity_refs": [],
                "integration_refs": [],
                "strong_identity": "entity_registry_id_hash" if registry_id else "exact_entity_id_hash",
                "state_only": state_only,
            })
        state_metadata = _metadata_attributes(states.get(entity_id))
        name = _safe_text(raw.get("name")) if raw is not None else None
        original_name = _safe_text(raw.get("original_name")) if raw is not None else None
        friendly_name = state_metadata.get("friendly_name")
        display_name = name or friendly_name or original_name or entity_id.split(".", 1)[1].replace("_", " ")[:160]
        aliases = _safe_names(raw.get("aliases")) if raw is not None else []
        area_id = (
            _safe_id(raw.get("area_id")) if raw is not None else None
        ) or _safe_id(device.get("area_id"))
        area_ref = area_ids.get(area_id or "")
        platform = _safe_code(raw.get("platform")) if raw is not None else None
        config_id = _safe_id(raw.get("config_entry_id")) if raw is not None else None
        integration_refs = add_integration(platform, config_id, entity_ref, target_ref)
        metadata = {
            "name": name, "original_name": original_name, "friendly_name": friendly_name,
            "aliases": aliases,
            "translation_key": _safe_code(raw.get("translation_key")) if raw is not None else None,
            "entity_category": _safe_code(raw.get("entity_category")) if raw is not None else None,
            "device_class": state_metadata.get("device_class"),
            "state_class": state_metadata.get("state_class"),
            "unit": state_metadata.get("unit_of_measurement"),
            "supported_features": state_metadata.get("supported_features"),
            "options": state_metadata.get("options", []),
            "min": state_metadata.get("min"), "max": state_metadata.get("max"),
            "step": state_metadata.get("step"),
        }
        entity = {
            "entity_ref": entity_ref, "entity_id": entity_id, "target_ref": target_ref,
            "target_kind": node["kind"], "domain": entity_id.split(".", 1)[0],
            "display_name": display_name, **metadata,
            "component": _component(entity_id, {**metadata, "friendly_name": display_name}),
            "disabled": bool(raw is not None and raw.get("disabled_by") is not None),
            "hidden": bool(raw is not None and raw.get("hidden_by") is not None),
            "platform": platform, "integration_refs": integration_refs,
            "area_ref": area_ref, "registry_backed": not state_only,
        }
        entities.append(entity)
        node["entity_refs"].append(entity_ref)
        if node["kind"] == "logical" or not node["names"]:
            for candidate in [display_name, name, original_name, friendly_name]:
                if candidate and candidate not in node["names"]:
                    node["names"].append(candidate)
            for candidate in aliases:
                if candidate not in node["aliases"]:
                    node["aliases"].append(candidate)
        node["display_name"] = node["names"][0] if node["names"] else node["display_name"]
        if area_ref and area_ref not in node["area_refs"]:
            node["area_refs"].append(area_ref)
        for ref in integration_refs:
            if ref not in node["integration_refs"]:
                node["integration_refs"].append(ref)
        if area_ref:
            area = area_nodes[area_ref]
            if entity_ref not in area["entity_refs"]:
                area["entity_refs"].append(entity_ref)
            if target_ref not in area["target_refs"]:
                area["target_refs"].append(target_ref)

    for raw in registry_entities:
        if not isinstance(raw, Mapping):
            continue
        try:
            entity_id = ha_read._validate_entity_id(raw.get("entity_id"))
        except ha_read.AdapterError:
            continue
        if entity_id in registered_ids:
            raise InventoryError("entity registry has duplicates")
        registered_ids.add(entity_id)
        append_entity(raw, entity_id, False)
    for entity_id in sorted(set(states) - registered_ids):
        append_entity(None, entity_id, True)

    for collection in (physical_nodes.values(), logical_nodes.values(), area_nodes.values(), integrations.values()):
        for node in collection:
            for key, value in list(node.items()):
                if isinstance(value, list):
                    node[key] = sorted(set(value))
    entities.sort(key=lambda item: item["entity_ref"])
    physical = sorted(physical_nodes.values(), key=lambda item: item["target_ref"])
    logical = sorted(logical_nodes.values(), key=lambda item: item["target_ref"])
    areas = sorted(area_nodes.values(), key=lambda item: item["area_ref"])
    integration_nodes = sorted(integrations.values(), key=lambda item: item["integration_ref"])
    document = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "source": "Home Assistant registry metadata only",
        "entity_count": len(entities), "physical_device_count": len(physical),
        "logical_entity_count": len(logical), "area_count": len(areas),
        "integration_count": len(integration_nodes),
        "entities": entities, "physical_nodes": physical, "logical_nodes": logical,
        "area_nodes": areas, "integration_nodes": integration_nodes,
    }
    return validate_inventory_document(document)


def collect_inventory(
    config: ha_read.AdapterConfig,
    *,
    connector: Callable[[ha_read.AdapterConfig], Any] = _connect,
    raw_state_reader: Callable[[ha_read.AdapterConfig, str], Any] = ha_read.request_json,
    **_legacy: Any,
) -> dict[str, Any]:
    """Read three registries and GET states; persist metadata only."""

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
    return build_inventory(
        registry_entities, registry_devices, registry_areas,
        raw_state_reader(config, "/api/states"),
    )


def _walk(document: Any) -> Iterable[tuple[str | None, Any]]:
    stack: list[tuple[str | None, Any]] = [(None, document)]
    while stack:
        key, value = stack.pop()
        yield key, value
        if isinstance(value, Mapping):
            stack.extend((str(child_key), child) for child_key, child in value.items())
        elif isinstance(value, list):
            stack.extend((None, child) for child in value)


def validate_inventory_document(document: dict[str, Any]) -> dict[str, Any]:
    if document.get("schema_version") != INVENTORY_SCHEMA_VERSION:
        raise InventoryError("inventory schema is unsupported")
    collections = {
        "entities": 8192, "physical_nodes": 4096, "logical_nodes": 8192,
        "area_nodes": 1024, "integration_nodes": 4096,
    }
    for key, limit in collections.items():
        value = document.get(key)
        if not isinstance(value, list) or len(value) > limit or any(not isinstance(item, dict) for item in value):
            raise InventoryError("inventory schema is invalid")
    for key, _value in _walk(document):
        if key in FORBIDDEN_DYNAMIC_KEYS:
            raise InventoryError("persistent inventory contains current state")
    entities = document["entities"]
    entity_refs = {item.get("entity_ref") for item in entities}
    target_refs = {
        item.get("target_ref")
        for collection in (document["physical_nodes"], document["logical_nodes"])
        for item in collection
    }
    if (
        len(entity_refs) != len(entities) or None in entity_refs
        or len(target_refs) != len(document["physical_nodes"]) + len(document["logical_nodes"])
        or None in target_refs
        or any(HASH_RE.fullmatch(str(ref)) is None for ref in entity_refs | target_refs)
    ):
        raise InventoryError("inventory identity is invalid")
    for item in entities:
        try:
            ha_read._validate_entity_id(item.get("entity_id"))
        except ha_read.AdapterError as error:
            raise InventoryError("inventory join identity is invalid") from error
        if item.get("target_ref") not in target_refs:
            raise InventoryError("inventory target binding is invalid")
    expected = {
        "entity_count": len(entities),
        "physical_device_count": len(document["physical_nodes"]),
        "logical_entity_count": len(document["logical_nodes"]),
        "area_count": len(document["area_nodes"]),
        "integration_count": len(document["integration_nodes"]),
    }
    if any(document.get(key) != value for key, value in expected.items()):
        raise InventoryError("inventory counts are invalid")
    return document


def migrate_inventory_document(document: dict[str, Any]) -> dict[str, Any]:
    """Stage 71 rejects state-bearing v4 instead of trusting stale migration."""

    return validate_inventory_document(document)


def _validate_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise InventoryError("inventory directory is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise InventoryError("inventory directory is unsafe")


def _atomic_write(path: Path, payload: bytes) -> None:
    _validate_directory(path.parent)
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.geteuid()
        or existing.st_nlink != 1 or existing.st_mode & 0o077
    ):
        raise InventoryError("inventory target is unsafe")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".inventory.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload); handle.flush(); os.fsync(handle.fileno())
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
        _atomic_write(
            inventory_path(),
            json.dumps(inventory, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n",
        )
        print(json.dumps({
            "schema_version": 1, "stored_schema": INVENTORY_SCHEMA_VERSION,
            "entity_count": inventory["entity_count"],
            "physical_device_count": inventory["physical_device_count"],
            "logical_entity_count": inventory["logical_entity_count"],
            "stored": True, "current_values_stored": 0,
        }, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 0
    except (InventoryError, ha_read.AdapterError, OSError):
        print("HOME_ASSISTANT_INVENTORY_FAILED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
