#!/usr/bin/env python3
"""Stage 69 owner acceptance against current Home Assistant facts.

Real devices are read only.  The two control-verification cases use local
in-memory adapters and can never call Home Assistant services.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pwd
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence
from unittest import mock


sys.dont_write_bytecode = True
PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))
sys.path.insert(0, str(PROJECT_DIR / "tests"))

import bounded_ha_agent  # noqa: E402
import capability_catalog  # noqa: E402
import device_learning  # noqa: E402
import home_assistant_control  # noqa: E402
import home_assistant_mcp  # noqa: E402
import home_assistant_read  # noqa: E402
import model_ha_proof  # noqa: E402
from evaluate_learning_stage68 import _load_inventory_for_qualification  # noqa: E402


ENTITY_ID_RE = re.compile(r"\b[a-z0-9_]{1,64}\.[a-z0-9_]{2,200}\b", re.I)
OPAQUE_ID_RE = re.compile(r"\b(?:cap_[a-f0-9]{24}|[a-f0-9]{64})\b", re.I)
PRIVATE_ADDRESS_RE = re.compile(
    r"\b(?:10|127|169\.254|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b"
)
FAILURE_PHRASES = (
    "назовите entity", "требуется токен", "не могу подключиться",
    "проверка home assistant не завершена",
)
PROFILE_ROOT = Path(
    "/home/homebutler/.local/share/home-butler/model-workspace/knowledge/devices"
)
MAX_PROFILE_BYTES = 1_048_576
ORIGINAL_LOAD_PROFILE = device_learning.load_profile


class EvaluationError(RuntimeError):
    """One secret-free Stage 69 acceptance failure."""


def _number_forms(value: str) -> set[str]:
    normalized = value.replace(",", ".")
    forms = {value, normalized, normalized.replace(".", ",")}
    try:
        numeric = float(normalized)
    except ValueError:
        return forms
    if numeric.is_integer():
        forms.add(str(int(numeric)))
    return forms


def _numbers(value: object) -> set[str]:
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return {
        form
        for number in re.findall(r"(?<!\d)\d+(?:[.,]\d+)?(?!\d)", raw)
        for form in _number_forms(number)
    }


def _snapshot_age_seconds(document: Mapping[str, Any]) -> float:
    observed = document.get("observed_at")
    if not isinstance(observed, str):
        raise EvaluationError("live snapshot has no observation time")
    try:
        parsed = dt.datetime.fromisoformat(observed.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvaluationError("live snapshot observation time is invalid") from error
    if parsed.tzinfo is None:
        raise EvaluationError("live snapshot observation time is not timezone-aware")
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds())


def _fresh_snapshot() -> tuple[dict[str, Any], float]:
    document, exit_code = home_assistant_read.execute_safely("snapshot")
    if exit_code != 0 or document.get("status") not in {"healthy", "stale_data"}:
        raise EvaluationError("current Home Assistant snapshot is unavailable")
    age = _snapshot_age_seconds(document)
    if age > 120:
        raise EvaluationError("current Home Assistant snapshot is older than two minutes")
    return document, age


def _runtime_profile(physical_id: str, *, root: Path | None = None) -> dict[str, Any]:
    """Read the service user's profile during a root-run qualification.

    Production still uses ``device_learning.load_profile`` normally.  This
    fallback only crosses the evaluator's uid boundary after strict metadata
    checks, so the repo qualification exercises the same semantic overlay.
    """
    try:
        return ORIGINAL_LOAD_PROFILE(physical_id, root=root)
    except device_learning.LearningError:
        if os.geteuid() != 0 or root is not None:
            raise
    if re.fullmatch(r"[a-f0-9]{64}", physical_id) is None:
        raise device_learning.LearningError("physical device identity is invalid")
    path = PROFILE_ROOT / f"{physical_id[:24]}.json"
    try:
        expected_uid = pwd.getpwnam("homebutler").pw_uid
        metadata = path.lstat()
    except (KeyError, OSError) as error:
        raise device_learning.LearningError("device profile is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or not 0 < metadata.st_size <= MAX_PROFILE_BYTES
    ):
        raise device_learning.LearningError("device profile is unsafe")
    descriptor = os.open(
        path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        raw = os.read(descriptor, MAX_PROFILE_BYTES + 1)
    finally:
        os.close(descriptor)
    try:
        profile = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise device_learning.LearningError("device profile is invalid") from error
    if (
        len(raw) > MAX_PROFILE_BYTES
        or not isinstance(profile, dict)
        or profile.get("schema_version") != 1
        or profile.get("physical_device_id") != physical_id
    ):
        raise device_learning.LearningError("device profile is invalid")
    return profile


def _profile_exists(physical_id: str) -> bool:
    try:
        _runtime_profile(physical_id)
    except device_learning.LearningError:
        return False
    return True


def _unique_device(
    inventory: dict[str, Any], query: str
) -> tuple[str, str]:
    found = home_assistant_mcp.find_model_devices(inventory, query=query, limit=3)
    devices = found.get("devices")
    if (
        found.get("matched_device_count") != 1
        or not isinstance(devices, list)
        or len(devices) != 1
        or not isinstance(devices[0], dict)
    ):
        raise EvaluationError(f"human query did not resolve uniquely: {query}")
    physical_id = devices[0].get("physical_device_id")
    display_name = devices[0].get("display_name")
    if not isinstance(physical_id, str) or not isinstance(display_name, str):
        raise EvaluationError("resolved physical device is invalid")
    return physical_id, display_name


def _answer_reasons(
    answer: str,
    details: Mapping[str, Any] | None = None,
    *,
    required_name: str | None = None,
) -> list[str]:
    reasons: list[str] = []
    folded = answer.casefold()
    if not answer.strip() or len(answer) > 900:
        reasons.append("answer_length_invalid")
    if ENTITY_ID_RE.search(answer) or OPAQUE_ID_RE.search(answer) or PRIVATE_ADDRESS_RE.search(answer):
        reasons.append("technical_identifier_exposed")
    if any(value in folded for value in FAILURE_PHRASES):
        reasons.append("live_grounded_path_failed")
    if required_name and required_name.casefold() not in folded:
        reasons.append("device_name_omitted")
    if details is not None and not _numbers(answer) <= _numbers(details):
        reasons.append("invented_number")
    return reasons


def _feature(
    details: Mapping[str, Any],
    *,
    components: set[str] = frozenset(),
    markers: Sequence[str] = (),
    numeric: bool = False,
) -> Mapping[str, Any] | None:
    for item in details.get("features", []) if isinstance(details.get("features"), list) else []:
        if not isinstance(item, Mapping):
            continue
        searchable = " ".join(
            str(item.get(key) or "")
            for key in ("human_name", "component", "semantic_role")
        ).casefold()
        state = item.get("state")
        value = state.get("value") if isinstance(state, Mapping) else None
        if components and str(item.get("component")) not in components:
            continue
        if markers and not any(marker in searchable for marker in markers):
            continue
        if numeric and not isinstance(value, (int, float)):
            continue
        return item
    return None


def _feature_value(item: Mapping[str, Any] | None) -> object:
    state = item.get("state") if isinstance(item, Mapping) else None
    return state.get("value") if isinstance(state, Mapping) else None


def _requires_value(answer: str, value: object, reason: str) -> list[str]:
    if isinstance(value, (int, float)):
        expected = _number_forms(f"{value:g}")
        return [] if expected & _numbers(answer) else [reason]
    if isinstance(value, str) and value.strip() and value.casefold() not in answer.casefold():
        return [reason]
    return []


def _status_reasons(answer: str, details: Mapping[str, Any]) -> list[str]:
    status = _feature(
        details,
        components={"main_status", "status", "main", "main_robot", "vacuum"},
    )
    value = str(_feature_value(status) or "").casefold()
    if not value:
        return ["live_status_feature_missing"]
    markers = {
        "charging": ("заряж", "док", "баз"),
        "docked": ("док", "баз"),
        "returning": ("возвращ", "док", "баз"),
        "cleaning": ("уборк", "убира"),
        "paused": ("приост", "пауз"),
        "idle": ("ожида", "бездейств"),
        "off": ("выключ",),
        "on": ("включ",),
    }.get(value, (value, "состояние"))
    return [] if any(marker in answer.casefold() for marker in markers) else ["live_status_omitted"]


class RealModelProbe:
    def __init__(self) -> None:
        self.model_calls = 0
        self.live_control_attempts = 0

    def ollama(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.model_calls += 1
        return model_ha_proof.call_ollama(*args, **kwargs)

    def forbid_control(self, *_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], int]:
        self.live_control_attempts += 1
        raise EvaluationError("read-only qualification attempted a live action")

    def ask(
        self,
        question: str,
        history: Sequence[Mapping[str, str]],
        inventory: dict[str, Any],
        intent: bounded_ha_agent.OwnerIntent,
        *,
        require_model: bool = True,
    ) -> tuple[str, float, dict[str, Any], float, int]:
        snapshot, snapshot_age = _fresh_snapshot()
        before = self.model_calls
        started = time.monotonic()
        answer = bounded_ha_agent.maybe_respond(
            question,
            {"transport": "stage69_owner_acceptance"},
            history,
            voice=False,
            runtime_profile="dialogue",
            intent_parser=lambda _q, _c, _h, selected=intent: selected,
            inventory_loader=lambda document=inventory: document,
            snapshot_reader=lambda _mode, document=snapshot: (document, 0),
            control_catalogue_reader=lambda _mode: ({"status": "forbidden"}, 1),
            control_executor=self.forbid_control,
            ollama_call=self.ollama,
        )
        latency = time.monotonic() - started
        if not isinstance(answer, str) or not answer.strip():
            raise EvaluationError("real local model returned no grounded answer")
        model_calls = self.model_calls - before
        if require_model and model_calls < 1:
            raise EvaluationError(
                f"acceptance answer did not call the real local model: {question}"
            )
        return answer.strip(), latency, snapshot, snapshot_age, model_calls


def _check_record(
    name: str,
    question: str,
    answer: str,
    latency: float,
    snapshot_age: float,
    model_calls: int,
    reasons: Sequence[str],
    **extra: Any,
) -> dict[str, Any]:
    unique_reasons = sorted(set(reasons))
    return {
        "name": name,
        "question": question,
        "answer": answer,
        "pass": not unique_reasons,
        "reasons": unique_reasons,
        "latency_seconds": round(latency, 3),
        "snapshot_age_seconds": round(snapshot_age, 3),
        "real_model_calls": model_calls,
        **extra,
    }


def _simulated_transient_switch() -> dict[str, Any]:
    values = iter(
        ["off", "on", "on", "on", "off", "off", "off", "off", "off", "off", "off", "off", "off"]
    )
    simulated_calls = 0

    def snapshot_reader(mode: str) -> tuple[dict[str, Any], int]:
        if mode != "snapshot":
            raise EvaluationError("simulated switch requested the wrong read mode")
        return ({
            "status": "healthy",
            "entities": [{
                "entity_id": "switch.simulated_power",
                "state_kind": "enum",
                "state_value": next(values),
            }],
        }, 0)

    def service_caller(*_args: Any, **_kwargs: Any) -> None:
        nonlocal simulated_calls
        simulated_calls += 1

    with mock.patch.object(home_assistant_control.ha_read, "load_config", return_value=object()):
        result, exit_code = home_assistant_control.execute(
            "switch.simulated_power",
            "turn_on",
            snapshot_reader=snapshot_reader,
            service_caller=service_caller,
            sleeper=lambda _seconds: None,
        )
    passed = (
        exit_code != 0
        and result.get("status") == "not_verified"
        and result.get("verification_strength") != "R3_stable_state"
        and simulated_calls == 1
    )
    return {
        "name": "transient_switch_is_not_verified",
        "pass": passed,
        "adapter_status": result.get("status"),
        "verification_strength": result.get("verification_strength"),
        "simulated_service_calls": simulated_calls,
        "live_service_calls": 0,
    }


def _simulated_stateless_button() -> dict[str, Any]:
    capability = capability_catalog.Capability(
        capability_id="cap_" + "a" * 24,
        physical_device_id="b" * 64,
        device_name="Имитация",
        area_name=None,
        feature_name="Кнопка",
        domain="button",
        action_id="press",
        available=True,
        risk_class="R2",
        owner_confirmation="explicit_request",
        parameter_schema={
            "type": "object", "properties": {}, "required": [],
            "additionalProperties": False,
        },
        verification_method="transport_acceptance_only",
        entity_id="button.simulated",
    )
    catalogue = capability_catalog.CapabilityCatalog([capability])
    result = catalogue.execute(
        capability.capability_id,
        {},
        explicit_owner_request=True,
        executor=lambda *_args: ({
            "status": "accepted",
            "verification_strength": "transport_only",
            "service_calls": 1,
            "delivery": "accepted",
        }, 0),
    )
    passed = result.get("adapter_status") == "accepted_unverified"
    return {
        "name": "stateless_button_is_not_success",
        "pass": passed,
        "adapter_status": result.get("adapter_status"),
        "simulated_service_calls": 1,
        "live_service_calls": 0,
    }


def _read_only_primary_power_check(inventory: dict[str, Any]) -> dict[str, Any]:
    physical_id, device_name = _unique_device(inventory, "посудомойка")
    document, exit_code = home_assistant_read.execute_safely("control-catalog")
    if exit_code != 0 or document.get("status") not in {"healthy", "stale_data"}:
        raise EvaluationError("read-only control catalogue is unavailable")
    catalogue = capability_catalog.CapabilityCatalog.from_documents(document, inventory)
    raw = [
        item for item in catalogue.model_view(physical_id).get("capabilities", [])
        if isinstance(item, dict)
    ]
    selected, selection_status, options = (
        bounded_ha_agent._filter_action_capabilities_for_owner(
            raw,
            bounded_ha_agent.OwnerIntent(
                "ha_action", "Посудомойка", "включить", None, False
            ),
        )
    )
    selected_names = [
        str(item.get("feature_name")) for item in selected
        if isinstance(item.get("feature_name"), str)
    ]
    passed = (
        selection_status in {"unique_primary", "clarification_required"}
        and (
            selected_names == ["Питание"]
            or (not selected and bool(options))
        )
    )
    return {
        "name": "dishwasher_primary_power_selection",
        "device_name": device_name,
        "pass": passed,
        "selection_status": selection_status,
        "selected_features": selected_names,
        "clarification_options": options,
        "live_service_calls": 0,
    }


def main() -> int:
    inventory = _load_inventory_for_qualification()
    if not isinstance(inventory.get("physical_devices"), list):
        raise EvaluationError("live physical-device inventory is unavailable")
    probe = RealModelProbe()
    checks: list[dict[str, Any]] = []
    device_learning.load_profile = _runtime_profile
    try:
        # 1. Current status, room and battery for Андрей.
        question = "Что сейчас делает Андрей, где он и сколько у него заряда?"
        intent = bounded_ha_agent.resolve_obvious_read_intent(question, [], inventory)
        if intent is None or intent.device_query != "Андрей":
            raise EvaluationError("Андрей did not resolve from ordinary language")
        andrew_id, andrew_name = _unique_device(inventory, intent.device_query)
        answer, latency, snapshot, age, calls = probe.ask(question, [], inventory, intent)
        details = home_assistant_mcp.get_model_device_details(snapshot, inventory, andrew_id)
        battery = _feature(details, components={"battery"}, markers=("battery", "батар", "заряд"), numeric=True)
        reasons = _answer_reasons(answer, details, required_name=andrew_name)
        reasons.extend(_requires_value(answer, _feature_value(battery), "live_battery_omitted"))
        reasons.extend(_status_reasons(answer, details))
        areas = [value for value in details.get("areas", []) if isinstance(value, str)]
        if not areas or not any(value.casefold() in answer.casefold() for value in areas):
            reasons.append("live_area_omitted")
        checks.append(_check_record(
            "andrew_live_status_area_battery", question, answer, latency, age, calls, reasons
        ))
        history: list[dict[str, str]] = [
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ]

        # 2. Coreference follow-up must use a new live read, not training data.
        question = "а фильтр?"
        intent = bounded_ha_agent.resolve_obvious_read_intent(question, history, inventory)
        if intent is None or intent.device_query != "Андрей" or not intent.uses_coreference:
            raise EvaluationError("filter follow-up did not resolve to Андрей")
        answer, latency, snapshot, age, calls = probe.ask(question, history, inventory, intent)
        details = home_assistant_mcp.get_model_device_details(snapshot, inventory, andrew_id)
        profile = _runtime_profile(andrew_id)
        compact = device_learning.compact_profile(
            profile, details, question, maximum=8
        )
        filter_feature = next(
            (
                item for item in compact.get("relevant_features", [])
                if isinstance(item, Mapping) and item.get("component") == "filter"
            ),
            None,
        )
        reasons = _answer_reasons(answer, details)
        if filter_feature is None:
            reasons.append("live_filter_feature_missing")
        elif filter_feature.get("availability") == "available":
            reasons.extend(_requires_value(
                answer, _feature_value(filter_feature), "live_filter_value_omitted"
            ))
        elif "недоступ" not in answer.casefold():
            reasons.append("live_filter_availability_omitted")
        checks.append(_check_record(
            "andrew_live_filter_followup", question, answer, latency, age, calls, reasons
        ))
        history.extend([
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ])

        # 3. Roborock is deliberately absent from learned profiles.
        question = "Что с Roborock? Где он и сколько батареи?"
        intent = bounded_ha_agent.resolve_obvious_read_intent(question, [], inventory)
        if intent is None:
            raise EvaluationError("Roborock did not resolve from ordinary language")
        roborock_id, roborock_name = _unique_device(inventory, intent.device_query or "")
        if _profile_exists(roborock_id):
            raise EvaluationError("Roborock is not an unlearned-device acceptance case")
        answer, latency, snapshot, age, calls = probe.ask(question, [], inventory, intent)
        details = home_assistant_mcp.get_model_device_details(snapshot, inventory, roborock_id)
        battery = _feature(details, components={"battery"}, markers=("battery", "батар", "заряд"), numeric=True)
        reasons = _answer_reasons(answer, details, required_name=roborock_name)
        reasons.extend(_requires_value(answer, _feature_value(battery), "live_battery_omitted"))
        reasons.extend(_status_reasons(answer, details))
        areas = [value for value in details.get("areas", []) if isinstance(value, str)]
        if not areas or not any(value.casefold() in answer.casefold() for value in areas):
            reasons.append("live_area_omitted")
        checks.append(_check_record(
            "unlearned_roborock_live_read", question, answer, latency, age, calls,
            reasons, unlearned_profile=True,
        ))

        # 4. Generic robot is ambiguous and must never silently choose one.
        question = "Что с роботом?"
        ambiguous = home_assistant_mcp.find_model_devices(inventory, query="робот", limit=4)
        ambiguous_devices = [
            item for item in ambiguous.get("devices", []) if isinstance(item, dict)
        ]
        if ambiguous.get("matched_device_count") < 2:
            raise EvaluationError("the live inventory no longer has two ambiguous robots")
        intent = bounded_ha_agent.OwnerIntent("ha_read", "робот", None, None, False)
        answer, latency, _snapshot, age, calls = probe.ask(
            question, [], inventory, intent, require_model=False
        )
        names = [
            str(item.get("display_name")) for item in ambiguous_devices
            if isinstance(item.get("display_name"), str)
        ]
        reasons = _answer_reasons(answer)
        if not any(marker in answer.casefold() for marker in ("несколько", "уточн")):
            reasons.append("ambiguity_not_explained")
        if len(names) >= 2 and not all(name.casefold() in answer.casefold() for name in names[:2]):
            reasons.append("ambiguity_options_omitted")
        if any(marker in answer.casefold() for marker in ("готово", "включил", "выключил", "запустил")):
            reasons.append("ambiguous_target_false_success")
        checks.append(_check_record(
            "two_robots_require_clarification", question, answer, latency, age, calls, reasons
        ))

        # 5. Russian human word resolves the English HA display name.
        question = "Что с посудомойкой?"
        intent = bounded_ha_agent.resolve_obvious_read_intent(question, [], inventory)
        if intent is None:
            raise EvaluationError("посудомойка did not resolve from ordinary language")
        dishwasher_id, dishwasher_name = _unique_device(inventory, "посудомойка")
        answer, latency, snapshot, age, calls = probe.ask(question, [], inventory, intent)
        details = home_assistant_mcp.get_model_device_details(snapshot, inventory, dishwasher_id)
        reasons = _answer_reasons(answer, details)
        if not any(
            marker in answer.casefold() for marker in ("посудомой", "dishwasher")
        ):
            reasons.append("dishwasher_identity_omitted")
        checks.append(_check_record(
            "russian_dishwasher_live_read", question, answer, latency, age, calls, reasons
        ))

        # 9. Causal evidence remains unknown even after a grounded device read.
        question = "Это Wi-Fi?"
        intent = bounded_ha_agent.resolve_obvious_read_intent(question, history, inventory)
        if intent is None or intent.device_query != "Андрей":
            raise EvaluationError("causal follow-up did not resolve to Андрей")
        answer, latency, snapshot, age, calls = probe.ask(question, history, inventory, intent)
        details = home_assistant_mcp.get_model_device_details(snapshot, inventory, andrew_id)
        reasons = _answer_reasons(answer, details, required_name=andrew_name)
        if "причина по текущим данным не подтверждена" not in answer.casefold():
            reasons.append("unknown_cause_was_not_preserved")
        if re.search(r"\b(?:да|нет),?\s+(?:это|не это)", answer.casefold()):
            reasons.append("unsupported_causal_yes_or_no")
        checks.append(_check_record(
            "wifi_cause_requires_evidence", question, answer, latency, age, calls, reasons
        ))

        # Roborock plus these five current devices prove the unlearned live path.
        unlearned_cases = (
            ("обхаркиватель", "Что с обхаркивателем?", False),
            ("зеркало", "Что с зеркалом?", False),
            ("свет кабинет", "Что со светом в кабинете?", True),
            ("реле туалет", "Что с реле в туалете?", True),
            ("реле коридор", "Что с реле в коридоре?", True),
        )
        unlearned_device_names = {roborock_name}
        room_type_passes = 0
        for resolver_query, natural_question, room_type in unlearned_cases:
            physical_id, device_name = _unique_device(inventory, resolver_query)
            if _profile_exists(physical_id):
                raise EvaluationError(
                    f"device unexpectedly has a learned profile: {device_name}"
                )
            intent = bounded_ha_agent.resolve_obvious_read_intent(
                natural_question, [], inventory
            )
            if intent is None:
                raise EvaluationError(
                    f"ordinary read question did not resolve: {natural_question}"
                )
            answer, latency, snapshot, age, calls = probe.ask(
                natural_question, [], inventory, intent
            )
            details = home_assistant_mcp.get_model_device_details(
                snapshot, inventory, physical_id
            )
            reasons = _answer_reasons(answer, details, required_name=device_name)
            check = _check_record(
                "unlearned_room_type_read" if room_type else "unlearned_device_read",
                natural_question,
                answer,
                latency,
                age,
                calls,
                reasons,
                device_name=device_name,
                unlearned_profile=True,
                room_type_query=room_type,
            )
            checks.append(check)
            unlearned_device_names.add(device_name)
            if room_type and check["pass"]:
                room_type_passes += 1

        if len(unlearned_device_names) < 6:
            raise EvaluationError("fewer than six distinct unlearned devices were read")
        if room_type_passes < 3:
            raise EvaluationError("fewer than three live room/type reads passed")

        control_checks = [
            _read_only_primary_power_check(inventory),
            _simulated_transient_switch(),
            _simulated_stateless_button(),
        ]
    finally:
        device_learning.load_profile = ORIGINAL_LOAD_PROFILE

    all_pass = (
        all(item.get("pass") is True for item in checks)
        and all(item.get("pass") is True for item in control_checks)
        and probe.live_control_attempts == 0
        and probe.model_calls >= len(checks) - 1
    )
    result = {
        "schema_version": 1,
        "stage": 69,
        "status": "pass" if all_pass else "fail",
        "real_current_home_assistant": True,
        "real_local_model": True,
        "scripted_model": False,
        "read_only_live_ha": True,
        "live_service_calls": 0,
        "live_control_attempts": probe.live_control_attempts,
        "real_model_call_count": probe.model_calls,
        "unlearned_distinct_device_count": len(unlearned_device_names),
        "room_type_pass_count": room_type_passes,
        "check_count": len(checks),
        "passed_count": sum(item.get("pass") is True for item in checks),
        "maximum_latency_seconds": max(
            (float(item.get("latency_seconds", 0)) for item in checks), default=0.0
        ),
        "checks": checks,
        "control_checks": control_checks,
        "external_yandex_skill_verified": False,
        "learning_started": False,
    }
    output = PROJECT_DIR / "reports" / "stage69-evaluation" / "latest.json"
    output.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if all_pass else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvaluationError as error:
        print(json.dumps({
            "schema_version": 1,
            "stage": 69,
            "status": "error",
            "error": str(error),
            "live_service_calls": 0,
        }, ensure_ascii=False, separators=(",", ":")))
        raise SystemExit(2)
