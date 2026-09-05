#!/usr/bin/env python3
"""Closed read contract plus non-executable Stage 72 shadow action planning."""

from __future__ import annotations

import http.client
import json
import math
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_mcp as resolver  # noqa: E402
import home_assistant_read  # noqa: E402
import model_runtime_policy  # noqa: E402
import shadow_action_policy as action_policy  # noqa: E402
from ollama_endpoint import OllamaEndpoint, load_runtime_ollama_endpoint  # noqa: E402


MAX_RESPONSE_BYTES = 4 * 1_048_576
MAX_OWNER_ANSWER_CHARS = 32_000
FOCUS_TTL_SECONDS = 20 * 60
TECHNICAL_ID_RE = re.compile(
    r"\b(?:alarm_control_panel|binary_sensor|button|camera|climate|cover|fan|"
    r"humidifier|light|lock|media_player|number|select|sensor|switch|vacuum)"
    r"\.[a-z0-9_]+\b|\b[a-f0-9]{32,64}\b|/api/(?:services|states|config)\b|"
    r"\b(?:entity|device|service|capability)[_-]?id\s*[:=]",
    re.IGNORECASE,
)
SECRET_RE = re.compile(
    r"(?:\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b|"
    r"https?://|(?:^|\D)(?:10|127|169\.254|172\.(?:1[6-9]|2\d|3[01])|"
    r"192\.168)(?:\.\d{1,3}){2,3}(?:\D|$))",
    re.IGNORECASE,
)
CONTROL_WORD_RE = re.compile(
    r"\b(?:включи|включите|включай|включить|включать|выключи|выключите|выключай|выключить|выключать|"
    r"зажги|зажгите|зажечь|погаси|погасите|погасить|вруби|отключи|отключите|отключить|активируй|активировать|"
    r"деактивируй|деактивировать|переключи|переключить|нажми|нажать|запусти|запустить|останови|"
    r"остановить|верни|установи|установить|заблокируй|заблокировать|разблокируй|разблокировать|"
    r"открой|открыть|закрой|закрыть|поставь|поставить|выбери|выбрать|задай|задать|"
    r"turn\s+on|turn\s+off|toggle|press|"
    r"start|stop|set|lock|unlock)\b",
    re.IGNORECASE,
)
TURN_ON_RE = re.compile(
    r"\b(?:включи|включите|включай|включить|включать|зажги|зажгите|зажечь|вруби|активируй|активировать|turn\s+on)\b",
    re.IGNORECASE,
)
TURN_OFF_RE = re.compile(
    r"\b(?:выключи|выключите|выключай|выключить|выключать|погаси|погасите|погасить|"
    r"отключи|отключите|отключить|деактивируй|деактивировать|turn\s+off)\b",
    re.IGNORECASE,
)
UNSUPPORTED_ACTION_RE = re.compile(
    r"\b(?:переключи|переключить|нажми|нажать|запусти|запустить|останови|остановить|"
    r"установи|установить|поставь|поставить|задай|задать|выбери|выбрать|"
    r"заблокируй|заблокировать|разблокируй|разблокировать|открой|открыть|закрой|закрыть|"
    r"toggle|press|start|stop|set|lock|unlock)\b",
    re.IGNORECASE,
)
NEGATED_ACTION_RE = re.compile(
    r"\b(?:не\s+(?:надо\s+|нужно\s+|стоит\s+)?|не\s+хочу\s+)(?:включать|выключать|включай|"
    r"выключай|включи|выключи|зажигать|зажигай|гасить|гаси)\b",
    re.IGNORECASE,
)
ACTION_FALLBACK_CUE_RE = re.compile(
    r"\b(?:сделай|сделайте|пусть|хочу|хотелось|нужно|надо|можешь|можете|прошу|"
    r"темно|светло|светил|светилась|горел|горела|убери|уберите|оставь|оставьте)\b",
    re.IGNORECASE,
)
NATURAL_OFF_RE = re.compile(
    r"\b(?:не\s+(?:горит|горел[аои]?|светит|светил[аои]?|светится|светил(?:ось|ась))|"
    r"без\s+света|темно(?:ты|й|ю)?|убери(?:те)?\s+свет)\b",
    re.IGNORECASE,
)
NATURAL_ON_RE = re.compile(
    r"\b(?:светло|светл(?:ым|ой|ую)|горит|горел[аои]?|светит|светил[аои]?|"
    r"светится|светил(?:ось|ась)|светящ(?:имся|ейся)|работает)\b",
    re.IGNORECASE,
)
NATURAL_LIGHT_STATE_RE = re.compile(
    r"\b(?:свет|света|светло|светл(?:ым|ой|ую)|темно(?:ты|й|ю)?)\b",
    re.IGNORECASE,
)
DEICTIC_TARGET_RE = re.compile(r"\b(?:его|ее|её|их|там|это|этот|эту)\b", re.IGNORECASE)
STRONG_ACTION_TIERS = frozenset({
    "exact_alias", "exact_name", "exact_area_type", "entity_name_alias",
    "session_focus",
})
CAUSAL_RE = re.compile(r"\b(?:почему|отчего|из-за чего|причина|причины)\b", re.IGNORECASE)
CORRECTION_RE = re.compile(r"\b(?:нет|не то|имел(?:а)? в виду|поправка)\b", re.IGNORECASE)
GENERAL_RE = re.compile(
    r"^(?:привет|здравствуй|добрый (?:день|вечер|утро)|спасибо|как дела|кто ты|"
    r"что ты умеешь|пока|до свидания)[!.? ]*$",
    re.IGNORECASE,
)


class BoundedAgentError(RuntimeError):
    """A bounded, secret-free failure."""


