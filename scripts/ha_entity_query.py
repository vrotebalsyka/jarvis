#!/usr/bin/env python3
"""Dependency-free sanitized all-entity index for local LLM queries."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
import unicodedata
from pathlib import Path
from typing import Any


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_read as adapter  # noqa: E402
import incident_monitor  # noqa: E402


MAX_INVENTORY_BYTES = 8 * 1_048_576
AVAILABILITY_FILTERS = {"all", "available", "unavailable", "redacted"}


class EntityQueryError(RuntimeError):
    """Fixed, secret-free entity-query failure."""


def load_inventory() -> dict[str, Any]:
    path = incident_monitor._state_dir() / "inventory.json"
    try:
        metadata = path.lstat()
    except OSError as error:
        raise EntityQueryError("inventory unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
        or not 0 < metadata.st_size <= MAX_INVENTORY_BYTES
    ):
        raise EntityQueryError("inventory unavailable")
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            raw = os.read(descriptor, MAX_INVENTORY_BYTES + 1)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise EntityQueryError("inventory unavailable") from error
    if len(raw) > MAX_INVENTORY_BYTES:
        raise EntityQueryError("inventory unavailable")
    try:
        document = adapter.strict_json_loads(raw)
    except adapter.AdapterError as error:
        raise EntityQueryError("inventory unavailable") from error
    if not isinstance(document, dict):
        raise EntityQueryError("inventory unavailable")
    return document


def _safe_query(value: Any) -> str:
    if value in {None, ""}:
        return ""
    safe = adapter.sanitize_friendly_name(value)
    if safe is None:
        raise EntityQueryError("invalid search query")
    return unicodedata.normalize("NFKC", safe).casefold()


def _snapshot_index(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entities = snapshot.get("entities")
    if not isinstance(entities, list) or len(entities) > adapter.MAX_LISTED_ENTITIES:
        raise EntityQueryError("snapshot unavailable")
    result: dict[str, dict[str, Any]] = {}
    for item in entities:
        if not isinstance(item, dict):
            raise EntityQueryError("snapshot unavailable")
        try:
            entity_id = adapter._validate_entity_id(item.get("entity_id"))
        except adapter.AdapterError as error:
            raise EntityQueryError("snapshot unavailable") from error
        result[entity_id] = item
    return result


def _token_matches(token: str, haystack: str) -> bool:
    if token in haystack:
        return True
    letters = "".join(character for character in token if character.isalnum())
    return len(letters) >= 7 and letters[:6] in haystack


def search_entities(
    snapshot: dict[str, Any],
    inventory: dict[str, Any],
    *,
    query: Any = "",
    domain: Any = "",
    availability: Any = "all",
    offset: Any = 0,
    limit: Any = 32,
) -> dict[str, Any]:
    normalized_query = _safe_query(query)
    if not isinstance(domain, str) or re.fullmatch(r"[a-z0-9_]{0,64}", domain) is None:
        raise EntityQueryError("invalid entity domain")
    if availability not in AVAILABILITY_FILTERS:
        raise EntityQueryError("invalid availability filter")
    if not isinstance(offset, int) or not 0 <= offset <= 4095:
        raise EntityQueryError("invalid search offset")
    if not isinstance(limit, int) or not 1 <= limit <= 64:
        raise EntityQueryError("invalid search limit")
    states = _snapshot_index(snapshot)
    raw_inventory = inventory.get("entities")
    if not isinstance(raw_inventory, list) or len(raw_inventory) > 4096:
        raise EntityQueryError("inventory unavailable")
    metadata: dict[str, dict[str, Any]] = {}
    for item in raw_inventory:
        if not isinstance(item, dict):
            continue
        entity_id = item.get("entity_id")
        if not isinstance(entity_id, str) or entity_id not in states:
            continue
        friendly_name = adapter.sanitize_friendly_name(item.get("friendly_name"))
        platform = item.get("platform")
        physical_hash = item.get("physical_device_hash")
        metadata[entity_id] = {
            "friendly_name": friendly_name,
            "platform": (
                platform if isinstance(platform, str)
                and re.fullmatch(r"[a-z0-9_]{1,64}", platform) else "runtime"
            ),
            "physical_device_hash": (
                physical_hash if isinstance(physical_hash, str)
                and re.fullmatch(r"[a-f0-9]{64}", physical_hash) else
                hashlib.sha256(f"entity\0{entity_id}".encode("ascii")).hexdigest()
            ),
        }
    tokens = normalized_query.split()
    matches: list[dict[str, Any]] = []
    for entity_id, state in sorted(states.items()):
        entity_domain = entity_id.split(".", 1)[0]
        if domain and entity_domain != domain:
            continue
        state_kind = str(state.get("state_kind", "redacted"))
        state_availability = (
            "unavailable" if state_kind == "unavailable" else
            "redacted" if state_kind == "redacted" else "available"
        )
        if availability != "all" and availability != state_availability:
            continue
        item_metadata = metadata.get(entity_id, {})
        friendly_name = item_metadata.get("friendly_name")
        haystack = " ".join(
            value for value in (
                entity_id.casefold(),
                friendly_name.casefold() if isinstance(friendly_name, str) else "",
                str(item_metadata.get("platform", "runtime")),
            ) if value
        )
        if tokens and not all(_token_matches(token, haystack) for token in tokens):
            continue
        matches.append({
            "entity_id": entity_id,
            "friendly_name": friendly_name,
            "domain": entity_domain,
            "platform": item_metadata.get("platform", "runtime"),
            "physical_device_hash": item_metadata.get("physical_device_hash"),
            "state_kind": state_kind,
            "state_value": state.get("state_value"),
            "source_last_updated_at": state.get("source_last_updated_at"),
        })
    selected = matches[offset:offset + limit]
    return {
        "schema_version": 1,
        "source": "Home Assistant sanitized all-entity index",
        "read_scope": "all_entities",
        "matched_entity_count": len(matches),
        "returned_entity_count": len(selected),
        "offset": offset,
        "next_offset": (
            offset + len(selected)
            if offset + len(selected) < len(matches) else None
        ),
        "entities": selected,
    }


def get_device(
    snapshot: dict[str, Any], inventory: dict[str, Any], physical_hash: Any
) -> dict[str, Any]:
    if not isinstance(physical_hash, str) or re.fullmatch(
        r"[a-f0-9]{64}", physical_hash
    ) is None:
        raise EntityQueryError("invalid physical device")
    devices = inventory.get("physical_devices")
    if not isinstance(devices, list) or len(devices) > 4096:
        raise EntityQueryError("device inventory unavailable")
    selected = next(
        (
            item for item in devices
            if isinstance(item, dict)
            and item.get("physical_device_hash") == physical_hash
        ),
        None,
    )
    if selected is None:
        raise EntityQueryError("physical device unavailable")
    entity_ids = selected.get("entity_ids")
    if not isinstance(entity_ids, list) or len(entity_ids) > 512:
        raise EntityQueryError("physical device unavailable")
    states = _snapshot_index(snapshot)
    entities = [
        {
            "entity_id": entity_id,
            "state_kind": states[entity_id].get("state_kind"),
            "state_value": states[entity_id].get("state_value"),
            "source_last_updated_at": states[entity_id].get(
                "source_last_updated_at"
            ),
        }
        for entity_id in entity_ids
        if isinstance(entity_id, str) and entity_id in states
    ]
    display_name = adapter.sanitize_friendly_name(selected.get("display_name"))
    config_domains = selected.get("config_domains")
    safe_domains = sorted({
        item for item in config_domains
        if isinstance(item, str) and re.fullmatch(r"[a-z0-9_]{1,64}", item)
    }) if isinstance(config_domains, list) else []
    safety_class = selected.get("safety_class")
    network_status = selected.get("network_status")
    return {
        "schema_version": 1,
        "source": "Home Assistant sanitized physical-device index",
        "physical_device_hash": physical_hash,
        "display_name": display_name,
        "safety_class": (
            safety_class
            if safety_class in incident_monitor.SAFETY_CLASSES else "unknown"
        ),
        "network_status": (
            network_status if network_status in {
                "stable", "ip_changed", "not_observed", "unknown"
            } else "unknown"
        ),
        "config_domains": safe_domains,
        "entity_count": len(entities),
        "entities": entities,
    }
