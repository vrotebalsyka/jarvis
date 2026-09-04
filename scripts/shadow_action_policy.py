#!/usr/bin/env python3
"""Single fail-closed policy and sealed, non-executable shadow action plans."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import unicodedata
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence


ActionName = Literal["turn_on", "turn_off", "unsupported"]
PolicyDecisionName = Literal["allow_shadow", "hard_deny"]
_PLAN_SEAL_KEY = secrets.token_bytes(32)
_ALLOWED = frozenset({"turn_on", "turn_off"})
_HARD_DENY_DOMAINS = frozenset({
    "alarm_control_panel", "button", "climate", "cover", "fan", "humidifier",
    "lock", "media_player", "script", "vacuum",
})
_PARENT_HARD_DENY_DOMAINS = _HARD_DENY_DOMAINS - {"button", "script"}
_APPLIANCE_MARKERS = (
    "dishwasher", "washer", "dryer", "oven", "kettle", "coffee", "посудомо",
    "стираль", "сушиль", "духов", "чайник", "кофевар", "кофемаш",
)


@dataclass(frozen=True, slots=True)
class ActionScope:
    requested_areas: tuple[str, ...] = ()
    requested_types: tuple[str, ...] = ()
    requested_name: str | None = None
    requested_feature: str = "power"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision: PolicyDecisionName
    reason: str
    domain: str | None = None


@dataclass(frozen=True, slots=True)
class ActionPlan:
    """Immutable host-only plan. It deliberately contains no HA executable data."""

    schema_version: int
    mode: Literal["shadow"]
    target_ref: str
    target_label: str
    areas: tuple[str, ...]
    domain: Literal["light", "switch"]
    action: Literal["turn_on", "turn_off"]
    value: bool
    scope: ActionScope
    policy: Literal["allow_shadow"]
    service_calls: Literal[0]
    seal: str


class ActionPolicyRegistry:
    """The one action registry; every unspecified combination is denied."""

    __slots__ = ("_allowed",)

    def __init__(self) -> None:
        self._allowed = MappingProxyType({
            ("light", "turn_on"): True,
            ("light", "turn_off"): False,
            ("switch", "turn_on"): True,
            ("switch", "turn_off"): False,
        })

    @staticmethod
    def _text(profile: Mapping[str, Any]) -> str:
        values: list[str] = []
        for key in ("display_name", "names", "aliases", "entity_names", "entity_aliases"):
            raw = profile.get(key)
            if isinstance(raw, str):
                values.append(raw)
            elif isinstance(raw, (list, tuple, set, frozenset)):
                values.extend(value for value in raw if isinstance(value, str))
        return unicodedata.normalize("NFKC", " ".join(values)).casefold().replace("ё", "е")

    def evaluate(self, action: str | None, profile: Mapping[str, Any]) -> PolicyDecision:
        if action not in _ALLOWED:
            return PolicyDecision("hard_deny", "unsupported_action")
        domains = {
            value for value in profile.get("domains", ())
            if isinstance(value, str)
        }
        safety_domains = {
            value for value in profile.get("safety_domains", domains)
            if isinstance(value, str)
        }
        denied = sorted(
            (domains & _HARD_DENY_DOMAINS)
            | (safety_domains & _PARENT_HARD_DENY_DOMAINS)
        )
        if denied:
            return PolicyDecision("hard_deny", "dangerous_domain", denied[0])
        if any(marker in self._text(profile) for marker in _APPLIANCE_MARKERS):
            return PolicyDecision("hard_deny", "appliance")
        controllable = sorted(domains & {"light", "switch"})
        if len(controllable) != 1:
            return PolicyDecision("hard_deny", "unsupported_or_ambiguous_domain")
        domain = controllable[0]
        if (domain, action) not in self._allowed:
            return PolicyDecision("hard_deny", "policy_miss", domain)
        return PolicyDecision("allow_shadow", "shadow_only", domain)

    def value_for(self, domain: str, action: str) -> bool:
        try:
            return self._allowed[(domain, action)]
        except KeyError as error:
            raise ValueError("action is not allowed by shadow policy") from error


ACTION_POLICY_REGISTRY = ActionPolicyRegistry()


def _plan_payload(
    target_ref: str, target_label: str, areas: Sequence[str], domain: str,
    action: str, value: bool, scope: ActionScope,
) -> bytes:
    return json.dumps({
        "schema_version": 1, "mode": "shadow", "target_ref": target_ref,
        "target_label": target_label, "areas": list(areas), "domain": domain,
        "action": action, "value": value,
        "scope": {
            "requested_areas": list(scope.requested_areas),
            "requested_types": list(scope.requested_types),
            "requested_name": scope.requested_name,
            "requested_feature": scope.requested_feature,
        },
        "policy": "allow_shadow", "service_calls": 0,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def seal_action_plan(
    *, target_ref: str, target_label: str, areas: Sequence[str], domain: str,
    action: str, scope: ActionScope, decision: PolicyDecision,
) -> ActionPlan:
    if decision.decision != "allow_shadow" or decision.domain != domain:
        raise ValueError("only an allowed shadow decision can be sealed")
    if domain not in {"light", "switch"} or action not in _ALLOWED:
        raise ValueError("plan is outside the closed shadow vocabulary")
    value = ACTION_POLICY_REGISTRY.value_for(domain, action)
    payload = _plan_payload(target_ref, target_label, areas, domain, action, value, scope)
    seal = hmac.new(_PLAN_SEAL_KEY, payload, hashlib.sha256).hexdigest()
    return ActionPlan(
        1, "shadow", target_ref, target_label, tuple(areas), domain, action,
        value, scope, "allow_shadow", 0, seal,
    )


def verify_action_plan(plan: ActionPlan) -> bool:
    if not isinstance(plan, ActionPlan):
        return False
    if (
        plan.schema_version != 1 or plan.mode != "shadow"
        or plan.policy != "allow_shadow" or plan.service_calls != 0
        or plan.domain not in {"light", "switch"} or plan.action not in _ALLOWED
        or plan.value != ACTION_POLICY_REGISTRY.value_for(plan.domain, plan.action)
    ):
        return False
    payload = _plan_payload(
        plan.target_ref, plan.target_label, plan.areas, plan.domain,
        plan.action, plan.value, plan.scope,
    )
    expected = hmac.new(_PLAN_SEAL_KEY, payload, hashlib.sha256).hexdigest()
    return plan.service_calls == 0 and hmac.compare_digest(plan.seal, expected)
