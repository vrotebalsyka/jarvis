"""Independent Stage 71 oracle. It imports no production resolver or renderer."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


FORBIDDEN_CURRENT_KEYS = frozenset({
    "state", "state_kind", "state_value", "availability", "available",
    "observed_at", "last_updated", "source_last_updated_at", "current",
    "available_entity_count", "unavailable_entity_count",
})
UNSAFE_TEXT_RE = re.compile(
    r"(?:https?://|\b(?:10|127|169\.254|172\.(?:1[6-9]|2\d|3[01])|192\.168)"
    r"(?:\.\d{1,3}){2,3}\b|\b(?:alarm_control_panel|binary_sensor|button|camera|"
    r"climate|cover|fan|humidifier|light|lock|media_player|number|select|sensor|"
    r"switch|vacuum)\.[a-z0-9_]+\b|\b[a-f0-9]{32,64}\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class OracleResult:
    wrong_target: int = 0
    invented_facts: int = 0
    lost_requested_values: int = 0
    model_generated_entity_ids: int = 0


def _safe_text(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    normalized = unicodedata.normalize("NFKC", " ".join(value.split()))
    if (
        not normalized or len(normalized) > 180 or UNSAFE_TEXT_RE.search(normalized)
        or any(unicodedata.category(character).startswith("C") for character in normalized)
    ):
        return fallback
    return normalized


def walk(value: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], Any]]:
    yield path, value
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from walk(child, (*path, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, (*path, str(index)))


def persistent_current_fields(document: Mapping[str, Any]) -> list[str]:
    return [".".join(path) for path, _value in walk(document) if path and path[-1] in FORBIDDEN_CURRENT_KEYS]


def coverage(
    registry_entities: Sequence[Mapping[str, Any]],
    raw_states: Sequence[Mapping[str, Any]],
    document: Mapping[str, Any],
) -> dict[str, int]:
    current = {item.get("entity_id") for item in raw_states if isinstance(item.get("entity_id"), str)}
    disabled = {
        item.get("entity_id") for item in registry_entities
        if isinstance(item.get("entity_id"), str) and item.get("disabled_by") is not None
    }
    expected = current - disabled
    represented = {
        item.get("entity_id") for item in document.get("entities", [])
        if isinstance(item, Mapping) and not item.get("disabled")
    }
    return {
        "enabled_current": len(expected), "represented_enabled_current": len(expected & represented),
        "missing_enabled_current": len(expected - represented),
        "extra_enabled_metadata": len(represented - expected),
    }


def _expected_value(state: Mapping[str, Any], metadata: Mapping[str, Any], feature: str) -> tuple[str, Any]:
    kind, value = state.get("state_kind"), state.get("state_value")
    if kind in {"unknown", "unavailable", "redacted"}:
        return str(kind), None
    if kind == "number" and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
        return "number", value
    if isinstance(value, bool):
        return "boolean", value
    if isinstance(value, str) and value.casefold() in {"true", "false"}:
        return "boolean", value.casefold() == "true"
    if isinstance(value, str) and value.casefold() in {"on", "off"}:
        flag = value.casefold() == "on"
        if metadata.get("device_class") == "problem" or feature == "error":
            return "problem", flag
        if metadata.get("domain") == "binary_sensor":
            return "boolean", flag
        return "on_off", value.casefold()
    if isinstance(value, str) and value.casefold() in {"problem", "ok"}:
        return "problem", value.casefold() == "problem"
    if isinstance(value, str):
        return "enum", value
    return "redacted", None


def _selected_entities(
    document: Mapping[str, Any], target_ref: str, feature: str,
) -> list[Mapping[str, Any]]:
    members = [
        item for item in document.get("entities", [])
        if isinstance(item, Mapping) and item.get("target_ref") == target_ref
        and not item.get("disabled") and not item.get("hidden")
    ]
    members.sort(key=lambda item: str(item.get("entity_ref")))
    if feature == "unknown":
        return []
    if feature == "consumables":
        return [item for item in members if item.get("component") in {"main_brush", "side_brush", "filter"}]
    if feature == "status":
        selected = [item for item in members if item.get("component") == "status"]
        if not selected:
            selected = [item for item in members if item.get("component") in {"power", "mode", "error"}]
        return selected or members[:1]
    return [item for item in members if item.get("component") == feature]


def _expected_receipts(
    document: Mapping[str, Any], snapshot: Mapping[str, Any],
    targets: Sequence[str], features: Sequence[str],
) -> list[tuple[Any, ...]]:
    target_nodes = {
        item.get("target_ref"): item
        for key in ("physical_nodes", "logical_nodes")
        for item in document.get(key, []) if isinstance(item, Mapping)
    }
    area_names = {
        item.get("area_ref"): item.get("name")
        for item in document.get("area_nodes", []) if isinstance(item, Mapping)
    }
    states = {
        item.get("entity_id"): item for item in snapshot.get("entities", [])
        if isinstance(item, Mapping) and isinstance(item.get("entity_id"), str)
    }
    observed_at = snapshot.get("observed_at") if isinstance(snapshot.get("observed_at"), str) else None
    expected: list[tuple[Any, ...]] = []
    for target_ref in targets:
        target = target_nodes.get(target_ref, {})
        target_kind = str(target.get("kind") or "logical")
        target_label = _safe_text(target.get("display_name"), "Устройство")
        # Independent HA provenance: inferred resolver rooms are not registry
        # bindings and must never enter the expected factual receipt.
        areas = tuple(
            safe for ref in target.get("area_refs", []) if ref in area_names
            for safe in [_safe_text(area_names[ref], "")] if safe
        )
        for feature in features:
            entities = _selected_entities(document, target_ref, feature)
            if not entities:
                expected.append((
                    target_ref, None, target_kind, target_label, areas, feature,
                    "unknown", None, None, None, observed_at, None,
                ))
                continue
            selected: list[tuple[Any, ...]] = []
            for entity in entities:
                state = states.get(entity.get("entity_id"))
                kind, value = (
                    ("unavailable", None) if state is None
                    else _expected_value(state, entity, feature)
                )
                selected.append((
                    target_ref, entity.get("entity_ref"), target_kind, target_label,
                    areas, feature, kind, value, entity.get("unit"),
                    entity.get("device_class"), observed_at,
                    state.get("source_last_updated_at") if state is not None else None,
                ))
            grounded = [item for item in selected if item[6] not in {"unknown", "unavailable", "redacted"}]
            if grounded:
                selected = grounded
            unique: dict[tuple[Any, ...], tuple[Any, ...]] = {}
            for item in selected:
                unique.setdefault((item[5], item[6], item[7], item[8], item[9]), item)
            expected.extend(unique.values())
    return expected


FEATURE_LABELS = {
    "power": "питание", "status": "состояние", "battery": "заряд",
    "filter": "ресурс фильтра", "main_brush": "ресурс основной щётки",
    "side_brush": "ресурс боковой щётки", "humidity": "влажность",
    "temperature": "температура", "child_lock": "защита от детей",
    "mode": "режим", "error": "ошибка", "consumables": "расходник",
    "unknown": "показатель",
}
ENUM_TRANSLATIONS = {
    "off": "выключено", "on": "включено", "docked": "на базе",
    "cleaning": "убирает", "returning": "возвращается на базу",
    "idle": "ожидает", "running": "работает", "open": "открыто",
    "closed": "закрыто", "locked": "заблокировано", "unlocked": "разблокировано",
}


def _rendered_value(receipt: Any) -> str:
    kind, value = receipt.value_kind, receipt.value
    if kind == "number" and isinstance(value, (int, float)):
        number = float(value)
        rendered = str(int(number)) if number.is_integer() else str(number).replace(".", ",")
        unit = receipt.unit or ""
        return rendered + (" " if unit and unit[0].isalnum() else "") + unit
    if kind == "on_off":
        return "включено" if value == "on" else "выключено"
    if kind == "boolean":
        if receipt.device_class in {"motion", "occupancy", "presence"}:
            return "обнаружено" if value is True else "не обнаружено"
        if receipt.device_class in {"door", "window", "opening"}:
            return "открыто" if value is True else "закрыто"
        return "да" if value is True else "нет"
    if kind == "problem":
        return "есть проблема" if value is True else "проблем нет"
    if kind == "enum" and isinstance(value, str):
        return ENUM_TRANSLATIONS.get(value.casefold(), value)
    if kind == "unknown":
        return "значение неизвестно"
    if kind == "unavailable":
        return "недоступно"
    return "значение скрыто"


def _expected_answer(result: Any) -> str:
    grouped: dict[tuple[str, str], list[Any]] = {}
    for receipt in result.receipts:
        grouped.setdefault((receipt.target_ref, receipt.target_label), []).append(receipt)
    sections = [
        f"{label}: " + "; ".join(
            f"{FEATURE_LABELS.get(item.feature, item.feature.replace('_', ' '))} — {_rendered_value(item)}"
            for item in items
        )
        for (_target_ref, label), items in grouped.items()
    ]
    prefixes: list[str] = []
    if result.frame.control_requested:
        prefixes.append("Управление отключено; ничего не меняю.")
    if result.frame.causal_question and not any(item.causal_evidence for item in result.receipts):
        prefixes.append("Home Assistant не сообщает причину.")
    return " ".join([*prefixes, *sections])


def evaluate_turn(
    result: Any,
    document: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    expected_targets: Sequence[str],
    expected_features: Sequence[str],
) -> OracleResult:
    expected_target_set = set(expected_targets)
    actual_target_set = {selection.target_ref for selection in result.frame.selections}
    wrong = len(actual_target_set - expected_target_set) + len(expected_target_set - actual_target_set)
    expected_receipts = Counter(_expected_receipts(
        document, snapshot, sorted(expected_target_set), expected_features,
    ))
    actual_receipts = Counter((
        receipt.target_ref, receipt.entity_ref, receipt.target_kind,
        receipt.target_label, tuple(receipt.areas), receipt.feature,
        receipt.value_kind, receipt.value, receipt.unit, receipt.device_class,
        receipt.observed_at, receipt.source_updated_at,
    ) for receipt in result.receipts)
    lost = sum((expected_receipts - actual_receipts).values())
    invented = sum((actual_receipts - expected_receipts).values())
    if result.receipts and result.answer != _expected_answer(result):
        invented += 1
    return OracleResult(
        wrong_target=wrong,
        invented_facts=invented,
        lost_requested_values=lost,
        model_generated_entity_ids=getattr(result, "model_generated_entity_ids", 1),
    )


def combine(results: Sequence[OracleResult]) -> OracleResult:
    return OracleResult(
        wrong_target=sum(item.wrong_target for item in results),
        invented_facts=sum(item.invented_facts for item in results),
        lost_requested_values=sum(item.lost_requested_values for item in results),
        model_generated_entity_ids=sum(item.model_generated_entity_ids for item in results),
    )
