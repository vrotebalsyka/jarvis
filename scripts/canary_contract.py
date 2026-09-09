#!/usr/bin/env python3
"""Host-owned canary authority. No transport, resolver, model or execution."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Mapping

import shadow_action_policy as policy


CONFIG_PATH = Path("/etc/home-butler/canary.json")
STATE_PATH = Path("/home/homebutler/.local/state/home-butler/canary")
PLAN_TTL = 12.0
MAX_METADATA_AGE = 2.0
VERIFICATION_PROFILE = "stable_boolean_state"
_KEY = secrets.token_bytes(32)
_ACTIONS = frozenset({"turn_on", "turn_off"})
_REF = re.compile(r"[a-f0-9]{64}")
_ID = re.compile(r"[a-zA-Z0-9_-]{1,80}")
_ENTITY = re.compile(r"(?:light|switch)\.[a-z0-9_]{1,200}")
_DENIED_PARENTS = frozenset({
    "vacuum", "fan", "humidifier", "media_player", "climate", "cover", "lock",
    "siren", "alarm_control_panel", "camera", "water_heater", "lawn_mower",
})


class CanaryError(ValueError):
    """Errors are closed reason codes; never include private input."""


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class CanaryRecord:
    canary_id: str
    target_ref: str
    entity_ref: str
    entity_id: str
    physical_identity: str
    domain: str
    registry_area_ref: str
    registry_area: str
    allowed_actions: tuple[str, ...]
    verification_profile: str
    rollback_actions: tuple[str, ...]
    allow_noop: bool

    @classmethod
    def parse(cls, row: Any) -> CanaryRecord:
        if not isinstance(row, dict) or set(row) != set(cls.__dataclass_fields__):
            raise CanaryError("invalid_allowlist_record")
        try:
            record = cls(**{**row, "allowed_actions": tuple(row["allowed_actions"]),
                            "rollback_actions": tuple(row["rollback_actions"])})
            if (
                not isinstance(record.canary_id, str) or not _ID.fullmatch(record.canary_id)
                or any(not isinstance(ref, str) or not _REF.fullmatch(ref) for ref in (
                    record.target_ref, record.entity_ref, record.physical_identity, record.registry_area_ref))
                or record.target_ref != record.entity_ref
                or not isinstance(record.entity_id, str) or not _ENTITY.fullmatch(record.entity_id)
                or record.domain not in {"light", "switch"}
                or not record.entity_id.startswith(record.domain + ".")
                or not isinstance(record.registry_area, str) or not 1 <= len(record.registry_area) <= 160
                or not record.allowed_actions or not set(record.allowed_actions) <= _ACTIONS
                or set(record.rollback_actions) != _ACTIONS
                or record.verification_profile != VERIFICATION_PROFILE
                or type(record.allow_noop) is not bool
            ):
                raise CanaryError("invalid_allowlist_record")
        except (TypeError, KeyError) as error:
            raise CanaryError("invalid_allowlist_record") from None
        return record


@dataclass(frozen=True, slots=True, repr=False)
class ControlConfig:
    control_enabled: bool = False
    canary_live_enabled: bool = False
    owner_approval: str = ""
    records: tuple[CanaryRecord, ...] = ()

    @property
    def enabled(self) -> bool:
        return self.control_enabled and self.canary_live_enabled and bool(self.owner_approval)

    @property
    def fingerprint(self) -> str:
        return digest(asdict(self))

    def record(self, target_ref: str) -> CanaryRecord:
        matches = [row for row in self.records if row.target_ref == target_ref]
        if len(matches) != 1:
            raise CanaryError("outside_allowlist")
        return matches[0]

    @classmethod
    def parse(cls, value: Any) -> ControlConfig:
        if not isinstance(value, dict) or set(value) != {
            "CONTROL_ENABLED", "CANARY_LIVE_ENABLED", "owner_approval", "canaries",
        }:
            raise CanaryError("invalid_control_config")
        if (type(value["CONTROL_ENABLED"]) is not bool
                or type(value["CANARY_LIVE_ENABLED"]) is not bool
                or not isinstance(value["owner_approval"], str)
                or (value["owner_approval"] and not _ID.fullmatch(value["owner_approval"]))
                or not isinstance(value["canaries"], list)):
            raise CanaryError("invalid_control_config")
        records = tuple(CanaryRecord.parse(row) for row in value["canaries"])
        if len(records) not in {0, 3, 4, 5}:
            raise CanaryError("canary_count")
        for name in ("canary_id", "target_ref", "entity_id", "physical_identity"):
            if len({getattr(row, name) for row in records}) != len(records):
                raise CanaryError("duplicate_canary_identity")
        result = cls(value["CONTROL_ENABLED"], value["CANARY_LIVE_ENABLED"], value["owner_approval"], records)
        if (result.control_enabled or result.canary_live_enabled) and (not records or not result.owner_approval):
            raise CanaryError("missing_owner_approval")
        return result


def load_config() -> ControlConfig:
    """A root-owned, non-writable configuration; absent means OFF, never ON."""
    try:
        descriptor = os.open(CONFIG_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return ControlConfig()
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
                or metadata.st_mode & 0o022 or metadata.st_nlink != 1 or metadata.st_size > 65_536):
            raise CanaryError("unsafe_control_config")
        raw = os.read(descriptor, 65_537)
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result:
                    raise CanaryError("invalid_control_config")
                result[key] = value
            return result
        return ControlConfig.parse(json.loads(raw, object_pairs_hook=pairs))
    except (UnicodeError, json.JSONDecodeError):
        raise CanaryError("invalid_control_config") from None
    finally:
        os.close(descriptor)


def binding_fingerprint(document: Mapping[str, Any], record: CanaryRecord, action: str) -> str:
    """Exact metadata assertion, not another resolver. No inferred room fallback."""
    entities = [row for row in document["entities"] if row["entity_ref"] == record.entity_ref]
    parents = [row for row in document["physical_nodes"] if row["target_ref"] == record.physical_identity]
    areas = [row for row in document["area_nodes"] if row["area_ref"] == record.registry_area_ref]
    if len(entities) != 1 or len(parents) != 1 or len(areas) != 1:
        raise CanaryError("identity_missing")
    entity, parent, area = entities[0], parents[0], areas[0]
    if (entity["entity_id"] != record.entity_id or entity["target_ref"] != record.physical_identity
            or entity["domain"] != record.domain or entity["area_ref"] != record.registry_area_ref
            or area["name"] != record.registry_area or entity.get("disabled") or entity.get("hidden")
            or entity.get("entity_category") is not None or not entity.get("registry_backed")
            or parent.get("strong_identity") != "device_registry_id_hash"
            or entity["entity_ref"] not in parent["entity_refs"]
            or action not in record.allowed_actions):
        raise CanaryError("identity_or_policy_changed")
    siblings = [row for row in document["entities"] if row["target_ref"] == record.physical_identity]
    domains = policy.ACTION_POLICY_REGISTRY.physical_safety_domains(siblings)
    profile = {"domains": {record.domain}, "safety_domains": domains,
               "names": [parent["display_name"], entity["display_name"]],
               "aliases": parent.get("aliases", []) + entity.get("aliases", [])}
    if domains & _DENIED_PARENTS or policy.ACTION_POLICY_REGISTRY.evaluate(action, profile).decision != "allow_shadow":
        raise CanaryError("hard_deny")
    return digest({"entity": entity, "parent": parent, "registry_area": area["name"],
                   "safety_domains": sorted(domains), "allowlist": asdict(record)})


@dataclass(frozen=True, slots=True, repr=False)
class SealedActionPlan:
    plan_id: str
    idempotency_key: str
    created_at: float
    expires_at: float
    intent_hash: str
    requested_area: tuple[str, ...]
    requested_type: tuple[str, ...]
    requested_feature: str
    resolved_target_ref: str
    resolved_area: str
    resolved_domain: str
    action: str
    target_fingerprint: str
    config_fingerprint: str
    verification_profile: str
    rollback_action: str
    risk: str
    owner_confirmation: str
    seal: str
    rollback_of: str | None = None


def _seal(plan: SealedActionPlan) -> str:
    return hmac.new(_KEY, digest(asdict(replace(plan, seal=""))).encode(), hashlib.sha256).hexdigest()


def seal_plan(shadow: policy.ActionPlan, intent: str, request_key: str,
              config: ControlConfig, document: Mapping[str, Any], *, now: float | None = None) -> SealedActionPlan:
    if not policy.verify_action_plan(shadow) or not request_key or len(request_key) > 512:
        raise CanaryError("invalid_shadow_plan")
    try:
        record = config.record(shadow.target_ref)
    except CanaryError:
        # Stage72 may seal an exact physical target, not an entity projection.
        # Bind it only when the entire physical device has ONE enabled output.
        # Never disambiguate multiple outputs merely by filtering the allowlist.
        outputs = [e for e in document["entities"] if e["target_ref"] == shadow.target_ref
                   and e["domain"] in {"light", "switch"} and not e.get("disabled") and not e.get("hidden")]
        if len(outputs) != 1:
            raise CanaryError("physical_output_ambiguous") from None
        record = config.record(outputs[0]["entity_ref"])
        if record.physical_identity != shadow.target_ref:
            raise CanaryError("physical_identity_mismatch")
    if shadow.domain != record.domain or shadow.scope.requested_feature != "power":
        raise CanaryError("scope_mismatch")
    # Existing host resolver checked human morphology/type/name. Here its resolved
    # effective area must equal the explicit registry binding, never inference.
    if tuple(shadow.areas) != (record.registry_area,):
        raise CanaryError("registry_area_required")
    area_node = next((a for a in document["area_nodes"] if a["area_ref"] == record.registry_area_ref), {})
    allowed_area_names = {str(name).casefold() for name in [record.registry_area, *area_node.get("aliases", [])]}
    if any(requested.casefold() not in allowed_area_names for requested in shadow.scope.requested_areas):
        raise CanaryError("requested_registry_area_mismatch")
    fingerprint = binding_fingerprint(document, record, shadow.action)
    created = time.time() if now is None else now
    plan = SealedActionPlan(
        secrets.token_hex(16), digest(request_key), created, created + PLAN_TTL,
        digest(intent), shadow.scope.requested_areas, shadow.scope.requested_types,
        shadow.scope.requested_feature, record.target_ref, record.registry_area, record.domain,
        shadow.action, fingerprint, config.fingerprint, record.verification_profile,
        "turn_off" if shadow.action == "turn_on" else "turn_on", "R2", "current_turn", "",
    )
    return replace(plan, seal=_seal(plan))


def valid_plan(plan: object, *, now: float | None = None) -> bool:
    if type(plan) is not SealedActionPlan:
        return False
    try:
        current = time.time() if now is None else now
        return (
            all(type(t) in {int, float} and math.isfinite(t) for t in (plan.created_at, plan.expires_at))
            and plan.created_at <= current < plan.expires_at <= plan.created_at + PLAN_TTL
            and plan.risk == "R2" and plan.owner_confirmation == "current_turn"
            and plan.action in _ACTIONS and plan.rollback_action in _ACTIONS
            and plan.resolved_domain in {"light", "switch"} and plan.requested_feature == "power"
            and plan.verification_profile == VERIFICATION_PROFILE
            and hmac.compare_digest(plan.seal, _seal(plan))
        )
    except (TypeError, ValueError, AttributeError):
        return False


def seal_rollback(original: SealedActionPlan, initial_state: str, config: ControlConfig,
                  document: Mapping[str, Any]) -> SealedActionPlan:
    """Host-only restoration to captured BEFORE state, through the same adapter.

    The original seal is required even after its execution TTL has elapsed.
    A rollback gets its own short TTL and a deterministic one-use request key.
    Emergency-stop flags are never bypassed: uncertain identity means no automatic rollback.
    """
    if (type(original) is not SealedActionPlan or not hmac.compare_digest(original.seal, _seal(original))
            or original.rollback_of is not None or initial_state not in {"on", "off"}):
        raise CanaryError("invalid_rollback_authority")
    record = config.record(original.resolved_target_ref)
    action = "turn_on" if initial_state == "on" else "turn_off"
    if action not in record.rollback_actions:
        raise CanaryError("rollback_not_approved")
    decision = policy.ACTION_POLICY_REGISTRY.evaluate(action, {"domains": {record.domain}})
    shadow = policy.seal_action_plan(target_ref=record.target_ref, target_label="Canary",
                                    areas=(record.registry_area,), domain=record.domain, action=action,
                                    scope=policy.ActionScope(requested_areas=(record.registry_area,)), decision=decision)
    plan = seal_plan(shadow, digest(["rollback", original.plan_id, initial_state]),
                     "rollback:" + original.plan_id, config, document)
    plan = replace(plan, rollback_of=original.plan_id, seal="")
    return replace(plan, seal=_seal(plan))