@dataclass(slots=True)
class SessionFocus:
    """Ephemeral per-session focus; transports own it and never persist it."""

    last_target_refs: tuple[str, ...] = ()
    last_feature: str | None = None
    pending_target_refs: tuple[str, ...] = ()
    pending_feature: str | None = None
    expires_at: float = 0.0

    def expire(self, now: float) -> None:
        if self.expires_at and now >= self.expires_at:
            self.last_target_refs = ()
            self.last_feature = None
            self.pending_target_refs = ()
            self.pending_feature = None
            self.expires_at = 0.0

    def remember(self, targets: Sequence[str], feature: str, now: float) -> None:
        self.last_target_refs = tuple(targets)
        self.last_feature = feature
        self.pending_target_refs = ()
        self.pending_feature = None
        self.expires_at = now + FOCUS_TTL_SECONDS

    def clarify(self, targets: Sequence[str], feature: str, now: float) -> None:
        self.pending_target_refs = tuple(targets)
        self.pending_feature = feature
        self.expires_at = now + FOCUS_TTL_SECONDS


@dataclass(frozen=True, slots=True)
class IntentSelection:
    target_ref: str
    feature: str


@dataclass(frozen=True, slots=True)
class IntentFrame:
    kind: Literal["conversation", "read", "action", "clarification"]
    selections: tuple[IntentSelection, ...] = ()
    clarification_target_refs: tuple[str, ...] = ()
    causal_question: bool = False
    control_requested: bool = False
    selector_used: bool = False
    action: action_policy.ActionName | None = None
    value: bool | str | int | float | None = None
    scope: action_policy.ActionScope | None = None


@dataclass(frozen=True, slots=True)
class ReadReceipt:
    target_ref: str
    entity_ref: str | None
    target_kind: str
    target_label: str
    areas: tuple[str, ...]  # HA registry bindings only; inference is not a fact.
    feature: str
    value_kind: Literal[
        "number", "boolean", "on_off", "enum", "problem", "unknown",
        "unavailable", "redacted",
    ]
    value: str | int | float | bool | None
    unit: str | None
    device_class: str | None
    observed_at: str | None
    source_updated_at: str | None
    causal_evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TurnResult:
    frame: IntentFrame
    receipts: tuple[ReadReceipt, ...]
    answer: str
    model_generated_entity_ids: int = 0
    action_plan: action_policy.ActionPlan | None = None
    trace_json: str | None = None


def _reject_constant(_value: str) -> None:
    raise BoundedAgentError("model returned non-finite JSON")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BoundedAgentError("model returned duplicate JSON keys")
        result[key] = value
    return result


