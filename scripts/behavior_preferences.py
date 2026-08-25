#!/usr/bin/env python3
"""Structured, owner-scoped Home Butler behavior preferences.

The conversational model may select only the closed tools defined here.  It
never writes prompt text, safety policy or executable configuration.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import memory_store
import recovery_playbook_registry


SCHEMA_VERSION = 1
PREFERENCE_KIND = "structured_behavior_preference"
MEMORY_KEY_PREFIX = "behavior:"
CATEGORIES = (
    "verbosity",
    "tone",
    "quiet_hours",
    "notification_thresholds",
    "incident_suppression_duration",
    "preferred_speaker",
    "aliases",
    "report_detail",
    "approved_r1_recovery_profiles",
)
CATEGORY_LABELS = {
    "verbosity": "подробность ответов",
    "tone": "тон общения",
    "quiet_hours": "тихие часы",
    "notification_thresholds": "пороги уведомлений",
    "incident_suppression_duration": "подавление кратких инцидентов",
    "preferred_speaker": "предпочитаемая колонка",
    "aliases": "псевдоним устройства",
    "report_detail": "подробность отчёта",
    "approved_r1_recovery_profiles": "разрешённые профили безопасного восстановления",
}
FORBIDDEN_TEXT_RE = re.compile(
    r"(?:ignore\s+(?:all\s+)?(?:previous|system)|system\s+prompt|"
    r"(?:^|\W)(?:root|sudo|shell|ssh|bearer|token|password|cookie)(?:\W|$)|"
    r"arbitrary\s+(?:service|command)|отключ\w*\s+(?:провер|cooldown|безопас)|"
    r"игнорир\w*\s+(?:инструк|правил)|секрет|парол|токен)",
    re.IGNORECASE,
)
SECRET_RE = re.compile(
    r"(?:\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b|"
    r"-----BEGIN\s+(?:OPENSSH|RSA|EC|PRIVATE)|Authorization\s*:\s*Bearer)",
    re.IGNORECASE,
)


class BehaviorPreferenceError(RuntimeError):
    """A closed behavior preference was invalid or unavailable."""


def _category(value: object, *, allow_all: bool = False) -> str:
    allowed = set(CATEGORIES)
    if allow_all:
        allowed.add("all")
    if not isinstance(value, str) or value not in allowed:
        raise BehaviorPreferenceError("behavior category is invalid")
    return value


def _safe_label(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise BehaviorPreferenceError(f"{field} is invalid")
    rendered = unicodedata.normalize("NFKC", value).strip()
    if (
        not 1 <= len(rendered) <= 80
        or any(ord(character) < 32 for character in rendered)
        or SECRET_RE.search(rendered)
        or FORBIDDEN_TEXT_RE.search(rendered)
    ):
        raise BehaviorPreferenceError(f"{field} is unsafe")
    return rendered


def _exact_mapping(value: object, keys: set[str], *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise BehaviorPreferenceError(f"{field} is invalid")
    return dict(value)


def _enum(value: object, allowed: set[str], *, field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise BehaviorPreferenceError(f"{field} is invalid")
    return value


def _integer(value: object, minimum: int, maximum: int, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise BehaviorPreferenceError(f"{field} is invalid")
    return value


def _validate_value(category: str, value: object) -> Any:
    if category == "verbosity":
        return _enum(value, {"concise", "balanced", "detailed"}, field=category)
    if category == "tone":
        return _enum(value, {"natural", "calm", "friendly", "formal"}, field=category)
    if category == "report_detail":
        return _enum(value, {"brief", "standard", "detailed"}, field=category)
    if category == "incident_suppression_duration":
        return _integer(value, 0, 86_400, field=category)
    if category == "preferred_speaker":
        return _safe_label(value, field=category)
    if category == "quiet_hours":
        document = _exact_mapping(value, {"start", "end", "timezone"}, field=category)
        for key in ("start", "end"):
            candidate = document[key]
            if not isinstance(candidate, str) or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", candidate) is None:
                raise BehaviorPreferenceError(f"quiet hours {key} is invalid")
        timezone = document["timezone"]
        if not isinstance(timezone, str) or not 1 <= len(timezone) <= 64:
            raise BehaviorPreferenceError("quiet hours timezone is invalid")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise BehaviorPreferenceError("quiet hours timezone is invalid") from error
        return document
    if category == "notification_thresholds":
        if not isinstance(value, dict) or not value:
            raise BehaviorPreferenceError("notification thresholds are invalid")
        allowed = {
            "wifi_outage_seconds",
            "device_unavailable_seconds",
            "diagnostic_min_severity",
        }
        if not set(value) <= allowed:
            raise BehaviorPreferenceError("notification thresholds are invalid")
        document: dict[str, Any] = {}
        if "wifi_outage_seconds" in value:
            document["wifi_outage_seconds"] = _integer(
                value["wifi_outage_seconds"], 10, 3_600, field="wifi outage threshold"
            )
        if "device_unavailable_seconds" in value:
            document["device_unavailable_seconds"] = _integer(
                value["device_unavailable_seconds"], 10, 3_600,
                field="device unavailable threshold",
            )
        if "diagnostic_min_severity" in value:
            document["diagnostic_min_severity"] = _enum(
                value["diagnostic_min_severity"],
                {"info", "warning", "error", "critical"},
                field="diagnostic minimum severity",
            )
        return document
    if category == "aliases":
        document = _exact_mapping(value, {"target", "alias"}, field=category)
        return {
            "target": _safe_label(document["target"], field="alias target"),
            "alias": _safe_label(document["alias"], field="alias value"),
        }
    if category == "approved_r1_recovery_profiles":
        if not isinstance(value, list) or len(value) > 16 or any(
            not isinstance(item, str) for item in value
        ):
            raise BehaviorPreferenceError("approved R1 profiles are invalid")
        selected = list(dict.fromkeys(value))
        allowed = {
            playbook.playbook_id
            for playbook in recovery_playbook_registry.PLAYBOOKS
            if playbook.risk_class == "R1" and playbook.automatic_permission
        }
        if any(item not in allowed for item in selected):
            raise BehaviorPreferenceError("approved R1 profile is not allow-listed")
        return selected
    raise BehaviorPreferenceError("behavior category is unsupported")


def _memory_key(category: str, value: Any) -> str:
    if category != "aliases":
        return MEMORY_KEY_PREFIX + category
    target = str(value["target"]).casefold().encode("utf-8")
    return MEMORY_KEY_PREFIX + "aliases:" + hashlib.blake2s(target, digest_size=12).hexdigest()


def _searchable_text(category: str, value: Any) -> str:
    label = CATEGORY_LABELS[category]
    if category == "aliases":
        return f"Настройка владельца — {label}: {value['target']} называется {value['alias']}."
    return f"Структурированная настройка владельца — {label}: {value}."


def _records(
    store: memory_store.MemoryStore,
    owner_scope: str,
    category: str | None = None,
) -> list[memory_store.MemoryRecord]:
    records = store.active_memories(owner_scope, memory_types=("owner",), limit=100)
    selected: list[memory_store.MemoryRecord] = []
    for record in records:
        payload = record.structured_payload
        if payload.get("kind") != PREFERENCE_KIND or payload.get("schema_version") != SCHEMA_VERSION:
            continue
        record_category = payload.get("category")
        if record_category not in CATEGORIES or (category is not None and record_category != category):
            continue
        selected.append(record)
    return selected


def behavior_get(
    category: object | None = None,
    *,
    store: memory_store.MemoryStore | None = None,
    owner_scope: str = memory_store.PRIMARY_OWNER_SCOPE,
) -> dict[str, Any]:
    selected_category = None if category is None else _category(category)
    database = memory_store.MemoryStore() if store is None else store
    preferences = [
        {
            "category": record.structured_payload["category"],
            "value": record.structured_payload["value"],
            "updated_at": record.updated_at,
        }
        for record in _records(database, owner_scope, selected_category)
    ]
    preferences.sort(key=lambda item: (str(item["category"]), str(item["value"])))
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ready",
        "category": selected_category,
        "preferences": preferences,
    }


def behavior_set(
    category: object,
    value: object,
    *,
    store: memory_store.MemoryStore | None = None,
    owner_scope: str = memory_store.PRIMARY_OWNER_SCOPE,
    source_transport: str = "dialogue",
    source_session: str | None = None,
) -> dict[str, Any]:
    selected_category = _category(category)
    normalized = _validate_value(selected_category, value)
    database = memory_store.MemoryStore() if store is None else store
    database.remember(
        memory_type="owner",
        owner_scope=owner_scope,
        source_transport=source_transport,
        source_session=source_session,
        source="structured_behavior_tool",
        confidence=1.0,
        memory_key=_memory_key(selected_category, normalized),
        searchable_text=_searchable_text(selected_category, normalized),
        structured_payload={
            "schema_version": SCHEMA_VERSION,
            "kind": PREFERENCE_KIND,
            "category": selected_category,
            "value": normalized,
        },
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "updated",
        "category": selected_category,
        "value": normalized,
    }


def behavior_reset(
    category: object,
    *,
    store: memory_store.MemoryStore | None = None,
    owner_scope: str = memory_store.PRIMARY_OWNER_SCOPE,
) -> dict[str, Any]:
    selected_category = _category(category, allow_all=True)
    database = memory_store.MemoryStore() if store is None else store
    records = _records(
        database,
        owner_scope,
        None if selected_category == "all" else selected_category,
    )
    for record in records:
        database.revoke(record.memory_id, owner_scope)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "reset",
        "category": selected_category,
        "removed": len(records),
    }


def model_view(
    store: memory_store.MemoryStore,
    owner_scope: str = memory_store.PRIMARY_OWNER_SCOPE,
) -> dict[str, Any]:
    """Return deterministic preference data, never executable prompt text."""
    result = behavior_get(store=store, owner_scope=owner_scope)
    return {
        "schema_version": SCHEMA_VERSION,
        "trust": "validated_owner_preferences_not_action_authority",
        "preferences": result["preferences"],
    }


def tool_definitions() -> list[dict[str, Any]]:
    category_schema = {"type": "string", "enum": list(CATEGORIES)}
    value_schemas: dict[str, dict[str, Any]] = {
        "verbosity": {"type": "string", "enum": ["concise", "balanced", "detailed"]},
        "tone": {"type": "string", "enum": ["natural", "calm", "friendly", "formal"]},
        "quiet_hours": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "pattern": "^(?:[01]\\d|2[0-3]):[0-5]\\d$"},
                "end": {"type": "string", "pattern": "^(?:[01]\\d|2[0-3]):[0-5]\\d$"},
                "timezone": {"type": "string", "maxLength": 64},
            },
            "required": ["start", "end", "timezone"],
            "additionalProperties": False,
        },
        "notification_thresholds": {
            "type": "object",
            "properties": {
                "wifi_outage_seconds": {"type": "integer", "minimum": 10, "maximum": 3600},
                "device_unavailable_seconds": {"type": "integer", "minimum": 10, "maximum": 3600},
                "diagnostic_min_severity": {
                    "type": "string", "enum": ["info", "warning", "error", "critical"]
                },
            },
            "minProperties": 1,
            "additionalProperties": False,
        },
        "incident_suppression_duration": {"type": "integer", "minimum": 0, "maximum": 86400},
        "preferred_speaker": {"type": "string", "minLength": 1, "maxLength": 80},
        "aliases": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "minLength": 1, "maxLength": 80},
                "alias": {"type": "string", "minLength": 1, "maxLength": 80},
            },
            "required": ["target", "alias"],
            "additionalProperties": False,
        },
        "report_detail": {"type": "string", "enum": ["brief", "standard", "detailed"]},
        "approved_r1_recovery_profiles": {
            "type": "array",
            "maxItems": 16,
            "uniqueItems": True,
            "items": {
                "type": "string",
                "enum": [
                    item.playbook_id
                    for item in recovery_playbook_registry.PLAYBOOKS
                    if item.risk_class == "R1" and item.automatic_permission
                ],
            },
        },
    }
    set_variants = [
        {
            "type": "object",
            "properties": {
                "category": {"type": "string", "const": category},
                "value": value_schemas[category],
            },
            "required": ["category", "value"],
            "additionalProperties": False,
        }
        for category in CATEGORIES
    ]
    return [
        {
            "type": "function",
            "function": {
                "name": "behavior_get",
                "description": "Read validated owner behavior preferences; never changes safety policy.",
                "parameters": {
                    "type": "object",
                    "properties": {"category": category_schema},
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "behavior_set",
                "description": "Set one allow-listed behavior preference from the current owner request.",
                "parameters": {"oneOf": set_variants},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "behavior_reset",
                "description": "Reset one allow-listed preference category or all behavior preferences.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "enum": [*CATEGORIES, "all"]}
                    },
                    "required": ["category"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def execute_tool(
    name: object,
    arguments: object,
    *,
    store: memory_store.MemoryStore | None = None,
    owner_scope: str = memory_store.PRIMARY_OWNER_SCOPE,
    source_transport: str = "dialogue",
    source_session: str | None = None,
) -> dict[str, Any]:
    if not isinstance(name, str) or not isinstance(arguments, Mapping):
        raise BehaviorPreferenceError("behavior tool call is invalid")
    values = dict(arguments)
    if name == "behavior_get":
        if not set(values) <= {"category"}:
            raise BehaviorPreferenceError("behavior_get arguments are invalid")
        return behavior_get(values.get("category"), store=store, owner_scope=owner_scope)
    if name == "behavior_set":
        if set(values) != {"category", "value"}:
            raise BehaviorPreferenceError("behavior_set arguments are invalid")
        return behavior_set(
            values["category"], values["value"], store=store, owner_scope=owner_scope,
            source_transport=source_transport, source_session=source_session,
        )
    if name == "behavior_reset":
        if set(values) != {"category"}:
            raise BehaviorPreferenceError("behavior_reset arguments are invalid")
        return behavior_reset(values["category"], store=store, owner_scope=owner_scope)
    raise BehaviorPreferenceError("behavior tool is not allow-listed")


def owner_message(result: Mapping[str, Any]) -> str:
    status = result.get("status")
    category = result.get("category")
    if status == "updated" and category in CATEGORY_LABELS:
        value = result.get("value")
        if category == "notification_thresholds" and isinstance(value, dict):
            wifi = value.get("wifi_outage_seconds")
            if isinstance(wifi, int):
                return f"Запомнил правило уведомлений: Wi‑Fi-сбои короче {wifi} секунд не озвучивать."
        if category == "aliases" and isinstance(value, dict):
            return f"Запомнил псевдоним: {value.get('target')} — {value.get('alias')}."
        return f"Запомнил настройку «{CATEGORY_LABELS[category]}»: {value}."
    if status == "reset":
        removed = result.get("removed", 0)
        label = "все настройки поведения" if category == "all" else CATEGORY_LABELS.get(str(category), "настройку")
        return f"Сбросил {label}. Удалено настроек: {removed}."
    if status == "ready":
        preferences = result.get("preferences")
        if not isinstance(preferences, list) or not preferences:
            return "Структурированных настроек поведения пока нет."
        rendered = "; ".join(
            f"{CATEGORY_LABELS.get(str(item.get('category')), str(item.get('category')))}: {item.get('value')}"
            for item in preferences
            if isinstance(item, dict)
        )
        return "Текущие настройки поведения: " + rendered + "."
    raise BehaviorPreferenceError("behavior tool result is invalid")
