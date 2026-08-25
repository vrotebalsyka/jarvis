#!/usr/bin/env python3
"""Let the local model select only from pre-authorized recovery candidates."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import incident_monitor  # noqa: E402
import model_ha_proof  # noqa: E402
import model_runtime_policy  # noqa: E402
import recovery_playbook_registry as playbook_registry  # noqa: E402
from ollama_endpoint import OllamaEndpoint, load_runtime_ollama_endpoint  # noqa: E402


MODEL = model_runtime_policy.get_profile("structured").model
RUNTIME_FACT_VALUES = {
    "yandex_cloud": {"unknown", "reachable", "unreachable"},
    "integration": {"unknown", "healthy", "unhealthy"},
    "intent": {"unknown", "current", "obsolete"},
    "target_state": {"unknown", "matched", "mismatched"},
    "config_entry": {"unknown", "known"},
    "helper_state": {"unknown", "consistent", "desynchronized"},
    "retry_budget": {"unknown", "available", "exhausted"},
    "integration_profile": {
        "unknown", "diagnose_only", "local_rebind_reload", "entry_reload",
        "idle_entry_reload", "cloud_backoff_entry_reload", "cloud_backoff",
        "permissioned_entry_reload",
    },
    "entry_match": {"unknown", "single", "ambiguous"},
    "device_activity": {"unknown", "idle", "active"},
}
CANDIDATE_IDS = frozenset(playbook_registry.BY_ID)


class PlannerError(RuntimeError):
    """Fixed, secret-free recovery planner failure."""


def _runtime_facts(values: dict[str, str] | None) -> dict[str, str]:
    source = values or {}
    if set(source) - set(RUNTIME_FACT_VALUES):
        raise PlannerError("unsupported recovery fact")
    result: dict[str, str] = {}
    for name, allowed in RUNTIME_FACT_VALUES.items():
        value = source.get(name, "unknown")
        if value not in allowed:
            raise PlannerError("invalid recovery fact")
        result[name] = value
    return result


def build_facts(
    incident: dict[str, object], runtime: dict[str, str] | None = None
) -> list[dict[str, str]]:
    required = {
        "incident_id", "status", "cause_code", "cause_confidence",
        "safety_class", "action_code", "target_entity_id",
    }
    if not required.issubset(incident):
        raise PlannerError("operational incident is incomplete")
    if incident["status"] not in {"confirmed", "escalated"}:
        raise PlannerError("operational incident is not actionable")
    cause = str(incident["cause_code"])
    confidence = str(incident["cause_confidence"])
    safety = str(incident["safety_class"])
    action = str(incident["action_code"])
    if (
        cause not in incident_monitor.CAUSE_CODES
        or confidence not in incident_monitor.CAUSE_CONFIDENCE
        or safety not in incident_monitor.SAFETY_CLASSES
        or re.fullmatch(r"[a-z0-9_.]{1,64}", action) is None
    ):
        raise PlannerError("operational incident is invalid")
    target_known = incident["target_entity_id"] is not None
    facts = [
        {"id": "incident:open", "value": str(incident["status"])},
        {"id": f"cause:{cause}", "value": cause},
        {"id": f"confidence:{confidence}", "value": confidence},
        {"id": f"safety:{safety}", "value": safety},
        {"id": f"action:{action}", "value": action},
        {"id": f"target:{'known' if target_known else 'unknown'}", "value": "known" if target_known else "unknown"},
    ]
    for name, value in _runtime_facts(runtime).items():
        facts.append({"id": f"{name}:{value}", "value": value})
    return facts


def build_candidates(facts: list[dict[str, str]]) -> list[dict[str, object]]:
    fact_ids = {item["id"] for item in facts}
    # The declarative registry is authoritative. The legacy predicates below
    # remain temporarily as a migration oracle and are proven equivalent by
    # tests before their final removal.
    return playbook_registry.matching_candidates(fact_ids)
def _parse_choice(document: dict[str, Any]) -> dict[str, object]:
    message = document.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or len(content) > 2_048:
        raise PlannerError("local model recovery choice is invalid")
    try:
        choice = json.loads(content)
    except json.JSONDecodeError as error:
        raise PlannerError("local model recovery choice is invalid") from error
    if not isinstance(choice, dict) or set(choice) != {"candidate_id", "fact_ids"}:
        raise PlannerError("local model recovery choice is invalid")
    candidate_id = choice.get("candidate_id")
    fact_ids = choice.get("fact_ids")
    if (
        not isinstance(candidate_id, str)
        or candidate_id not in CANDIDATE_IDS
        or not isinstance(fact_ids, list)
        or not fact_ids
        or len(fact_ids) > 32
        or len(set(fact_ids)) != len(fact_ids)
        or any(not isinstance(item, str) for item in fact_ids)
    ):
        raise PlannerError("local model recovery choice is invalid")
    return {"candidate_id": candidate_id, "fact_ids": fact_ids}


def choose(
    facts: list[dict[str, str]],
    candidates: list[dict[str, object]],
    *,
    endpoint_loader: Callable[[], OllamaEndpoint] = load_runtime_ollama_endpoint,
    ollama_call: Callable[..., dict[str, Any]] = model_ha_proof.call_ollama,
) -> dict[str, object]:
    if not candidates:
        raise PlannerError("recovery candidates are unavailable")
    all_candidates = {str(item["id"]): item for item in candidates}
    available_facts = {item["id"] for item in facts}
    fallback = all_candidates.get("observe_and_notify")
    if fallback is None:
        raise PlannerError("observe-only fallback is unavailable")
    specific_candidates = [
        item for item in candidates if item["id"] != "observe_and_notify"
    ]
    prompt_candidates = specific_candidates or [fallback]
    offered = {str(item["id"]): item for item in prompt_candidates}
    response_format = {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "string", "enum": sorted(offered)},
                "fact_ids": {
                    "type": "array", "minItems": 1, "maxItems": 32,
                    "items": {"type": "string", "enum": sorted(available_facts)},
                },
            },
            "required": ["candidate_id", "fact_ids"],
            "additionalProperties": False,
        }
    runtime_profile = model_runtime_policy.get_profile("structured")
    payload = model_runtime_policy.build_chat_payload(
        "structured",
        [
            {
                "role": "system",
                "content": (
                    "Select exactly one offered recovery candidate. Use only the "
                    "fact IDs supplied. Return JSON only. Never propose commands, "
                    "service names, parameters, shell, or a candidate not offered. "
                    "Candidates are ordered from most specific to fallback. Select "
                    "the first candidate whose required facts are present. Use "
                    "observe_and_notify only when no earlier candidate applies."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"facts": facts, "candidates": prompt_candidates},
                    ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                ),
            },
        ],
        response_format=response_format,
    )
    try:
        choice = _parse_choice(ollama_call(
            endpoint_loader(),
            "/api/chat",
            payload,
            timeout=runtime_profile.request_timeout_seconds,
        ))
        candidate = offered.get(str(choice["candidate_id"]))
        selected_facts = set(choice["fact_ids"])
        if (
            candidate is None
            or not selected_facts.issubset(available_facts)
        ):
            raise PlannerError("local model selected an unsupported recovery")
        return {
            "candidate_id": choice["candidate_id"],
            "fact_ids": sorted(set(candidate["required_fact_ids"])),
            "source": "model",
        }
    except Exception as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        return {
            "candidate_id": fallback["id"],
            "fact_ids": list(fallback["required_fact_ids"]),
            "source": "verified_fallback",
        }


def plan_one(
    store: incident_monitor.IncidentStore,
    incident: dict[str, object],
    runtime: dict[str, str] | None,
    *,
    now: int | None = None,
    chooser: Callable[[list[dict[str, str]], list[dict[str, object]]], dict[str, object]] = choose,
) -> dict[str, object]:
    decided_epoch = int(time.time()) if now is None else now
    facts = build_facts(incident, runtime)
    candidates = build_candidates(facts)
    decision = chooser(facts, candidates)
    candidate_id = str(decision["candidate_id"])
    fact_ids = [str(item) for item in decision["fact_ids"]]
    source = str(decision["source"])
    seed = json.dumps({
        "incident_id": incident["incident_id"],
        "candidate_id": candidate_id,
        "fact_ids": sorted(fact_ids),
        "decided_epoch": decided_epoch,
    }, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    decision_id = hashlib.sha256(seed.encode("ascii")).hexdigest()
    store.record_recovery_decision(
        decision_id=decision_id,
        operational_incident_id=int(incident["incident_id"]),
        selected_candidate_id=candidate_id,
        decision_source=source,
        fact_ids=fact_ids,
        decided_epoch=decided_epoch,
    )
    return {
        "decision_id": decision_id,
        "incident_id": int(incident["incident_id"]),
        "candidate_id": candidate_id,
        "fact_ids": sorted(fact_ids),
        "source": source,
    }