def parse_model_document(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise BoundedAgentError("model response size is invalid")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BoundedAgentError("model returned invalid JSON") from error
    if not isinstance(value, dict):
        raise BoundedAgentError("model response is not an object")
    return value


def call_ollama(
    endpoint: OllamaEndpoint, path: str, payload: dict[str, Any], *, timeout: float,
) -> dict[str, Any]:
    if path not in {"/api/chat", "/api/generate"}:
        raise BoundedAgentError("model path is not allowed")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    connection = http.client.HTTPConnection(endpoint.host, endpoint.port, timeout=timeout)
    try:
        connection.request("POST", path, body=body, headers={
            "Content-Type": "application/json", "Connection": "close",
        })
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if response.status != 200:
            raise BoundedAgentError("model request failed")
        return parse_model_document(raw)
    except (OSError, TimeoutError, http.client.HTTPException) as error:
        raise BoundedAgentError("model is unreachable") from error
    finally:
        connection.close()


def _safe_text(value: object, *, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    normalized = unicodedata.normalize("NFKC", " ".join(value.split()))
    if (
        not normalized or len(normalized) > 180
        or any(unicodedata.category(character).startswith("C") for character in normalized)
        or TECHNICAL_ID_RE.search(normalized) or SECRET_RE.search(normalized)
    ):
        return fallback
    return normalized


def validate_owner_answer(answer: str) -> str:
    normalized = " ".join(answer.strip().split())
    if not normalized or len(normalized) > MAX_OWNER_ANSWER_CHARS:
        raise BoundedAgentError("owner answer size is invalid")
    if TECHNICAL_ID_RE.search(normalized) or SECRET_RE.search(normalized):
        raise BoundedAgentError("owner answer exposed technical data")
    if any(ord(character) > 0xFFFF for character in normalized):
        raise BoundedAgentError("owner answer contains unsupported pictographs")
    return normalized


def _model_content(response: Mapping[str, Any]) -> str:
    message = response.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, str) or not 1 <= len(content) <= 4096:
        raise BoundedAgentError("model response is malformed")
    return content


def _structured_model_content(response: Mapping[str, Any]) -> str:
    content = response.get("response")
    if isinstance(content, str) and 1 <= len(content) <= 4096:
        return content
    return _model_content(response)


def _parse_model_action_fields(
    utterance: str, *,
    endpoint_loader: Callable[[], OllamaEndpoint],
    ollama_call: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Parse intent fields only; the model never sees or selects targets."""

    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "enum": ["on", "off", "none", "unsupported"]},
            "n": {"type": ["string", "null"], "maxLength": 160},
            "r": {"type": ["string", "null"], "maxLength": 80},
            "t": {
                "type": ["string", "null"],
                "enum": [*sorted(resolver.TYPE_CONCEPTS), None],
            },
        },
        "required": ["a", "n", "r", "t"],
        "additionalProperties": False,
    }
    profile = model_runtime_policy.get_profile("selector")
    prompt = (
        "Разбери просьбу изменить состояние дома в закрытый IntentFrame. Не выбирай устройство и не создавай IDs. "
        "Желание, чтобы стало светло, горело или светилось, означает a=on; желание темноты, погасить или чтобы не горело — a=off. "
        "Явный запрет выполнять действие означает a=none; другое действие означает a=unsupported. "
        "Ключ n: только явно названное устройство без комнаты и служебных слов или null. "
        "Ключ r: явно названная комната или null. Ключ t: класс устройства или null. "
        "Верни компактный JSON в одну строку только с ключами a,n,r,t. UTTERANCE=" + utterance
    )
    response = ollama_call(
        endpoint_loader(), "/api/generate",
        model_runtime_policy.build_generate_payload("selector", prompt, response_format=schema),
        timeout=profile.request_timeout_seconds,
    )
    content = _structured_model_content(response)
    if TECHNICAL_ID_RE.search(content) or SECRET_RE.search(content):
        raise BoundedAgentError("model crossed the intent boundary")
    try:
        parsed = json.loads(
            content, object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as error:
        raise BoundedAgentError("model intent is malformed") from error
    if not isinstance(parsed, dict) or set(parsed) != {"a", "n", "r", "t"}:
        raise BoundedAgentError("model intent is malformed")
    if parsed.get("a") not in {"on", "off", "none", "unsupported"}:
        raise BoundedAgentError("model intent is malformed")
    for key, limit in (("n", 160), ("r", 80)):
        value = parsed.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            raise BoundedAgentError("model intent is malformed")
        if isinstance(value, str) and not value.strip():
            parsed[key] = None
    if parsed.get("t") not in {*resolver.TYPE_CONCEPTS, None}:
        raise BoundedAgentError("model intent is malformed")
    action = {
        "on": "turn_on", "off": "turn_off", "none": None,
        "unsupported": "unsupported",
    }[parsed["a"]]
    return {
        "intent": "not_action" if action is None else "action",
        "action": action,
        "requested_name": parsed["n"], "requested_area": parsed["r"],
        "requested_type": parsed["t"], "requested_feature": "power",
    }


def _general_answer(
    question: str,
    history: Sequence[Mapping[str, str]],
    *,
    runtime_profile: str,
    endpoint_loader: Callable[[], OllamaEndpoint],
    ollama_call: Callable[..., dict[str, Any]],
) -> str:
    profile = model_runtime_policy.get_profile(runtime_profile)
    messages: list[dict[str, str]] = [{"role": "system", "content": (
        "Ты Home Butler. Ответь кратко. Не утверждай ничего о состоянии дома: "
        "домашние факты разрешены только из ReadReceipt. Не показывай идентификаторы или адреса."
    )}]
    messages.extend(
        {"role": str(item["role"]), "content": str(item["content"])}
        for item in history[-8:]
        if item.get("role") in {"user", "assistant"} and isinstance(item.get("content"), str)
    )
    messages.append({"role": "user", "content": question})
    return validate_owner_answer(_model_content(ollama_call(
        endpoint_loader(), "/api/chat",
        model_runtime_policy.build_chat_payload(runtime_profile, messages),
        timeout=profile.request_timeout_seconds,
    )))


def _extract_features(utterance: str) -> tuple[tuple[str, ...], bool]:
    normalized = resolver.normalize_text(utterance)
    found: list[str] = []
    for feature in (
        "main_brush", "side_brush", "child_lock", "battery", "filter",
        "humidity", "temperature", "power", "mode", "error", "consumables",
        "unknown", "status",
    ):
        if any(resolver.normalize_text(term) in normalized for term in resolver.FEATURE_TERMS[feature]):
            found.append(feature)
    if not found and ("щетк" in normalized or "щеток" in normalized):
        found.append("consumables")
    if "status" in found and len(found) > 1:
        status_terms = [resolver.normalize_text(term) for term in resolver.FEATURE_TERMS["status"]]
        coordinated = any(
            re.search(
                rf"(?:^| )(?:{re.escape(status_term)}) (?:и|а также) (?:{re.escape(other_term)})(?: |$)|"
                rf"(?:^| )(?:{re.escape(other_term)}) (?:и|а также) (?:{re.escape(status_term)})(?: |$)",
                normalized,
            )
            for feature in found if feature != "status"
            for status_term in status_terms
            for other_term in (resolver.normalize_text(term) for term in resolver.FEATURE_TERMS[feature])
        )
        if not coordinated:
            found = ["status"]
    if not found:
        inferred = resolver.resolve_feature(utterance)
        if inferred != "status":
            found.append(inferred)
    return (tuple(dict.fromkeys(found)) if found else ("status",), bool(found))


def _resolution_frame(
    utterance: str,
    feature: str,
    inventory: dict[str, Any],
    allowed: Sequence[str] | None,
    *,
    endpoint_loader: Callable[[], OllamaEndpoint],
    ollama_call: Callable[..., dict[str, Any]],
) -> tuple[tuple[str, ...], resolver.Resolution, bool]:
    resolution = resolver.resolve_targets(inventory, utterance, feature, allowed_target_refs=allowed)
    if len(resolution.target_refs) <= 1:
        return resolution.target_refs, resolution, False
    # Equal top-tier candidates are genuine ambiguity. A model cannot create
    # missing evidence, so the host always asks; the model selector remains a
    # bounded optional path for externally ranked candidate sets.
    return (), resolution, False


def _build_intent_frame(
    question: str,
    inventory: dict[str, Any],
    focus: SessionFocus,
    now: float,
    *,
    endpoint_loader: Callable[[], OllamaEndpoint],
    ollama_call: Callable[..., dict[str, Any]],
) -> IntentFrame:
    focus.expire(now)
    control = bool(CONTROL_WORD_RE.search(question))
    causal = bool(CAUSAL_RE.search(question))
    features, explicit_feature = _extract_features(question)
    if not explicit_feature and focus.pending_target_refs and focus.pending_feature:
        features = (focus.pending_feature,)
    elif not explicit_feature and focus.last_feature and CORRECTION_RE.search(question):
        features = (focus.last_feature,)
    parts = [part.strip() for part in re.split(r"\s+(?:и|а также)\s+", question, flags=re.IGNORECASE) if part.strip()]
    if 2 <= len(parts) <= 3:
        compound: list[IntentSelection] = []
        selector_used = False
        for part in parts:
            part_features, _explicit = _extract_features(part)
            selected, resolution, used = _resolution_frame(
                part, part_features[0], inventory, focus.pending_target_refs or None,
                endpoint_loader=endpoint_loader, ollama_call=ollama_call,
            )
            selector_used = selector_used or used
            if len(selected) != 1:
                compound = []
                break
            compound.extend(IntentSelection(selected[0], feature) for feature in part_features)
        if compound:
            return IntentFrame("read", tuple(compound), causal_question=causal, control_requested=control, selector_used=selector_used)

    allowed = focus.pending_target_refs or None
    selected, resolution, selector_used = _resolution_frame(
        question, features[0], inventory, allowed,
        endpoint_loader=endpoint_loader, ollama_call=ollama_call,
    )
    if not selected and not resolution.target_refs and focus.last_target_refs and (
        explicit_feature or len(resolver.normalize_text(question).split()) <= 5
    ):
        selected = focus.last_target_refs
    if len(selected) == 1:
        selections = tuple(IntentSelection(selected[0], feature) for feature in features)
        return IntentFrame("read", selections, causal_question=causal, control_requested=control, selector_used=selector_used)
    if len(selected) > 1:
        selections = tuple(IntentSelection(target, feature) for target in selected for feature in features)
        return IntentFrame("read", selections, causal_question=causal, control_requested=control, selector_used=selector_used)
    ambiguous = resolution.target_refs
    if ambiguous:
        return IntentFrame(
            "clarification", clarification_target_refs=ambiguous,
            causal_question=causal, control_requested=control, selector_used=selector_used,
        )
    if GENERAL_RE.fullmatch(question.strip()):
        return IntentFrame("conversation")
    return IntentFrame("clarification", causal_question=causal, control_requested=control)


def _receipt(
    fact: Mapping[str, Any] | None,
    target: Mapping[str, Any],
    target_ref: str,
    feature: str,
    snapshot_observed_at: str | None,
) -> ReadReceipt:
    label = _safe_text(target.get("display_name"), fallback="Устройство")
    areas = tuple(_safe_text(item, fallback="") for item in target.get("registry_areas", []) if _safe_text(item, fallback=""))
    if fact is None:
        return ReadReceipt(
            target_ref, None, str(target.get("kind") or "logical"), label, areas, feature,
            "unknown", None, None, None, snapshot_observed_at, None,
        )
    metadata = fact.get("metadata") if isinstance(fact.get("metadata"), Mapping) else {}
    state = fact.get("fresh_state") if isinstance(fact.get("fresh_state"), Mapping) else None
    unit = _safe_text(metadata.get("unit"), fallback="") or None
    device_class = _safe_text(metadata.get("device_class"), fallback="") or None
    if state is None:
        kind: str = "unavailable"; value: Any = None; updated = None
    else:
        raw_kind = state.get("state_kind")
        raw_value = state.get("state_value")
        updated = state.get("source_last_updated_at") if isinstance(state.get("source_last_updated_at"), str) else None
        if raw_kind in {"unknown", "unavailable", "redacted"}:
            kind, value = str(raw_kind), None
        elif raw_kind == "number" and isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool) and math.isfinite(float(raw_value)):
            kind, value = "number", raw_value
        elif isinstance(raw_value, bool):
            kind, value = "boolean", raw_value
        elif isinstance(raw_value, str) and raw_value.casefold() in {"true", "false"}:
            kind, value = "boolean", raw_value.casefold() == "true"
        elif isinstance(raw_value, str) and raw_value.casefold() in {"on", "off"}:
            boolean = raw_value.casefold() == "on"
            if device_class == "problem" or feature == "error":
                kind, value = "problem", boolean
            elif metadata.get("domain") == "binary_sensor":
                kind, value = "boolean", boolean
            else:
                kind, value = "on_off", raw_value.casefold()
        elif isinstance(raw_value, str) and raw_value.casefold() in {"problem", "ok"}:
            kind, value = "problem", raw_value.casefold() == "problem"
        elif isinstance(raw_value, str):
            kind, value = "enum", _safe_text(raw_value, fallback="значение скрыто")
            if value == "значение скрыто":
                kind, value = "redacted", None
        else:
            kind, value = "redacted", None
    return ReadReceipt(
        target_ref, str(metadata.get("entity_ref")) if metadata.get("entity_ref") else None,
        str(target.get("kind") or "logical"), label, areas, feature,
        kind, value, unit, device_class,
        fact.get("observed_at") if isinstance(fact.get("observed_at"), str) else snapshot_observed_at,
        updated,
    )


def make_receipts(frame: IntentFrame, inventory: dict[str, Any], snapshot: Mapping[str, Any]) -> tuple[ReadReceipt, ...]:
    if frame.kind != "read":
        return ()
    receipts: list[ReadReceipt] = []
    observed_at = snapshot.get("observed_at") if isinstance(snapshot.get("observed_at"), str) else None
    for selection in frame.selections:
        target = resolver.target_context(inventory, selection.target_ref)
        facts = resolver.fresh_facts(snapshot, inventory, selection.target_ref, selection.feature)
        if not facts:
            receipts.append(_receipt(None, target, selection.target_ref, selection.feature, observed_at))
        else:
            selected = [_receipt(fact, target, selection.target_ref, selection.feature, observed_at) for fact in facts]
            grounded = [item for item in selected if item.value_kind not in {"unknown", "unavailable", "redacted"}]
            if grounded:
                selected = grounded
            unique: dict[tuple[Any, ...], ReadReceipt] = {}
            for item in selected:
                key = (item.feature, item.value_kind, item.value, item.unit, item.device_class)
                unique.setdefault(key, item)
            receipts.extend(unique.values())
    return tuple(receipts)


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


def _format_number(value: int | float) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number).replace(".", ",")


def _render_receipt(receipt: ReadReceipt) -> str:
    label = FEATURE_LABELS.get(receipt.feature, receipt.feature.replace("_", " "))
    if receipt.value_kind == "number" and isinstance(receipt.value, (int, float)):
        unit = receipt.unit or ""
        separator = " " if unit and unit[0].isalnum() else ""
        value = _format_number(receipt.value) + separator + unit
    elif receipt.value_kind == "on_off":
        value = "включено" if receipt.value == "on" else "выключено"
    elif receipt.value_kind == "boolean":
        truth = receipt.value is True
        if receipt.device_class in {"motion", "occupancy", "presence"}:
            value = "обнаружено" if truth else "не обнаружено"
        elif receipt.device_class in {"door", "window", "opening"}:
            value = "открыто" if truth else "закрыто"
        else:
            value = "да" if truth else "нет"
    elif receipt.value_kind == "problem":
        value = "есть проблема" if receipt.value is True else "проблем нет"
    elif receipt.value_kind == "enum" and isinstance(receipt.value, str):
        value = ENUM_TRANSLATIONS.get(receipt.value.casefold(), receipt.value)
    elif receipt.value_kind == "unknown":
        value = "значение неизвестно"
    elif receipt.value_kind == "unavailable":
        value = "недоступно"
    else:
        value = "значение скрыто"
    return f"{label} — {value}"


def render_receipts(receipts: Sequence[ReadReceipt], frame: IntentFrame) -> str:
    if not receipts:
        raise BoundedAgentError("grounded renderer received no receipt")
    grouped: dict[tuple[str, str], list[ReadReceipt]] = {}
    for receipt in receipts:
        grouped.setdefault((receipt.target_ref, receipt.target_label), []).append(receipt)
    sections = [
        f"{label}: " + "; ".join(_render_receipt(item) for item in items)
        for (_target_ref, label), items in grouped.items()
    ]
    prefixes: list[str] = []
    if frame.control_requested:
        prefixes.append("Управление отключено; ничего не меняю.")
    if frame.causal_question and not any(receipt.causal_evidence for receipt in receipts):
        prefixes.append("Home Assistant не сообщает причину.")
    return validate_owner_answer(" ".join([*prefixes, *sections]))


def _clarification_answer(frame: IntentFrame, inventory: dict[str, Any]) -> str:
    if not frame.clarification_target_refs:
        suffix = " Уточните устройство или комнату."
        if frame.control_requested:
            suffix = " Управление отключено; ничего не меняю." + suffix
        return validate_owner_answer("Не удалось однозначно определить цель." + suffix)
    labels: list[str] = []
    for ref in frame.clarification_target_refs[:8]:
        target = resolver.target_context(inventory, ref)
        label = _safe_text(target.get("display_name"), fallback="Устройство")
        areas = target.get("registry_areas")
        area = _safe_text(areas[0], fallback="") if isinstance(areas, list) and areas else ""
        labels.append(f"{label} ({area})" if area else label)
    prefix = "Управление отключено; ничего не меняю. " if frame.control_requested else ""
    return validate_owner_answer(prefix + "Уточните цель: " + "; ".join(labels) + ".")


def _parse_shadow_action(question: str) -> tuple[action_policy.ActionName, bool | None] | None:
    """Deterministic fast path; free speech is handled by the structured parser."""

    on = list(TURN_ON_RE.finditer(question))
    off = list(TURN_OFF_RE.finditer(question))
    if NEGATED_ACTION_RE.search(question) or UNSUPPORTED_ACTION_RE.search(question):
        return "unsupported", None
    if not on and not off and ACTION_FALLBACK_CUE_RE.search(question):
        if NATURAL_OFF_RE.search(question):
            return "turn_off", False
        if NATURAL_ON_RE.search(question):
            return "turn_on", True
    if len(on) + len(off) == 0:
        return ("unsupported", None) if CONTROL_WORD_RE.search(question) else None
    if len(on) + len(off) != 1:
        return "unsupported", None
    return ("turn_on", True) if on else ("turn_off", False)


def _action_scope(inventory: Mapping[str, Any], question: str) -> action_policy.ActionScope:
    extracted = resolver.extract_action_scope(inventory, question)
    return action_policy.ActionScope(
        tuple(extracted["requested_areas"]), tuple(extracted["requested_types"]),
        extracted["requested_name"], str(extracted["requested_feature"]),
    )


def _model_action_scope(
    inventory: Mapping[str, Any], utterance: str, fields: Mapping[str, Any],
) -> action_policy.ActionScope:
    base = _action_scope(inventory, utterance)
    requested_name = fields.get("requested_name")
    requested_area = fields.get("requested_area")
    requested_type = fields.get("requested_type")
    if isinstance(requested_name, str):
        requested_name = resolver.normalize_text(requested_name)
        if not resolver.owner_text_supports(requested_name, utterance):
            requested_name = None
    if isinstance(requested_area, str):
        requested_area = resolver.normalize_text(requested_area)
        if not resolver.owner_text_supports(requested_area, utterance):
            requested_area = None
        if requested_area is not None:
            canonical = resolver.extract_action_scope(inventory, requested_area)["requested_areas"]
            areas = tuple(canonical) if canonical else (requested_area,)
        else:
            areas = base.requested_areas
    else:
        areas = base.requested_areas
    if isinstance(requested_type, str):
        if not resolver.owner_text_supports_type(requested_type, utterance):
            requested_type = None
        types = (requested_type,) if requested_type is not None else base.requested_types
    else:
        types = base.requested_types
    return action_policy.ActionScope(
        areas, types,
        requested_name if isinstance(requested_name, str) else base.requested_name,
        "power",
    )


def parse_action_intent(
    question: str, inventory: Mapping[str, Any], *,
    endpoint_loader: Callable[[], OllamaEndpoint],
    ollama_call: Callable[..., dict[str, Any]],
) -> IntentFrame | None:
    """Create the one closed action IntentFrame, with a bounded model fallback."""

    deterministic = _parse_shadow_action(question)
    if deterministic is not None:
        action, value = deterministic
        try:
            scope = _action_scope(inventory, question)
        except ValueError:
            scope = action_policy.ActionScope()
            action, value = "unsupported", None
        if (
            action in {"turn_on", "turn_off"}
            and not scope.requested_types
            and NATURAL_LIGHT_STATE_RE.search(question)
        ):
            scope = action_policy.ActionScope(
                scope.requested_areas, ("light",), scope.requested_name,
                scope.requested_feature,
            )
        return IntentFrame(
            "action", control_requested=True, action=action, value=value,
            scope=scope,
        )
    if not ACTION_FALLBACK_CUE_RE.search(question):
        return None
    fields = _parse_model_action_fields(
        question, endpoint_loader=endpoint_loader, ollama_call=ollama_call,
    )
    if fields["intent"] != "action" or fields["action"] is None:
        return IntentFrame(
            "action", control_requested=False, selector_used=True,
            action="unsupported", value=None, scope=action_policy.ActionScope(),
        )
    action = fields["action"]
    value = True if action == "turn_on" else False if action == "turn_off" else None
    scope = _model_action_scope(inventory, question, fields)
    return IntentFrame(
        "action", control_requested=True, selector_used=True,
        action=action, value=value, scope=scope,
    )


def _scope_query(scope: action_policy.ActionScope) -> str:
    parts = [
        scope.requested_name,
        *scope.requested_types,
        *scope.requested_areas,
    ]
    return " ".join(part for part in parts if part)


def _resolution_evidence(
    resolution: resolver.Resolution, scope: action_policy.ActionScope,
    *, weak_owner_verified: bool = False,
) -> str:
    if not resolution.candidates:
        return "none"
    if len(resolution.candidates) != 1:
        return "ambiguous"
    if resolution.tier in STRONG_ACTION_TIERS and not resolution.weak:
        return "strong"
    if weak_owner_verified or scope.requested_name or (scope.requested_areas and scope.requested_types):
        return "weak_verified"
    return "insufficient"


def _scope_mapping(scope: action_policy.ActionScope) -> dict[str, Any]:
    return {
        "areas": list(scope.requested_areas), "types": list(scope.requested_types),
        "name": scope.requested_name, "feature": scope.requested_feature,
    }


def _trace_candidate(
    profile: Mapping[str, Any], turn_ref: str, decision: action_policy.PolicyDecision,
) -> dict[str, Any]:
    public = resolver.public_candidate(profile, turn_ref)
    return {
        "ref": public["ref"], "label": public["label"], "areas": public["areas"],
        "kind": public["kind"],
        "policy": {"decision": decision.decision, "reason": decision.reason},
    }


def _write_shadow_trace(raw: str) -> None:
    print(raw, file=sys.stderr, flush=True)


def _seal_trace(
    *, frame: IntentFrame, candidates: Sequence[Mapping[str, Any]],
    selected: Mapping[str, Any] | None, decision: action_policy.PolicyDecision,
    plan: action_policy.ActionPlan | None,
    trace_sink: Callable[[str], None] | None,
    resolution_tier: str = "none", target_evidence: str = "none",
) -> str:
    trace_candidates = [
        _trace_candidate(
            candidate, f"r{index}",
            action_policy.ACTION_POLICY_REGISTRY.evaluate(frame.action, candidate),
        )
        for index, candidate in enumerate(candidates[:8], 1)
    ]
    selected_public = None
    if selected is not None:
        public = resolver.public_candidate(selected, "r1")
        selected_public = {
            "ref": public["ref"], "label": public["label"],
            "areas": public["areas"], "kind": public["kind"],
        }
    document = {
        "schema_version": 1, "mode": "shadow",
        "intent": {
            "kind": frame.kind, "action": frame.action, "value": frame.value,
            "scope": _scope_mapping(frame.scope or action_policy.ActionScope()),
            "parser": "model" if frame.selector_used else "deterministic",
        },
        "target_decision": {
            "resolution_tier": resolution_tier, "evidence": target_evidence,
        },
        "candidates": trace_candidates, "selected_target": selected_public,
        "policy": {"decision": decision.decision, "reason": decision.reason},
        "plan": None if plan is None else {
            "mode": plan.mode, "action": plan.action, "value": plan.value,
            "domain": plan.domain, "sealed": action_policy.verify_action_plan(plan),
        },
        "service_calls": 0, "ha_post": 0,
    }
    raw = json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if TECHNICAL_ID_RE.search(raw) or SECRET_RE.search(raw):
        raise BoundedAgentError("shadow trace exposed technical data")
    if trace_sink is not None:
        trace_sink(raw)
    return raw


def _shadow_action_result(
    question: str, inventory: dict[str, Any], frame: IntentFrame,
    focus: SessionFocus, now: float, *,
    trace_sink: Callable[[str], None] | None,
) -> TurnResult:
    scope = frame.scope or action_policy.ActionScope()
    if frame.action == "unsupported":
        decision = action_policy.PolicyDecision("hard_deny", "unsupported_or_untrusted_command")
        trace = _seal_trace(
            frame=frame, candidates=(), selected=None, decision=decision,
            plan=None, trace_sink=trace_sink,
        )
        return TurnResult(
            frame, (), "Действие запрещено политикой shadow; план не создан.",
            action_plan=None, trace_json=trace,
        )

    prepared_action_profiles = resolver.prepare_action_profiles(inventory)
    scope_query = _scope_query(scope)
    weak_owner_verified = False
    if not scope_query and DEICTIC_TARGET_RE.search(question) and focus.last_target_refs:
        candidates = resolver.profiles_for_target_refs(inventory, focus.last_target_refs)
        resolution = resolver.Resolution(
            "session_focus", tuple(str(item["target_ref"]) for item in candidates),
            candidates,
        )
    else:
        target_resolution = resolver.resolve_targets(
            inventory, question, "power", preserve_feature_words=True,
        )
        action_resolution = resolver.resolve_action_targets(
            inventory, question, preserve_feature_words=True,
            prepared_profiles=prepared_action_profiles,
        )
        target_hard_deny = False
        if len(target_resolution.candidates) == 1:
            target_candidate = target_resolution.candidates[0]
            action_parents = {
                candidate.get("parent_target_ref")
                for candidate in action_resolution.candidates
            }
            target_hard_deny = (
                action_policy.ACTION_POLICY_REGISTRY.evaluate(
                    frame.action, target_candidate,
                ).decision == "hard_deny"
                and (
                    target_resolution.tier in {"exact_alias", "exact_name", "entity_name_alias"}
                    or not action_resolution.candidates
                    or action_parents == {target_candidate.get("target_ref")}
                )
            )
        target_exact = (
            target_resolution.tier in {"exact_alias", "exact_name"}
            and bool(target_resolution.candidates)
        )
        raw_resolution = (
            target_resolution if target_exact or target_hard_deny else action_resolution
        )
        if not raw_resolution.candidates:
            raw_resolution = target_resolution
        if target_hard_deny:
            resolution = target_resolution
        elif (
            raw_resolution.tier in {"exact_alias", "exact_name", "entity_name_alias"}
            and raw_resolution.candidates
        ):
            resolution = raw_resolution
        elif raw_resolution.tier in STRONG_ACTION_TIERS and len(raw_resolution.candidates) == 1:
            resolution = raw_resolution
        elif (
            not frame.selector_used
            and raw_resolution.weak and len(raw_resolution.candidates) == 1
            and resolver.weak_action_evidence_matches(inventory, raw_resolution.candidates[0], question)
        ):
            resolution = raw_resolution
            weak_owner_verified = True
        else:
            action_resolution = resolver.resolve_action_targets(
                inventory, scope_query or question,
                prepared_profiles=prepared_action_profiles,
            )
            resolution = action_resolution if action_resolution.candidates else resolver.resolve_targets(
                inventory, scope_query or question, "power",
            )
    candidates = resolution.candidates
    evidence = _resolution_evidence(
        resolution, scope, weak_owner_verified=weak_owner_verified,
    )
    if not candidates:
        clarify_frame = IntentFrame(
            "clarification", control_requested=True, selector_used=frame.selector_used,
            action=frame.action, value=frame.value, scope=scope,
        )
        decision = action_policy.PolicyDecision("hard_deny", "target_not_resolved")
        trace = _seal_trace(
            frame=clarify_frame, candidates=(), selected=None, decision=decision,
            plan=None, trace_sink=trace_sink, resolution_tier=resolution.tier,
            target_evidence=evidence,
        )
        return TurnResult(
            clarify_frame, (), "Не удалось однозначно определить цель. Уточните устройство или комнату; shadow-план не создан.",
            action_plan=None, trace_json=trace,
        )
    if len(candidates) != 1:
        refs = tuple(str(candidate["target_ref"]) for candidate in candidates)
        clarify_frame = IntentFrame(
            "clarification", clarification_target_refs=refs, control_requested=True,
            selector_used=frame.selector_used, action=frame.action,
            value=frame.value, scope=scope,
        )
        decision = action_policy.PolicyDecision("hard_deny", "equal_candidates")
        trace = _seal_trace(
            frame=clarify_frame, candidates=candidates, selected=None, decision=decision,
            plan=None, trace_sink=trace_sink, resolution_tier=resolution.tier,
            target_evidence=evidence,
        )
        return TurnResult(
            clarify_frame, (), "Найдены равные кандидаты. Уточните цель; shadow-план не создан.",
            action_plan=None, trace_json=trace,
        )

    candidate = candidates[0]
    decision = action_policy.ACTION_POLICY_REGISTRY.evaluate(frame.action, candidate)
    if decision.decision != "allow_shadow" or decision.domain is None:
        trace = _seal_trace(
            frame=frame, candidates=candidates, selected=candidate,
            decision=decision, plan=None, trace_sink=trace_sink,
            resolution_tier=resolution.tier, target_evidence=evidence,
        )
        return TurnResult(
            frame, (), "Эта цель или команда жёстко запрещена политикой shadow; план не создан.",
            action_plan=None, trace_json=trace,
        )

    verification_scope = scope
    if resolution.tier in {"exact_alias", "exact_name", "entity_name_alias"}:
        verification_scope = action_policy.ActionScope(requested_feature=scope.requested_feature)
    matches, mismatch_reason = resolver.action_scope_matches(
        inventory, candidate, {
            "requested_areas": verification_scope.requested_areas,
            "requested_types": verification_scope.requested_types,
            "requested_name": verification_scope.requested_name,
            "requested_feature": verification_scope.requested_feature,
        },
        prepared_profiles=prepared_action_profiles,
    )
    if not matches:
        decision = action_policy.PolicyDecision("hard_deny", mismatch_reason)
        clarify_frame = IntentFrame(
            "clarification", clarification_target_refs=(str(candidate["target_ref"]),),
            control_requested=True, selector_used=frame.selector_used,
            action=frame.action, value=frame.value, scope=scope,
        )
        trace = _seal_trace(
            frame=clarify_frame, candidates=candidates, selected=None, decision=decision,
            plan=None, trace_sink=trace_sink, resolution_tier=resolution.tier,
            target_evidence=evidence,
        )
        return TurnResult(
            clarify_frame, (), "Цель не совпадает с указанной комнатой, типом или именем; shadow-план не создан.",
            action_plan=None, trace_json=trace,
        )

    if evidence == "insufficient":
        decision = action_policy.PolicyDecision("hard_deny", "insufficient_target_evidence")
        clarify_frame = IntentFrame(
            "clarification", clarification_target_refs=(str(candidate["target_ref"]),),
            control_requested=True, selector_used=frame.selector_used,
            action=frame.action, value=frame.value, scope=scope,
        )
        trace = _seal_trace(
            frame=clarify_frame, candidates=candidates, selected=None, decision=decision,
            plan=None, trace_sink=trace_sink, resolution_tier=resolution.tier,
            target_evidence=evidence,
        )
        return TurnResult(
            clarify_frame, (), "Недостаточно подтверждений цели; shadow-план не создан.",
            action_plan=None, trace_json=trace,
        )

    label = _safe_text(candidate.get("display_name"), fallback="Устройство")
    areas = tuple(
        value for item in candidate.get("areas", ())
        if (value := _safe_text(item, fallback=""))
    )
    plan = action_policy.seal_action_plan(
        target_ref=str(candidate["target_ref"]), target_label=label, areas=areas,
        domain=decision.domain, action=str(frame.action), scope=scope, decision=decision,
    )
    result_frame = IntentFrame(
        "action", (IntentSelection(str(candidate["target_ref"]), "power"),),
        control_requested=True, selector_used=frame.selector_used, action=frame.action,
        value=plan.value, scope=scope,
    )
    trace = _seal_trace(
        frame=result_frame, candidates=candidates, selected=candidate,
        decision=decision, plan=plan, trace_sink=trace_sink,
        resolution_tier=resolution.tier, target_evidence=evidence,
    )
    focus.remember((str(candidate["target_ref"]),), "power", now)
    verb = "включить" if frame.action == "turn_on" else "выключить"
    answer = validate_owner_answer(
        f"Shadow-план построен: {verb} {label}. Ничего не отправлено в Home Assistant."
    )
    return TurnResult(result_frame, (), answer, action_plan=plan, trace_json=trace)


def process_turn(
    question: str,
    context: Mapping[str, Any],
    history: Sequence[Mapping[str, str]],
    *,
    voice: bool = False,
    runtime_profile: str = "dialogue",
    inventory_loader: Callable[[], dict[str, Any]] = resolver.load_inventory,
    snapshot_reader: Callable[[str], tuple[dict[str, Any], int]] = home_assistant_read.execute_safely,
    endpoint_loader: Callable[[], OllamaEndpoint] = load_runtime_ollama_endpoint,
    ollama_call: Callable[..., dict[str, Any]] = call_ollama,
    clock: Callable[[], float] = time.monotonic,
    trace_sink: Callable[[str], None] | None = _write_shadow_trace,
) -> TurnResult:
    del voice
    inventory = inventory_loader()
    if not isinstance(inventory, dict):
        raise BoundedAgentError("HomeGraph is unavailable")
    focus = context.get("session_focus")
    if not isinstance(focus, SessionFocus):
        focus = SessionFocus()
    now = clock()
    focus.expire(now)
    action_frame = parse_action_intent(
        question, inventory, endpoint_loader=endpoint_loader,
        ollama_call=ollama_call,
    )
    if action_frame is not None:
        return _shadow_action_result(
            question, inventory, action_frame, focus, now,
            trace_sink=trace_sink,
        )
    frame = _build_intent_frame(
        question, inventory, focus, now,
        endpoint_loader=endpoint_loader, ollama_call=ollama_call,
    )
    if frame.kind == "conversation":
        answer = _general_answer(
            question, history, runtime_profile=runtime_profile,
            endpoint_loader=endpoint_loader, ollama_call=ollama_call,
        )
        return TurnResult(frame, (), answer)
    if frame.kind == "clarification":
        feature = _extract_features(question)[0][0]
        focus.clarify(frame.clarification_target_refs, feature, now)
        return TurnResult(frame, (), _clarification_answer(frame, inventory))
    snapshot, exit_code = snapshot_reader("snapshot")
    if exit_code != 0 or not isinstance(snapshot, dict) or snapshot.get("service_calls", 0) != 0:
        raise BoundedAgentError("fresh Home Assistant read failed")
    receipts = make_receipts(frame, inventory, snapshot)
    answer = render_receipts(receipts, frame)
    focus.remember(
        tuple(dict.fromkeys(selection.target_ref for selection in frame.selections)),
        frame.selections[-1].feature,
        now,
    )
    return TurnResult(frame, receipts, answer)


def respond(
    question: str,
    context: Mapping[str, Any],
    history: Sequence[Mapping[str, str]],
    **kwargs: Any,
) -> str:
    return process_turn(question, context, history, **kwargs).answer


def maybe_respond(
    question: str,
    context: Mapping[str, Any],
    history: Sequence[Mapping[str, str]],
    **kwargs: Any,
) -> str:
    return respond(question, context, history, **kwargs)
