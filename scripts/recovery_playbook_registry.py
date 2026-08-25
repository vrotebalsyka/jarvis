#!/usr/bin/env python3
"""Declarative, closed recovery playbooks layered over existing executors."""

from __future__ import annotations

import re
import sys
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping


sys.dont_write_bytecode = True

PLAYBOOK_ID_RE = re.compile(r"[a-z][a-z0-9_]{2,63}\Z")
FACT_ID_RE = re.compile(r"[a-z0-9_.:-]{2,128}\Z")
ADAPTER_ID_RE = re.compile(r"[a-z][a-z0-9_.]{2,95}\Z")
RISK_CLASSES = frozenset({"R0", "R1", "R2", "R3"})
QUALIFICATION_STAGES = frozenset({
    "offline", "dry_run", "owner_approved", "controlled_live", "staged", "enabled"
})
ALLOWED_ADAPTER_IDS = frozenset({
    "observe.record", "notify.owner", "scheduler.wait", "incident.resolve",
    "ha.control.retry_exact", "ha.config_entry.reload_exact",
    "ha.helper.repair_exact", "ha.integration.reload_exact",
    "ha.localtuya.reload_exact",
})


class PlaybookRegistryError(RuntimeError):
    """A fixed, secret-free declarative recovery failure."""


@dataclass(frozen=True, slots=True)
class FactCondition:
    """At least one listed canonical fact must be present."""

    any_of: tuple[str, ...]

    def match(self, facts: set[str]) -> str | None:
        return next((fact for fact in self.any_of if fact in facts), None)


@dataclass(frozen=True, slots=True)
class ConditionalRequirement:
    """If one canonical fact is present, another fact group is required."""

    if_fact: str
    require_any: tuple[str, ...]

    def match(self, facts: set[str]) -> tuple[str, ...] | None:
        if self.if_fact not in facts:
            return ()
        matched = next((fact for fact in self.require_any if fact in facts), None)
        return (self.if_fact, matched) if matched is not None else None


@dataclass(frozen=True, slots=True)
class RecoveryStep:
    order: int
    adapter_id: str
    verification: str
    rollback_adapter_id: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryPlaybook:
    playbook_id: str
    version: int
    scope: str
    supported_integrations: tuple[str, ...]
    supported_device_classes: tuple[str, ...]
    trigger_conditions: tuple[FactCondition, ...]
    conditional_requirements: tuple[ConditionalRequirement, ...]
    required_evidence: tuple[str, ...]
    preconditions: tuple[str, ...]
    risk_class: str
    automatic_permission: bool
    ordered_steps: tuple[RecoveryStep, ...]
    rollback: str
    maximum_attempts: int
    cooldown_seconds: int
    stop_conditions: tuple[str, ...]
    escalation_text: str

    def evidence(self, facts: set[str]) -> list[str] | None:
        selected: list[str] = []
        for condition in self.trigger_conditions:
            matched = condition.match(facts)
            if matched is None:
                return None
            selected.append(matched)
        for requirement in self.conditional_requirements:
            matched = requirement.match(facts)
            if matched is None:
                return None
            selected.extend(matched)
        return sorted(set(selected))


@dataclass(frozen=True, slots=True)
class QualificationRecord:
    stage: str
    offline_tests_passed: bool
    dry_run_passed: bool
    owner_approved: bool
    controlled_live_passed: bool
    post_enable_observed: bool
    rollback_enabled: bool


def _conditions(*groups: Iterable[str]) -> tuple[FactCondition, ...]:
    return tuple(FactCondition(tuple(group)) for group in groups)


def _step(
    order: int,
    adapter_id: str,
    verification: str,
    rollback: str | None = None,
) -> RecoveryStep:
    return RecoveryStep(order, adapter_id, verification, rollback)


