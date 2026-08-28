#!/usr/bin/env python3
"""Bounded read-only learning for one real Home Assistant physical device."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import ha_entity_query  # noqa: E402
import home_assistant_mcp as ha_mcp  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402
import model_ha_proof  # noqa: E402
import model_runtime_policy  # noqa: E402
import model_workspace  # noqa: E402
import ollama_endpoint  # noqa: E402


SCHEMA_VERSION = 1
VALIDATOR_VERSION = "stage68-v1"
TEACHER_MODEL = model_runtime_policy.PRODUCTION_MODEL
MIN_VALIDATED = 75
POSITIVE_COUNT = 50
NEGATIVE_COUNT = 25
ALLOWED_ROLES = frozenset({
    "primary_control", "status", "battery", "mode", "cleaning", "dock",
    "consumable", "maintenance", "diagnostic", "configuration",
    "conditional_feature", "unknown",
})
CATEGORIES = (
    "READ", "COREFERENCE", "ACTION_SELECTION", "DIAGNOSTICS", "MAINTENANCE",
    "CONDITIONAL_AVAILABILITY", "UNKNOWN_FACT", "PARTIAL_FAILURE",
    "ACTION_VERIFICATION",
)
CAUSE_RE = re.compile(
    r"(?:из-за|причин[аы]\s*[-—:]?)\s*(?:wi-?fi|вай-?фай|сброс|завис|сервер|"
    r"интеграц|модул)|(?:wi-?fi|вай-?фай|сервер|модул)\s+(?:упал|слом|завис)",
    re.IGNORECASE,
)
ENTITY_ID_RE = re.compile(r"\b[a-z_][a-z0-9_]{0,63}\.[a-z_][a-z0-9_]{1,199}\b")
PRIVATE_RE = re.compile(
    r"(?:\b(?:\d{1,3}\.){3}\d{1,3}\b|"
    r"\b(?:[0-9A-F]{2}:){5}[0-9A-F]{2}\b|"
    r"\beyJ[A-Za-z0-9_-]{12,}\.|\b01[A-Z0-9]{20,}\b)", re.IGNORECASE,
)
NUMBER_RE = re.compile(r"(?<![\w])[-+]?\d+(?:[.,]\d+)?(?![\w])")
SAFE_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9 _().,%+:/-]{1,160}\Z")


class LearningError(RuntimeError):
    """The learning pipeline rejected unsafe or ungrounded material."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value: object, fallback: str = "unknown") -> str:
    if not isinstance(value, str):
        return fallback
    value = " ".join(unicodedata.normalize("NFKC", value).split())[:160]
    return value if value and SAFE_WORD_RE.fullmatch(value) else fallback


