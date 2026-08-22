#!/usr/bin/env python3
"""Maintain a private read-only knowledge catalog of HA physical devices."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import ha_entity_query  # noqa: E402
import heartbeat  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402


MAX_CATALOG_BYTES = 8 * 1_048_576
MAX_DEVICES = 4096
NEW_DEVICE_WINDOW_SECONDS = 24 * 60 * 60
HASH_RE = re.compile(r"^[a-f0-9]{64}$")
SAFE_DOMAIN_RE = re.compile(r"^[a-z0-9_]{1,64}$")
CATALOG_PATH = Path(os.environ.get(
    "HOME_BUTLER_HA_DEVICE_KNOWLEDGE_PATH",
    str(Path.home() / ".local/state/home-butler/ha-device-knowledge.json"),
))


class KnowledgeError(RuntimeError):
    """A secret-free HA knowledge catalog failure."""


def _timestamp(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def _safe_strings(value: Any, *, maximum: int = 512) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        return []
    return sorted({
        item for item in value
        if isinstance(item, str) and SAFE_DOMAIN_RE.fullmatch(item)
    })


def _safe_entity_ids(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 512:
        raise KnowledgeError("device entity list is invalid")
    result: list[str] = []
    for item in value:
        try:
            result.append(ha_read._validate_entity_id(item))
        except ha_read.AdapterError:
            continue
    return sorted(set(result))


def read_catalog(path: Path | None = None, *, missing_ok: bool = False) -> dict[str, Any]:
    target = CATALOG_PATH if path is None else path
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        if missing_ok:
            return {}
        raise KnowledgeError("device knowledge is unavailable")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o077
        or not 0 < metadata.st_size <= MAX_CATALOG_BYTES
    ):
        raise KnowledgeError("device knowledge is unavailable")
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
        try:
            raw = os.read(descriptor, MAX_CATALOG_BYTES + 1)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise KnowledgeError("device knowledge is unavailable") from error
    if len(raw) > MAX_CATALOG_BYTES:
        raise KnowledgeError("device knowledge is unavailable")
    try:
        document = ha_read.strict_json_loads(raw)
    except ha_read.AdapterError as error:
        raise KnowledgeError("device knowledge is unavailable") from error
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise KnowledgeError("device knowledge is unavailable")
    return document


def build_catalog(
    inventory: dict[str, Any],
    previous: dict[str, Any] | None = None,
    *,
    now: int | None = None,
) -> dict[str, Any]:
    observed_epoch = int(time.time()) if now is None else now
    if isinstance(observed_epoch, bool) or observed_epoch < 0:
        raise KnowledgeError("observation time is invalid")
    raw_devices = inventory.get("physical_devices")
    raw_entities = inventory.get("entities", [])
    if not isinstance(raw_devices, list) or len(raw_devices) > MAX_DEVICES:
        raise KnowledgeError("physical device inventory is invalid")
    if not isinstance(raw_entities, list) or len(raw_entities) > 4096:
        raise KnowledgeError("entity inventory is invalid")
    entity_metadata: dict[str, dict[str, Any]] = {}
    for raw_entity in raw_entities:
        if not isinstance(raw_entity, dict):
            continue
        try:
            entity_id = ha_read._validate_entity_id(raw_entity.get("entity_id"))
        except ha_read.AdapterError:
            continue
        platform = raw_entity.get("platform")
        entity_metadata[entity_id] = {
            "entity_id": entity_id,
            "friendly_name": ha_read.sanitize_friendly_name(
                raw_entity.get("friendly_name")
            ),
            "domain": entity_id.split(".", 1)[0],
            "platform": (
                platform if isinstance(platform, str)
                and SAFE_DOMAIN_RE.fullmatch(platform) else "runtime"
            ),
            "state_kind": str(raw_entity.get("state_kind", "unknown")),
        }
    previous_devices = {
        item.get("physical_device_hash"): item
        for item in (previous or {}).get("devices", [])
        if isinstance(item, dict) and HASH_RE.fullmatch(
            str(item.get("physical_device_hash", ""))
        )
    } if isinstance(previous, dict) else {}
    baseline = not previous_devices
    active_hashes: set[str] = set()
    devices: list[dict[str, Any]] = []
    for raw in raw_devices:
        if not isinstance(raw, dict):
            continue
        physical_hash = raw.get("physical_device_hash")
        if not isinstance(physical_hash, str) or HASH_RE.fullmatch(physical_hash) is None:
            continue
        active_hashes.add(physical_hash)
        old = previous_devices.get(physical_hash, {})
        first_seen = old.get("first_seen_epoch", observed_epoch)
        if not isinstance(first_seen, int) or isinstance(first_seen, bool) or first_seen < 0:
            first_seen = observed_epoch
        display_name = ha_read.sanitize_friendly_name(raw.get("display_name"))
        integrations = _safe_strings(raw.get("config_domains"))
        platforms = _safe_strings(raw.get("platforms"))
        entity_ids = _safe_entity_ids(raw.get("entity_ids"))
        entity_details = [
            entity_metadata.get(entity_id, {
                "entity_id": entity_id,
                "friendly_name": None,
                "domain": entity_id.split(".", 1)[0],
                "platform": "runtime",
                "state_kind": "unknown",
            })
            for entity_id in entity_ids
        ]
        if baseline:
            status = "baseline"
        elif not old:
            status = "new"
        elif (
            old.get("lifecycle") == "new"
            and observed_epoch - first_seen < NEW_DEVICE_WINDOW_SECONDS
        ):
            status = "new"
        else:
            status = "known"
        devices.append({
            "physical_device_hash": physical_hash,
            "display_name": display_name or "Без имени",
            "active": True,
            "lifecycle": status,
            "first_seen_epoch": first_seen,
            "first_seen_at": _timestamp(first_seen),
            "last_seen_epoch": observed_epoch,
            "last_seen_at": _timestamp(observed_epoch),
            "integration_paths": integrations,
            "platforms": platforms,
            "multiple_connection_paths": len(set(integrations) | set(platforms)) > 1,
            "entity_ids": entity_ids,
            "entities": entity_details,
            "entity_count": len(entity_ids),
            "available_entity_count": int(raw.get("available_entity_count", 0)),
            "unavailable_entity_count": int(raw.get("unavailable_entity_count", 0)),
            "network_status": str(raw.get("network_status", "unknown")),
            "safety_class": str(raw.get("safety_class", "unknown")),
        })
    for physical_hash, old in previous_devices.items():
        if physical_hash in active_hashes or old.get("active") is False:
            continue
        missing = dict(old)
        missing["active"] = False
        missing["lifecycle"] = "removed_from_current_registry"
        devices.append(missing)
    devices.sort(key=lambda item: (
        not bool(item.get("active")),
        str(item.get("display_name", "")).casefold(),
        str(item.get("physical_device_hash", "")),
    ))
    name_groups: dict[str, list[str]] = {}
    for item in devices:
        if item.get("active") is not True:
            continue
        normalized = unicodedata.normalize(
            "NFKC", str(item.get("display_name", ""))
        ).casefold().strip()
        if normalized and normalized != "без имени":
            name_groups.setdefault(normalized, []).append(item["physical_device_hash"])
    review_candidates = [
        {"shared_display_name": name, "physical_device_hashes": sorted(hashes)}
        for name, hashes in sorted(name_groups.items()) if len(hashes) > 1
    ]
    active = [item for item in devices if item.get("active") is True]
    return {
        "schema_version": 1,
        "observed_epoch": observed_epoch,
        "observed_at": _timestamp(observed_epoch),
        "source_inventory_observed_at": inventory.get("observed_at"),
        "learning_scope": "read_only_sanitized_home_assistant_registry",
        "actions_performed": 0,
        "grouping_policy": (
            "Merge only by proven stable registry identity; never merge by name alone. "
            "Entities are functions, HA device records are integration views, and a "
            "physical device may have several integration paths."
        ),
        "active_physical_device_count": len(active),
        "known_physical_device_count": len(devices),
        "new_device_count": sum(item.get("lifecycle") == "new" for item in active),
        "multiple_connection_device_count": sum(
            item.get("multiple_connection_paths") is True for item in active
        ),
        "review_candidate_count": len(review_candidates),
        "review_candidates": review_candidates,
        "devices": devices,
    }


def compact_context(document: dict[str, Any], question: str) -> dict[str, Any]:
    devices = document.get("devices")
    if not isinstance(devices, list) or len(devices) > MAX_DEVICES:
        raise KnowledgeError("device knowledge is invalid")
    active = [item for item in devices if isinstance(item, dict) and item.get("active") is True]
    normalized_question = unicodedata.normalize("NFKC", question).casefold()
    stop_words = {
        "home", "assistant", "хаос", "хоум", "ассистант", "устройство",
        "устройства", "сущность", "сущности", "интеграция", "интеграции",
        "подключись", "покажи", "расскажи", "изучи", "проверь", "какие",
    }
    tokens = [
        item for item in re.findall(r"[a-zа-яё0-9_]+", normalized_question)
        if len(item) >= 3 and item not in stop_words
    ]
    matched: list[dict[str, Any]] = []
    for item in active:
        haystack = " ".join([
            str(item.get("display_name", "")),
            *item.get("integration_paths", []),
            *item.get("platforms", []),
            *item.get("entity_ids", []),
        ]).casefold()
        if tokens and all(
            token in haystack or (len(token) >= 7 and token[:6] in haystack)
            for token in tokens
        ):
            matched.append(item)
    detailed = matched[:12]
    if not detailed and re.search(
        r"(?:home\s+assistant|хаос|хоум\s*ассист|сущност|интеграц|устройств|девайс)",
        normalized_question,
    ):
        detailed = active[:64]

    def safe_device(item: dict[str, Any], *, with_entities: bool) -> dict[str, Any]:
        result = {
            "display_name": item.get("display_name"),
            "lifecycle": item.get("lifecycle"),
            "integration_paths": item.get("integration_paths", []),
            "platforms": item.get("platforms", []),
            "multiple_connection_paths": item.get("multiple_connection_paths"),
            "entity_count": item.get("entity_count"),
            "available_entity_count": item.get("available_entity_count"),
            "unavailable_entity_count": item.get("unavailable_entity_count"),
            "network_status": item.get("network_status"),
        }
        if with_entities:
            result["entities"] = item.get("entities", [])
        return result

    return {
        "connected_via": (
            "внутренний защищённый read-only адаптер; подключение уже работает; "
            "учётные данные и токен скрыты от модели"
        ),
        "ontology": {
            "entity": "одно состояние или управляемая функция Home Assistant",
            "ha_device_record": "принадлежащее интеграции представление с сущностями",
            "physical_device": "реальный прибор, объединённый по стабильным доказательствам идентичности",
            "integration_path": "один из нескольких способов появления того же прибора в HA",
            "network_node": "отдельное наблюдение LAN; один пропуск не доказывает отказ",
            "merge_rule": "не объединять только по похожему имени; сомнительные дубли требуют проверки",
        },
        "observed_at": document.get("observed_at"),
        "active_physical_device_count": document.get("active_physical_device_count"),
        "new_device_count": document.get("new_device_count"),
        "multiple_connection_device_count": document.get(
            "multiple_connection_device_count"
        ),
        "new_devices": [
            safe_device(item, with_entities=True)
            for item in active if item.get("lifecycle") == "new"
        ][:16],
        "matched_or_catalog_devices": [
            safe_device(item, with_entities=len(detailed) <= 12) for item in detailed
        ],
        "ambiguous_same_name_groups": document.get("review_candidates", [])[:16],
    }


def main() -> int:
    try:
        inventory = ha_entity_query.load_inventory()
        previous = read_catalog(missing_ok=True)
        document = build_catalog(inventory, previous)
        heartbeat._validate_state_dir(CATALOG_PATH.parent)
        heartbeat._atomic_write(
            CATALOG_PATH,
            json.dumps(
                document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8") + b"\n",
        )
    except (KnowledgeError, ha_entity_query.EntityQueryError, ha_read.AdapterError, OSError):
        print("HA_DEVICE_KNOWLEDGE_FAILED", file=sys.stderr)
        return 2
    print(json.dumps({
        "status": "learned",
        "active_physical_device_count": document["active_physical_device_count"],
        "new_device_count": document["new_device_count"],
        "multiple_connection_device_count": document[
            "multiple_connection_device_count"
        ],
        "actions_performed": 0,
    }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