PLAYBOOKS: tuple[RecoveryPlaybook, ...] = (
    RecoveryPlaybook(
        "observe_and_notify", 1, "incident", ("*",), ("*",),
        _conditions(("incident:open",)), (), ("confirmed incident",),
        ("incident remains open",), "R0", True,
        (_step(1, "observe.record", "observation persisted"),),
        "No state change to roll back.", 1, 600,
        ("incident resolved",), "Наблюдение сохранено; автоматическое действие не разрешено.",
    ),
    RecoveryPlaybook(
        "wait_yandex_backoff", 1, "integration:yandex_station", ("yandex_station",), ("relay",),
        _conditions(("cause:yandex_cloud_unreachable",),
                    ("yandex_cloud:unreachable", "yandex_cloud:unknown")),
        (), ("cloud probe",), ("confirmed cloud path issue",), "R0", True,
        (_step(1, "scheduler.wait", "fresh cloud probe after backoff"),),
        "No state change to roll back.", 4, 1800,
        ("cloud reachable", "attempt budget exhausted"),
        "Облачный путь Яндекса недоступен; безопасный backoff исчерпан.",
    ),
    RecoveryPlaybook(
        "retry_original_intent_once", 1, "automation", ("yandex_station",),
        ("light", "ordinary_relay"),
        _conditions(
            ("confidence:confirmed",),
            ("yandex_cloud:reachable",), ("intent:current",),
            ("target_state:mismatched",), ("target:known",),
            ("retry_budget:available",), ("safety:light", "safety:ordinary_relay"),
            ("action:light.turn_on", "action:light.turn_off",
             "action:switch.turn_on", "action:switch.turn_off"),
        ), (), ("fresh target state", "current intent", "cloud probe"),
        ("exact target and action",), "R1", True,
        (_step(1, "ha.control.retry_exact", "target state readback"),),
        "Do not repeat when delivery is unknown.", 1, 600,
        ("target matches", "delivery unknown", "attempt exhausted"),
        "Команда не подтверждена после одной разрешённой попытки.",
    ),
    RecoveryPlaybook(
        "reload_yandex_entry_once", 1, "integration:yandex_station",
        ("yandex_station",), ("*",),
        _conditions(("confidence:confirmed",), ("yandex_cloud:reachable",),
                    ("integration:unhealthy",),
                    ("config_entry:known",)), (),
        ("fresh entry state", "cloud probe"), ("one exact config entry",),
        "R1", True,
        (_step(1, "ha.config_entry.reload_exact", "entity availability readback"),),
        "Wait for cooldown; never reload a different entry.", 1, 3600,
        ("entry loaded", "attempt exhausted"),
        "Интеграция Яндекса не восстановилась после точной перезагрузки entry.",
    ),
    RecoveryPlaybook(
        "repair_helper_state", 1, "automation", ("*",),
        ("light", "ordinary_relay"),
        _conditions(("confidence:confirmed",),
                    ("helper_state:desynchronized",), ("target:known",),
                    ("safety:light", "safety:ordinary_relay")), (),
        ("fresh helper and target states",), ("reviewed helper mapping",),
        "R1", True,
        (_step(1, "ha.helper.repair_exact", "helper state readback"),),
        "Restore only the exact previous helper value when recorded.", 1, 600,
        ("helper consistent", "attempt exhausted"),
        "Состояние helper не удалось безопасно согласовать.",
    ),
    RecoveryPlaybook(
        "close_obsolete_intent", 1, "incident", ("*",), ("*",),
        _conditions(("intent:obsolete",), ("target_state:mismatched",),
                    ("helper_state:consistent",)), (),
        ("fresh intent and target state",), ("no action required",), "R0", True,
        (_step(1, "incident.resolve", "incident closure persisted"),),
        "Reopen on a new observation.", 1, 0, ("incident closed",),
        "Устаревший intent закрыт без изменения устройства.",
    ),
    RecoveryPlaybook(
        "close_verified_state", 1, "incident", ("*",), ("*",),
        _conditions(("intent:current",), ("target_state:matched",)), (),
        ("fresh target state",), ("target already matches",), "R0", True,
        (_step(1, "incident.resolve", "incident closure persisted"),),
        "Reopen on a new observation.", 1, 0, ("incident closed",),
        "Целевое состояние уже подтверждено.",
    ),
    RecoveryPlaybook(
        "reload_integration_entry_once", 1, "integration", ("*",), ("*",),
        _conditions(
            ("confidence:confirmed",), ("cause:integration_not_loaded",),
            ("entry_match:single",),
            ("retry_budget:available",),
            ("integration_profile:entry_reload", "integration_profile:idle_entry_reload",
             "integration_profile:cloud_backoff_entry_reload"),
        ), (
            ConditionalRequirement("integration_profile:idle_entry_reload", ("device_activity:idle",)),
            ConditionalRequirement("integration_profile:cloud_backoff_entry_reload", ("yandex_cloud:reachable",)),
        ), ("fresh config entry state", "alternate paths checked"),
        ("single exact entry", "profile allows reload"), "R1", True,
        (_step(1, "ha.integration.reload_exact", "entry loaded readback"),),
        "Do not try another entry; retain incident evidence.", 1, 3600,
        ("entry loaded", "attempt exhausted"),
        "Интеграция не загрузилась после одной точной попытки.",
    ),
    RecoveryPlaybook(
        "reload_local_integration_once", 1, "integration:localtuya",
        ("localtuya", "tuya_local"), ("*",),
        _conditions(("confidence:confirmed",),
                    ("cause:integration_not_loaded",), ("entry_match:single",),
                    ("retry_budget:available",),
                    ("integration_profile:local_rebind_reload",)), (),
        ("stable identity", "fresh integration state", "alternate paths checked"),
        ("single exact local integration",), "R1", True,
        (_step(1, "ha.localtuya.reload_exact", "members available readback"),),
        "Never guess an IP or edit HA storage; retain prior binding.", 1, 3600,
        ("members available", "attempt exhausted"),
        "Локальная интеграция не сошлась после разрешённой перезагрузки.",
    ),
)