def _source_hash(snapshot: dict[str, Any], physical_id: str) -> str:
    observed = str(snapshot.get("observed_at", ""))
    entities = snapshot.get("entities", [])
    bounded = [
        item for item in entities
        if isinstance(item, dict) and isinstance(item.get("entity_id"), str)
    ]
    raw = json.dumps(
        {"physical_device_id": physical_id, "observed_at": observed, "entities": bounded},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _feature_ref(physical_id: str, entity_id: str) -> str:
    digest = hashlib.sha256(f"{physical_id}\0{entity_id}".encode("utf-8")).hexdigest()
    return f"feature-{digest[:20]}"


def _normalized_metadata(feature: dict[str, Any]) -> str:
    values = [
        feature.get("component"), feature.get("human_name"), feature.get("original_name"),
        feature.get("translation_key"), feature.get("domain"), feature.get("device_class"),
    ]
    return " ".join(str(value) for value in values if value not in {None, ""}).casefold()


def _component_and_role(feature: dict[str, Any], device_type: str) -> tuple[str, str]:
    text = _normalized_metadata(feature)
    domain = str(feature.get("domain", "unknown"))
    rules = (
        (r"start.?charge|док|зарядк", "dock", "dock"),
        (r"battery|батар|заряд", "battery", "battery"),
        (r"hypa|filter|фильтр", "filter", "maintenance"),
        (r"main.?brush|основн.*щ[её]тк", "main_brush", "maintenance"),
        (r"side.?brush|боков.*щ[её]тк", "side_brush", "maintenance"),
        (r"mop.?life|ресурс.*швабр", "mop", "maintenance"),
        (r"rinse.?aid|ополаскив", "rinse_aid", "diagnostic"),
        (r"salt|соль", "salt", "diagnostic"),
        (r"error|ошиб", "error", "diagnostic"),
        (r"time.?remaining|оставш.*врем", "time_remaining", "status"),
        (r"progress|этап|прогресс", "progress", "status"),
        (r"door|двер", "door", "status"),
        (r"status|состояни", "main_status", "status"),
        (r"power|питани", "power", "primary_control"),
        (r"delay|отлож", "delay_start", "configuration"),
        (r"extra.?dry|экстра.*суш", "extra_dry", "configuration"),
        (r"half.?load|половин", "half_load", "configuration"),
        (r"mode|режим", "mode", "mode"),
        (r"suction|всасыван", "suction", "configuration"),
        (r"water|вода", "water", "configuration"),
        (r"volume|громк", "volume", "configuration"),
        (r"alarm|сигнал", "alarm", "configuration"),
        (r"start|начал|sweep|clean", "cleaning", "cleaning"),
        (r"stop|останов", "stop", "cleaning"),
        (r"temperature|температур", "temperature", "diagnostic"),
    )
    for pattern, component, role in rules:
        if re.search(pattern, text):
            return component, role
    if domain == "vacuum":
        return "main", "primary_control"
    if domain in {"switch", "button", "lock", "select", "number"}:
        return _canonical(feature.get("component"), domain).replace(" ", "_")[:64], "configuration"
    return _canonical(feature.get("component"), domain).replace(" ", "_")[:64], "unknown"


def _device_type(details: dict[str, Any]) -> str:
    text = " ".join([
        str(details.get("display_name", "")),
        *[str(value) for value in details.get("models", [])],
        *[str(item.get("domain", "")) for item in details.get("features", []) if isinstance(item, dict)],
    ]).casefold()
    if "vacuum" in text or "пылесос" in text or "ijai" in text:
        return "robot_vacuum"
    if "dishwasher" in text or "посудом" in text or "760ey" in text:
        return "dishwasher"
    return "home_assistant_device"


def _state(feature: dict[str, Any]) -> tuple[str, Any]:
    state = feature.get("state")
    if isinstance(state, dict):
        return str(state.get("kind", "unknown")), state.get("value")
    return str(feature.get("state_kind", "unknown")), feature.get("state_value")


def _availability_policy(
    feature: dict[str, Any], component: str, device_type: str, power_state: Any,
) -> tuple[str, str | None, str]:
    availability = str(feature.get("availability", "unknown"))
    if (
        device_type == "dishwasher" and availability == "unavailable"
        and component in {"mode", "time_remaining", "cleaning", "delay_start", "extra_dry", "half_load"}
        and power_state == "off"
    ):
        return "conditional", "power:on", "conditional_feature"
    if availability == "unavailable":
        return "feature_unavailable_unknown_cause", None, "conditional_feature"
    return "normally_available", None, "available"


def _capability(feature: dict[str, Any], ref: str, component: str) -> dict[str, Any] | None:
    capability = str(feature.get("capability", "observe"))
    domain = str(feature.get("domain", "unknown"))
    if capability not in {"control", "press", "set_value"} and domain not in {
        "vacuum", "switch", "button", "lock", "select", "number",
    }:
        return None
    if domain == "button" or capability == "press":
        verification = "accepted_unverified; require a reviewed state/progress transition"
    elif domain == "vacuum":
        verification = "verified only by a relevant vacuum state transition/readback"
    else:
        verification = "verified only when a readback equals the requested state/value"
    return {
        "capability_ref": f"capability-{ref.removeprefix('feature-')}",
        "feature_ref": ref,
        "human_name": _canonical(feature.get("human_name"), component),
        "domain": domain,
        "component": component,
        "action": capability,
        "verification_rule": verification,
    }


def build_profile(details: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    physical_id = details.get("physical_device_id")
    if not isinstance(physical_id, str) or re.fullmatch(r"[a-f0-9]{64}", physical_id) is None:
        raise LearningError("physical device identity is invalid")
    raw_features = details.get("features")
    if not isinstance(raw_features, list) or not raw_features:
        raise LearningError("device has no learnable features")
    device_type = _device_type(details)
    power_state: Any = None
    for raw in raw_features:
        if isinstance(raw, dict) and _component_and_role(raw, device_type)[0] == "power":
            power_state = _state(raw)[1]
    features: list[dict[str, Any]] = []
    capabilities: list[dict[str, Any]] = []
    for raw in raw_features:
        if not isinstance(raw, dict):
            continue
        entity_id = raw.get("entity_id")
        if not isinstance(entity_id, str):
            raise LearningError("feature identity is missing")
        ref = _feature_ref(physical_id, entity_id)
        component, role = _component_and_role(raw, device_type)
        availability_policy, conditional_on, availability_class = _availability_policy(
            raw, component, device_type, power_state
        )
        if availability_class == "conditional_feature":
            role = "conditional_feature"
        state_kind, state_value = _state(raw)
        semantic_attributes = raw.get("semantic_attributes", {})
        device_class = None
        unit = None
        if isinstance(semantic_attributes, dict):
            raw_class = semantic_attributes.get("device_class")
            raw_unit = semantic_attributes.get("unit_of_measurement")
            if isinstance(raw_class, dict):
                device_class = _canonical(raw_class.get("text"), "") or None
            if isinstance(raw_unit, dict):
                unit = _canonical(raw_unit.get("text"), "") or None
        human_name = _canonical(raw.get("human_name"), component)
        evidence = [
            "ha_registry:domain",
            "ha_registry:component",
            "ha_state:availability",
            "ha_state:current_state",
        ]
        feature = {
            "feature_ref": ref,
            "human_name": human_name,
            "domain": _canonical(raw.get("domain")),
            "component": component,
            "semantic_role": role,
            "device_class": device_class,
            "unit": unit,
            "availability": _canonical(raw.get("availability")),
            "current_state": {"kind": state_kind, "value": state_value},
            "availability_policy": availability_policy,
            "conditional_on": conditional_on,
            "normal_semantics": "Report only the current Home Assistant fact without extrapolation.",
            "abnormal_semantics": (
                "An unavailable feature is a partial feature failure; its cause remains unknown."
                if raw.get("availability") == "unavailable" else
                "Only a confirmed abnormal state may be reported as a problem."
            ),
            "diagnostic_importance": (
                "high" if role in {"status", "diagnostic", "battery"} else
                "medium" if role in {"maintenance", "primary_control"} else "low"
            ),
            "confidence": 1.0 if role != "unknown" else 0.6,
            "evidence": evidence,
        }
        features.append(feature)
        learned_capability = _capability(raw, ref, component)
        if learned_capability is not None:
            capabilities.append(learned_capability)
    groups = {
        "primary_features": [item["feature_ref"] for item in features if item["semantic_role"] in {"primary_control", "status"}],
        "secondary_features": [item["feature_ref"] for item in features if item["semantic_role"] in {"mode", "cleaning", "dock", "configuration", "unknown"}],
        "diagnostic_features": [item["feature_ref"] for item in features if item["semantic_role"] in {"diagnostic", "battery"}],
        "maintenance_features": [item["feature_ref"] for item in features if item["semantic_role"] == "maintenance"],
        "conditional_features": [item["feature_ref"] for item in features if item["availability_policy"] == "conditional" or item["semantic_role"] == "conditional_feature"],
    }
    device = next(
        (item for item in inventory.get("physical_devices", []) if isinstance(item, dict) and item.get("physical_device_hash") == physical_id),
        {},
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "physical_device_id": physical_id,
        "stable_id": physical_id[:24],
        "display_name": _canonical(details.get("display_name"), "Без имени"),
        "device_type": device_type,
        "manufacturer": _canonical(next(iter(details.get("manufacturers", [])), "unknown")),
        "model": _canonical(next(iter(details.get("models", [])), "unknown")),
        "areas": [_canonical(value) for value in details.get("areas", []) if _canonical(value) != "unknown"],
        "integration_paths": sorted({_canonical(value) for value in device.get("config_domains", []) if _canonical(value) != "unknown"}),
        "physical_availability": _canonical(details.get("physical_availability")),
        "features": features,
        "capabilities": capabilities,
        "health_model": groups,
        "learning_policy": {
            "facts_from_model_answers": False,
            "unknown_cause_stays_unknown": True,
            "accepted_is_verified": False,
            "partial_feature_failure_is_device_outage": False,
        },
    }


def teacher_semantic_analysis(profile: dict[str, Any]) -> dict[str, Any]:
    projection = {
        "device_type": profile["device_type"],
        "features": [
            {
                "feature_ref": item["feature_ref"], "human_name": item["human_name"],
                "domain": item["domain"], "component": item["component"],
                "semantic_role": item["semantic_role"], "availability": item["availability"],
            }
            for item in profile["features"]
        ],
    }
    prompt = (
        "Ты read-only учитель семантики Home Assistant. Проверь классификацию, не "
        "добавляй фактов и причин. Верни JSON: {features:[{feature_ref,semantic_role,component}]}. "
        f"Допустимые роли: {sorted(ALLOWED_ROLES)}. Данные: "
        + json.dumps(projection, ensure_ascii=False, separators=(",", ":"))
    )
    payload = model_runtime_policy.build_chat_payload(
        "structured", [{"role": "user", "content": prompt}], response_format={"type": "object"}
    )
    endpoint = ollama_endpoint.load_runtime_ollama_endpoint()
    response = model_ha_proof.call_ollama(endpoint, "/api/chat", payload, timeout=180)
    message = response.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or len(content) > 64_000:
        raise LearningError("teacher returned no bounded analysis")
    try:
        document = json.loads(content)
    except json.JSONDecodeError as error:
        raise LearningError("teacher returned invalid analysis") from error
    known = {item["feature_ref"] for item in profile["features"]}
    accepted: list[dict[str, str]] = []
    for item in document.get("features", []) if isinstance(document, dict) else []:
        if not isinstance(item, dict):
            continue
        ref = item.get("feature_ref")
        role = item.get("semantic_role")
        component = item.get("component")
        if ref in known and role in ALLOWED_ROLES and isinstance(component, str) and re.fullmatch(r"[a-z0-9_]{1,64}", component):
            accepted.append({"feature_ref": ref, "semantic_role": role, "component": component})
    return {
        "teacher_model": TEACHER_MODEL,
        "mode": "advisory_only_deterministically_bounded",
        "accepted_suggestions": accepted,
        "suggestion_count": len(accepted),
    }


def _fact_projection(profile: dict[str, Any], features: Iterable[dict[str, Any]]) -> dict[str, Any]:
    projected = []
    for item in features:
        projected.append({
            "feature_ref": item["feature_ref"],
            "human_name": item["human_name"],
            "component": item["component"],
            "semantic_role": item["semantic_role"],
            "availability": item["availability"],
            "state": item["current_state"],
            "availability_policy": item["availability_policy"],
            "conditional_on": item["conditional_on"],
        })
    return {
        "device_name": profile["display_name"],
        "device_type": profile["device_type"],
        "physical_availability": profile["physical_availability"],
        "features": projected,
    }


def _render_fact(name: str, feature: dict[str, Any]) -> str:
    value = feature["current_state"].get("value")
    availability = feature["availability"]
    component = feature["component"]
    if availability == "unavailable":
        return f"{name}: функция «{feature['human_name']}» недоступна; причина по текущим данным не подтверждена."
    if component == "battery" and isinstance(value, (int, float)):
        return f"{name}: заряд {value:g}%."
    if feature["semantic_role"] == "maintenance" and isinstance(value, (int, float)):
        return f"{name}: ресурс «{feature['human_name']}» — {value:g}%."
    if feature.get("domain") == "button":
        return f"{name}: функция «{feature['human_name']}» доступна."
    if component == "main_status" and str(value).casefold() == "charging":
        return f"{name} находится на док-станции и заряжается."
    return f"{name}: «{feature['human_name']}» — {value}."


def _example(
    profile: dict[str, Any], snapshot_hash: str, index: int, category: str,
    polarity: str, question: str, expected: str, selected: list[dict[str, Any]],
    *, capability: dict[str, Any] | None = None, receipt: str | None = None,
) -> dict[str, Any]:
    context: dict[str, Any] = {"facts": _fact_projection(profile, selected)}
    if capability is not None:
        context["capability"] = capability
    if receipt is not None:
        context["action_receipt"] = receipt
    digest = hashlib.sha256(
        f"{profile['physical_device_id']}\0{polarity}\0{index}\0{question}".encode("utf-8")
    ).hexdigest()[:20]
    return {
        "id": f"stage68-{digest}",
        "source_device": profile["stable_id"],
        "source_snapshot_hash": snapshot_hash,
        "category": category,
        "polarity": polarity,
        "input": question,
        "context": context,
        "expected": expected,
        "evidence": [item["feature_ref"] for item in selected],
        "validator_version": VALIDATOR_VERSION,
        "teacher_model": TEACHER_MODEL,
        "created_at": _now(),
    }


def generate_examples(profile: dict[str, Any], snapshot_hash: str) -> list[dict[str, Any]]:
    name = profile["display_name"]
    features = profile["features"]
    available = [item for item in features if item["availability"] == "available"] or features
    unavailable = [item for item in features if item["availability"] == "unavailable"]
    capabilities = profile["capabilities"]
    examples: list[dict[str, Any]] = []
    positive_questions = (
        "Что с {name}?", "Как там {name}?", "Что показывает {feature}?",
        "Расскажи про {feature} у {name}.", "Каково состояние {feature}?",
        "А {feature}?", "Проверь {feature} у {name}.",
    )
    for index in range(POSITIVE_COUNT):
        feature = available[index % len(available)]
        category = CATEGORIES[index % len(CATEGORIES)]
        capability = capabilities[index % len(capabilities)] if capabilities and category in {"ACTION_SELECTION", "ACTION_VERIFICATION"} else None
        if category == "ACTION_SELECTION" and capability is not None:
            question = f"Как безопасно управлять функцией «{capability['human_name']}» у {name}?"
            expected = f"Для функции «{capability['human_name']}» доступно действие {capability['action']}; результат нужно проверить чтением состояния."
        elif category == "ACTION_VERIFICATION" and capability is not None:
            question = f"Команда для «{capability['human_name']}» уже означает физический успех?"
            expected = f"Нет. Для «{capability['human_name']}» правило проверки: {capability['verification_rule']}."
        else:
            question = positive_questions[index % len(positive_questions)].format(name=name, feature=feature["human_name"])
            expected = _render_fact(name, feature)
        examples.append(_example(profile, snapshot_hash, index, category, "positive", question, expected, [feature], capability=capability))
    for offset in range(NEGATIVE_COUNT):
        index = POSITIVE_COUNT + offset
        feature = (unavailable or available)[offset % len(unavailable or available)]
        mode = offset % 5
        if mode == 0:
            question = f"У {name} сломался Wi-Fi, поэтому функция недоступна?"
            expected = "Причина по текущим данным не подтверждена."
            category = "UNKNOWN_FACT"
        elif mode == 1:
            question = f"Одна функция {name} недоступна — значит весь прибор сломан?"
            expected = f"Нет. Сам прибор «{name}» доступен; состояние отдельной функции не доказывает отказ всего устройства."
            category = "PARTIAL_FAILURE"
        elif mode == 2:
            question = f"Если команда для {name} принята, она уже физически выполнена?"
            expected = "Нет. Принятие команды не равно подтверждённому выполнению; нужен проверяемый readback или переход состояния."
            category = "ACTION_VERIFICATION"
        elif mode == 3 and profile["device_type"] == "dishwasher":
            question = "Питание посудомойки включено — значит цикл мойки уже запущен?"
            expected = "Нет. Питание on не подтверждает запуск цикла; нужен переход status, progress или time remaining."
            category = "ACTION_VERIFICATION"
        else:
            question = f"Можно назвать точную причину состояния «{feature['human_name']}» у {name}?"
            expected = "Нет. Причина по текущим данным не подтверждена."
            category = "DIAGNOSTICS"
        capability = capabilities[offset % len(capabilities)] if capabilities and category == "ACTION_VERIFICATION" else None
        examples.append(_example(profile, snapshot_hash, index, category, "negative", question, expected, [feature], capability=capability, receipt="accepted_unverified" if category == "ACTION_VERIFICATION" else None))
    return examples


def _all_fact_numbers(context: dict[str, Any]) -> set[str]:
    raw = json.dumps(context, ensure_ascii=False, sort_keys=True)
    values = {value.replace(",", ".") for value in NUMBER_RE.findall(raw)} | {"0", "1"}
    normalized = set(values)
    for value in values:
        try:
            number = float(value)
        except ValueError:
            continue
        if number.is_integer():
            normalized.add(str(int(number)))
    return normalized


def validate_example(example: dict[str, Any], profile: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    expected = example.get("expected")
    context = example.get("context")
    if not isinstance(expected, str) or not isinstance(context, dict):
        return ["invalid_shape"]
    if ENTITY_ID_RE.search(expected):
        reasons.append("entity_id_exposed")
    if PRIVATE_RE.search(expected):
        reasons.append("private_identifier_exposed")
    if CAUSE_RE.search(expected):
        reasons.append("invented_cause")
    output_numbers = {value.replace(",", ".") for value in NUMBER_RE.findall(expected)}
    if not output_numbers <= _all_fact_numbers(context):
        reasons.append("number_not_in_source_facts")
    facts = context.get("facts", {})
    physical = facts.get("physical_availability") if isinstance(facts, dict) else None
    if physical == "available" and re.search(r"(?:весь|сам)\s+(?:прибор|робот|устройство).{0,20}(?:слом|недоступ|offline|офлайн)", expected, re.I):
        reasons.append("feature_confused_with_physical_device")
    receipt = context.get("action_receipt")
    if receipt == "accepted_unverified" and re.search(r"\b(?:готово|выполнено|подтверждено|verified)\b", expected, re.I):
        reasons.append("accepted_called_verified")
    if example.get("category") in {"ACTION_SELECTION", "ACTION_VERIFICATION"}:
        capability = context.get("capability")
        if not isinstance(capability, dict) or capability not in profile.get("capabilities", []):
            reasons.append("capability_missing")
        elif not capability.get("verification_rule"):
            reasons.append("verification_rule_missing")
    if example.get("category") not in CATEGORIES:
        reasons.append("unknown_category")
    return sorted(set(reasons))


def deliberately_rejected_examples(profile: dict[str, Any], snapshot_hash: str) -> list[dict[str, Any]]:
    feature = profile["features"][0]
    bad_outputs = (
        "Устройство сломалось из-за Wi-Fi.",
        "Датчик sensor.private_entity показывает ошибку.",
        "Команда принята: готово, физическое выполнение подтверждено.",
        "Сам прибор недоступен, потому что одна функция unavailable.",
        "Температура сейчас 999 градусов.",
    )
    result = []
    for index, output in enumerate(bad_outputs):
        item = _example(
            profile, snapshot_hash, 1000 + index,
            "ACTION_VERIFICATION" if index == 2 else "DIAGNOSTICS",
            "adversarial", "Проверка валидатора", output, [feature],
            receipt="accepted_unverified" if index == 2 else None,
        )
        result.append(item)
    return result


def validate_corpus(
    generated: list[dict[str, Any]], profile: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item in generated:
        reasons = validate_example(item, profile)
        if reasons:
            rejected.append({**item, "rejection_reasons": reasons})
        else:
            accepted.append(item)
    return accepted, rejected


def _jsonl(items: Iterable[dict[str, Any]]) -> str:
    return "".join(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for item in items)


def persist_learning(
    profile: dict[str, Any], generated: list[dict[str, Any]],
    validated: list[dict[str, Any]], rejected: list[dict[str, Any]],
    *, root: Path | None = None,
) -> dict[str, Any]:
    workspace_root = model_workspace.WORKSPACE_ROOT if root is None else root
    stable_id = profile["stable_id"]
    paths = {
        "profile": f"knowledge/devices/{stable_id}.json",
        "generated": f"knowledge/training/generated/{stable_id}.jsonl",
        "validated": f"knowledge/training/validated/{stable_id}.jsonl",
        "rejected": f"reports/learning-rejected/{stable_id}.jsonl",
    }
    model_workspace.write_text(paths["profile"], json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", workspace_root)
    model_workspace.write_text(paths["generated"], _jsonl(generated), workspace_root)
    model_workspace.write_text(paths["validated"], _jsonl(validated), workspace_root)
    if rejected:
        model_workspace.write_text(paths["rejected"], _jsonl(rejected), workspace_root)
    else:
        paths["rejected"] = ""
    return paths


def load_profile(physical_id: str, *, root: Path | None = None) -> dict[str, Any]:
    if re.fullmatch(r"[a-f0-9]{64}", physical_id) is None:
        raise LearningError("physical device identity is invalid")
    workspace_root = model_workspace.WORKSPACE_ROOT if root is None else root
    relative = f"knowledge/devices/{physical_id[:24]}.json"
    try:
        result = model_workspace.read_text(relative, workspace_root)
    except model_workspace.WorkspaceError as error:
        raise LearningError("device profile is unavailable") from error
    if result.get("truncated") is True:
        raise LearningError("device profile is too large for voice retrieval")
    try:
        profile = json.loads(result["content"])
    except (KeyError, json.JSONDecodeError) as error:
        raise LearningError("device profile is invalid") from error
    if (
        not isinstance(profile, dict)
        or profile.get("physical_device_id") != physical_id
        or profile.get("schema_version") != 1
    ):
        raise LearningError("device profile is invalid")
    return profile


def compact_profile(
    profile: dict[str, Any], live_details: dict[str, Any], question: str,
    *, maximum: int = 8,
) -> dict[str, Any]:
    """Return only 3–8 relevant live features plus learned semantics."""
    if not 3 <= maximum <= 8:
        raise LearningError("compact profile feature limit is invalid")
    physical_id = profile.get("physical_device_id")
    if live_details.get("physical_device_id") != physical_id:
        raise LearningError("live facts do not belong to learned profile")
    learned = {
        item.get("feature_ref"): item
        for item in profile.get("features", []) if isinstance(item, dict)
    }
    candidates: list[dict[str, Any]] = []
    normalized = unicodedata.normalize("NFKC", question).casefold()
    query_tokens = set(re.findall(r"[a-zа-яё0-9]+", normalized))
    role_priority = {
        "status": 70, "primary_control": 65, "battery": 60,
        "diagnostic": 55, "maintenance": 45, "conditional_feature": 40,
        "dock": 35, "mode": 30, "cleaning": 30, "configuration": 20,
        "unknown": 5,
    }
    for raw in live_details.get("features", []):
        if not isinstance(raw, dict) or not isinstance(raw.get("entity_id"), str):
            continue
        ref = _feature_ref(str(physical_id), raw["entity_id"])
        semantic = learned.get(ref)
        if not isinstance(semantic, dict):
            continue
        searchable = " ".join([
            str(semantic.get("human_name", "")), str(semantic.get("component", "")),
            str(semantic.get("semantic_role", "")),
        ]).casefold()
        score = role_priority.get(str(semantic.get("semantic_role")), 0)
        score += 100 * sum(token in searchable for token in query_tokens if len(token) >= 3)
        component = str(semantic.get("component", ""))
        requested_components = (
            (r"батар|заряд", "battery"), (r"фильтр", "filter"),
            (r"основн.*щ[её]тк", "main_brush"),
            (r"боков.*щ[её]тк", "side_brush"), (r"швабр", "mop"),
            (r"статус|состояни", "main_status"), (r"ошиб", "error"),
            (r"ополаскив", "rinse_aid"), (r"соль", "salt"),
        )
        score += 300 * sum(
            component == expected and re.search(pattern, normalized) is not None
            for pattern, expected in requested_components
        )
        if raw.get("availability") == "unavailable":
            score += 25
        candidates.append({
            "score": score,
            "feature_ref": ref,
            "human_name": semantic.get("human_name"),
            "domain": semantic.get("domain"),
            "component": semantic.get("component"),
            "semantic_role": semantic.get("semantic_role"),
            "unit": semantic.get("unit"),
            "availability": raw.get("availability"),
            "state": raw.get("state"),
            "availability_policy": semantic.get("availability_policy"),
            "conditional_on": semantic.get("conditional_on"),
        })
    candidates.sort(key=lambda item: (-int(item["score"]), str(item["feature_ref"])))
    selected = candidates[:maximum]
    compact_features: list[dict[str, Any]] = []
    for item in selected:
        compact = {
            key: item.get(key)
            for key in (
                "human_name", "component", "semantic_role", "availability", "state"
            )
        }
        for optional in ("unit", "availability_policy", "conditional_on"):
            value = item.get(optional)
            if value is not None and value != "" and value != "always_available":
                compact[optional] = value
        compact_features.append(compact)
    return {
        "schema_version": 1,
        "source": "learned profile plus current read-only HA facts",
        "display_name": profile.get("display_name"),
        "device_type": profile.get("device_type"),
        "physical_availability": live_details.get("physical_availability"),
        "available_feature_count": live_details.get(
            "available_feature_count",
            sum(
                item.get("availability") == "available"
                for item in live_details.get("features", []) if isinstance(item, dict)
            ),
        ),
        "unavailable_feature_count": live_details.get(
            "unavailable_feature_count",
            sum(
                item.get("availability") == "unavailable"
                for item in live_details.get("features", []) if isinstance(item, dict)
            ),
        ),
        "relevant_features": compact_features,
        "relevant_feature_count": len(selected),
        "unknown_cause_stays_unknown": True,
        "accepted_is_verified": False,
    }


def render_compact_observation(document: dict[str, Any], question: str) -> str:
    """Render a strictly grounded read fallback from an already compact profile."""
    name = str(document.get("display_name") or "Устройство")
    features = [
        item for item in document.get("relevant_features", [])
        if isinstance(item, dict)
    ]
    normalized = unicodedata.normalize("NFKC", question).casefold()

    requested = (
        (r"батар|заряд", "battery"),
        (r"фильтр", "filter"),
        (r"основн.*щ[её]тк", "main_brush"),
        (r"боков.*щ[её]тк", "side_brush"),
        (r"швабр", "mop"),
        (r"ополаскив", "rinse_aid"),
        (r"соль", "salt"),
        (r"ошиб", "error"),
    )
    component = next(
        (value for pattern, value in requested if re.search(pattern, normalized)),
        None,
    )
    if component is not None:
        feature = next(
            (item for item in features if item.get("component") == component),
            None,
        )
        if feature is None:
            return f"{name}: запрошенная функция не найдена в текущих проверенных данных."
        if feature.get("availability") == "unavailable":
            return (
                f"{name}: функция «{feature.get('human_name', component)}» недоступна. "
                "Причина по текущим данным не подтверждена."
            )
        state = feature.get("state")
        value = state.get("value") if isinstance(state, dict) else None
        if isinstance(value, (int, float)):
            suffix = "%" if component in {"battery", "filter", "main_brush", "side_brush", "mop"} else ""
            labels = {
                "battery": "заряд",
                "filter": "ресурс фильтра",
                "main_brush": "ресурс основной щётки",
                "side_brush": "ресурс боковой щётки",
                "mop": "ресурс швабры",
            }
            label = labels.get(component, str(feature.get("human_name") or component))
            return f"{name}: {label} — {value:g}{suffix}."
        return f"{name}: «{feature.get('human_name', component)}» — {value}."

    unavailable = document.get("unavailable_feature_count")
    asks_problem = re.search(r"проблем|недоступ|почему|ошиб", normalized) is not None
    if asks_problem and isinstance(unavailable, int):
        prefix = f"Само устройство «{name}» доступно. " if document.get("physical_availability") == "available" else ""
        if unavailable:
            return (
                f"{prefix}Недоступны {unavailable} отдельные функции; "
                "причина по текущим данным не подтверждена."
            )
        return f"{prefix}Недоступных функций сейчас нет."

    status = next(
        (item for item in features if item.get("component") == "main_status" and item.get("availability") == "available"),
        None,
    )
    battery = next(
        (
            item for item in features
            if item.get("component") == "battery"
            and item.get("availability") == "available"
            and isinstance(item.get("state"), dict)
            and isinstance(item["state"].get("value"), (int, float))
        ),
        None,
    )
    parts: list[str] = []
    status_value = status.get("state", {}).get("value") if isinstance(status, dict) else None
    if str(status_value).casefold() == "charging":
        parts.append(f"{name} находится на док-станции и заряжается")
    elif status_value is not None:
        parts.append(f"{name}: состояние — {status_value}")
    if battery is not None:
        parts.append(f"заряд {battery['state']['value']:g}%")
    if isinstance(unavailable, int) and unavailable:
        parts.append(
            f"недоступны {unavailable} отдельные функции; причина по текущим данным не подтверждена"
        )
    if parts:
        return ". ".join(parts) + "."
    return f"{name}: проверенные данные получены, но запрошенный факт не определён."


def validate_compact_answer(
    document: dict[str, Any], question: str, answer: str
) -> list[str]:
    """Reject factual distortions in a natural answer over one compact profile."""
    if not isinstance(document, dict) or not isinstance(question, str) or not isinstance(answer, str):
        return ["invalid_compact_answer"]
    folded = unicodedata.normalize("NFKC", answer).casefold()
    question_folded = unicodedata.normalize("NFKC", question).casefold()
    features = [
        item for item in document.get("relevant_features", [])
        if isinstance(item, dict)
    ]
    reasons: list[str] = []
    forbidden_causes = (
        "wi-fi", "wifi", "вай-ф", "сброс связи", "завис", "сервер",
        "интеграц", "сетевой модуль",
    )
    source_text = json.dumps(document, ensure_ascii=False).casefold()
    if any(value in folded and value not in source_text for value in forbidden_causes):
        reasons.append("invented_unavailable_cause")

    status = next(
        (item for item in features if item.get("component") == "main_status"), None
    )
    status_state = status.get("state") if isinstance(status, dict) else None
    status_value = status_state.get("value") if isinstance(status_state, dict) else None
    asks_problem = re.search(r"проблем|недоступ|почему|ошиб", question_folded) is not None
    asks_component = re.search(
        r"батар|заряд|фильтр|щ[её]тк|швабр|ополаскив|соль", question_folded
    ) is not None
    if str(status_value).casefold() in {"charging", "docked"}:
        if any(value in folded for value in ("едет", "движется", "убирает", "полной мощности")):
            reasons.append("charging_state_distorted")
        if not asks_problem and not asks_component and not any(
            value in folded for value in ("заряж", "зарядк", "док", "баз")
        ):
            reasons.append("charging_state_omitted")

    battery = next(
        (item for item in features if item.get("component") == "battery"), None
    )
    battery_state = battery.get("state") if isinstance(battery, dict) else None
    battery_value = battery_state.get("value") if isinstance(battery_state, dict) else None
    if isinstance(battery_value, (int, float)) and float(battery_value) == 100:
        if any(value in folded for value in ("разряж", "низкий заряд", "требуется зарядка")):
            reasons.append("full_battery_distorted")

    requested = (
        (r"батар|заряд", "battery"),
        (r"фильтр", "filter"),
        (r"основн.*щ[её]тк", "main_brush"),
        (r"боков.*щ[её]тк", "side_brush"),
        (r"швабр", "mop"),
        (r"ополаскив", "rinse_aid"),
        (r"соль", "salt"),
    )
    component = next(
        (value for pattern, value in requested if re.search(pattern, question_folded)),
        None,
    )
    if component is not None:
        feature = next(
            (item for item in features if item.get("component") == component), None
        )
        if isinstance(feature, dict) and feature.get("availability") == "available":
            state = feature.get("state")
            value = state.get("value") if isinstance(state, dict) else None
            if isinstance(value, (int, float)):
                normalized = f"{float(value):g}"
                if re.search(
                    rf"(?<!\d){re.escape(normalized)}(?:[.,]0+)?(?!\d)",
                    answer.replace(",", "."),
                ) is None:
                    reasons.append("requested_value_omitted")
            if component in {"filter", "main_brush", "side_brush", "mop"}:
                if "заряж" in folded or not any(
                    value in folded for value in ("ресурс", "износ", "остал", "%")
                ):
                    reasons.append("maintenance_semantics_distorted")
                if any(
                    value in folded
                    for value in ("фильтрация воздуха", "чистит пол", "приостановлена")
                ):
                    reasons.append("invented_device_process")

    unavailable = document.get("unavailable_feature_count")
    if (
        asks_problem
        and isinstance(unavailable, int)
        and unavailable > 0
        and document.get("physical_availability") == "available"
    ):
        if any(value in folded for value in ("робот сломан", "устройство сломано", "полностью недоступ")):
            reasons.append("feature_became_device_outage")
        # A learned conditional rule is not evidence that the condition caused
        # this live outage.  The cause stays unknown unless a future adapter
        # supplies an explicit, current and verified causal fact.
        unknown = not bool(document.get("confirmed_current_cause"))
        if unknown and any(value in folded for value in ("так как", "из-за", "потому что")):
            reasons.append("invented_unavailable_cause")
        if unknown and "почему" in question_folded and not any(
            value in folded for value in ("не подтвержд", "неизвест", "нет данных о причин")
        ):
            reasons.append("unknown_cause_not_disclosed")
    return sorted(set(reasons))


def learn_one(
    snapshot: dict[str, Any], inventory: dict[str, Any], physical_id: str,
    *, use_teacher: bool = True, workspace_root: Path | None = None,
) -> dict[str, Any]:
    details = ha_mcp.get_model_device_details(snapshot, inventory, physical_id)
    profile = build_profile(details, inventory)
    if use_teacher:
        profile["teacher_analysis"] = teacher_semantic_analysis(profile)
    else:
        profile["teacher_analysis"] = {"teacher_model": TEACHER_MODEL, "mode": "skipped_for_test", "accepted_suggestions": [], "suggestion_count": 0}
    snapshot_hash = _source_hash(snapshot, physical_id)
    candidates = generate_examples(profile, snapshot_hash)
    accepted, rejected = validate_corpus(candidates, profile)
    adversarial = deliberately_rejected_examples(profile, snapshot_hash)
    adversarial_accepted, adversarial_rejected = validate_corpus(adversarial, profile)
    if adversarial_accepted:
        raise LearningError("validator accepted an adversarial example")
    rejected.extend(adversarial_rejected)
    if len(accepted) < MIN_VALIDATED:
        reason_counts = Counter(
            reason for item in rejected for reason in item.get("rejection_reasons", [])
        )
        raise LearningError(
            "validated corpus is below the required minimum: "
            f"accepted={len(accepted)} reasons={dict(sorted(reason_counts.items()))}"
        )
    polarity = Counter(item["polarity"] for item in accepted)
    if polarity["positive"] < POSITIVE_COUNT or polarity["negative"] < NEGATIVE_COUNT:
        raise LearningError("positive or negative corpus coverage is incomplete")
    paths = persist_learning(profile, candidates + adversarial, accepted, rejected, root=workspace_root)
    return {
        "schema_version": 1,
        "status": "learned_and_validated",
        "physical_device_id": physical_id,
        "stable_id": profile["stable_id"],
        "display_name": profile["display_name"],
        "feature_count": len(profile["features"]),
        "capability_count": len(profile["capabilities"]),
        "generated_count": len(candidates) + len(adversarial),
        "validated_count": len(accepted),
        "rejected_count": len(rejected),
        "positive_count": polarity["positive"],
        "negative_count": polarity["negative"],
        "category_counts": dict(sorted(Counter(item["category"] for item in accepted).items())),
        "actions_performed": 0,
        "teacher_model": TEACHER_MODEL,
        "paths": paths,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--physical-device-id", required=True)
    parser.add_argument("--skip-teacher", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if re.fullmatch(r"[a-f0-9]{64}", args.physical_device_id) is None:
        print("DEVICE_LEARNING_INVALID_DEVICE", file=sys.stderr)
        return 2
    try:
        snapshot, exit_code = ha_read.execute_safely("snapshot")
        if exit_code != 0:
            raise LearningError("Home Assistant snapshot is unavailable")
        inventory = ha_entity_query.load_inventory()
        result = learn_one(
            snapshot, inventory, args.physical_device_id,
            use_teacher=not args.skip_teacher,
        )
    except (LearningError, ha_read.AdapterError, ha_entity_query.EntityQueryError, ValueError, model_workspace.WorkspaceError, model_ha_proof.ProofError, model_runtime_policy.ModelRuntimePolicyError, ollama_endpoint.EndpointConfigError) as error:
        print(
            f"DEVICE_LEARNING_FAILED:{type(error).__name__}:{error}",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