def _validate_registry(playbooks: tuple[RecoveryPlaybook, ...]) -> None:
    identifiers: set[str] = set()
    for playbook in playbooks:
        if (
            PLAYBOOK_ID_RE.fullmatch(playbook.playbook_id) is None
            or playbook.playbook_id in identifiers
            or playbook.version < 1
            or playbook.risk_class not in RISK_CLASSES
            or not playbook.required_evidence or not playbook.preconditions
            or not playbook.ordered_steps or not playbook.stop_conditions
            or not playbook.escalation_text.strip()
            or playbook.maximum_attempts < 1
            or playbook.cooldown_seconds < 0
        ):
            raise PlaybookRegistryError("recovery playbook registry is invalid")
        identifiers.add(playbook.playbook_id)
        orders = [step.order for step in playbook.ordered_steps]
        if orders != list(range(1, len(orders) + 1)):
            raise PlaybookRegistryError("recovery step order is invalid")
        for step in playbook.ordered_steps:
            if (
                step.adapter_id not in ALLOWED_ADAPTER_IDS
                or not step.verification.strip()
                or step.rollback_adapter_id is not None
                and step.rollback_adapter_id not in ALLOWED_ADAPTER_IDS
            ):
                raise PlaybookRegistryError("recovery adapter is not allow-listed")
        fact_ids = [
            fact
            for condition in playbook.trigger_conditions
            for fact in condition.any_of
        ] + [
            fact
            for requirement in playbook.conditional_requirements
            for fact in (requirement.if_fact, *requirement.require_any)
        ]
        if any(FACT_ID_RE.fullmatch(fact) is None for fact in fact_ids):
            raise PlaybookRegistryError("recovery fact condition is invalid")
        if (
            playbook.risk_class == "R1"
            and "confidence:confirmed" not in fact_ids
        ):
            raise PlaybookRegistryError("R1 requires confirmed failure evidence")


_validate_registry(PLAYBOOKS)
BY_ID: Mapping[str, RecoveryPlaybook] = {
    playbook.playbook_id: playbook for playbook in PLAYBOOKS
}


def get(playbook_id: object) -> RecoveryPlaybook:
    if not isinstance(playbook_id, str) or playbook_id not in BY_ID:
        raise PlaybookRegistryError("recovery playbook is unavailable")
    return BY_ID[playbook_id]


def validate_fact_ids(fact_ids: Iterable[str]) -> set[str]:
    facts = set(fact_ids)
    if any(
        not isinstance(fact, str) or FACT_ID_RE.fullmatch(fact) is None
        for fact in facts
    ):
        raise PlaybookRegistryError("recovery facts are invalid")
    return facts


def matching_candidates(fact_ids: Iterable[str]) -> list[dict[str, object]]:
    facts = validate_fact_ids(fact_ids)
    result: list[dict[str, object]] = []
    for playbook in PLAYBOOKS:
        evidence = playbook.evidence(facts)
        if evidence is None:
            continue
        result.append({
            "id": playbook.playbook_id,
            "risk": {"R0": "none", "R1": "low", "R2": "owner", "R3": "sensitive"}[
                playbook.risk_class
            ],
            "required_fact_ids": evidence,
        })
    return result


def describe(playbook_id: object) -> dict[str, Any]:
    return asdict(get(playbook_id))


def authorize_live(
    playbook_id: object,
    qualification: QualificationRecord,
    *,
    explicit_owner_request: bool = False,
    separate_confirmation: bool = False,
) -> bool:
    playbook = get(playbook_id)
    if qualification.stage not in QUALIFICATION_STAGES:
        raise PlaybookRegistryError("qualification stage is invalid")
    if playbook.risk_class == "R0":
        return qualification.offline_tests_passed
    if playbook.risk_class == "R1":
        return bool(
            playbook.automatic_permission
            and qualification.stage == "enabled"
            and qualification.offline_tests_passed
            and qualification.dry_run_passed
            and qualification.owner_approved
            and qualification.controlled_live_passed
            and qualification.post_enable_observed
            and qualification.rollback_enabled
        )
    if playbook.risk_class == "R2":
        return explicit_owner_request and qualification.offline_tests_passed
    return (
        explicit_owner_request and separate_confirmation
        and qualification.offline_tests_passed and qualification.rollback_enabled
    )
