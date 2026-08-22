#!/usr/bin/env python3
"""Execute one reviewed automation recovery with cooldown and readback."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import ssl
import sys
import time
from pathlib import Path
from typing import Any, Callable


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_control as ha_control  # noqa: E402
import home_assistant_inventory as ha_inventory  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402
import home_assistant_recovery as ha_recovery  # noqa: E402
import incident_monitor  # noqa: E402
import recovery_planner  # noqa: E402


YANDEX_HOST = "iot.quasar.yandex.ru"
YANDEX_PORT = 443
CLOUD_PROBE_TIMEOUT_SECONDS = 5
VERIFY_OFFSETS_SECONDS = (20, 40, 60)
BACKOFF_SECONDS = (30, 120, 600, 1_800)
RELOAD_COOLDOWN_SECONDS = 3_600
OBSERVE_COOLDOWN_SECONDS = 600
REVIEWED_AUTOMATIONS = {
    "automation.garderob_rele_po_datchiku_dvizheniia": {
        "motion": "binary_sensor.24g_presence_sensor_v3_dvizhenie",
        "helper": "input_boolean.garderob_rele_2_vkliucheno_datchikom",
        "target": "light.rele_2_garderob",
    },
}
EXACT_ACTIONS = {
    "light.turn_on": ("turn_on", "on"),
    "light.turn_off": ("turn_off", "off"),
    "switch.turn_on": ("turn_on", "on"),
    "switch.turn_off": ("turn_off", "off"),
}


class AutomationRecoveryError(RuntimeError):
    """Fixed, secret-free automation recovery failure."""


def probe_yandex_cloud(
    *,
    connection_factory: Callable[..., socket.socket] = socket.create_connection,
    context_factory: Callable[[], ssl.SSLContext] = ssl.create_default_context,
) -> bool:
    """Confirm DNS, TCP, and TLS to the one reviewed Yandex endpoint."""
    raw: socket.socket | None = None
    secured: socket.socket | None = None
    try:
        raw = connection_factory(
            (YANDEX_HOST, YANDEX_PORT), CLOUD_PROBE_TIMEOUT_SECONDS
        )
        secured = context_factory().wrap_socket(raw, server_hostname=YANDEX_HOST)
        return secured.version() is not None
    except (OSError, TimeoutError, ssl.SSLError):
        return False
    finally:
        for handle in (secured, raw):
            try:
                if handle is not None:
                    handle.close()
            except OSError:
                pass


def _read_states(
    config: ha_read.AdapterConfig,
    entity_ids: set[str],
    raw_state_reader: Callable[[ha_read.AdapterConfig, str], Any],
) -> dict[str, str]:
    for entity_id in entity_ids:
        ha_read._validate_entity_id(entity_id)
    document = raw_state_reader(config, "/api/states")
    if not isinstance(document, list) or len(document) > 8_192:
        raise AutomationRecoveryError("Home Assistant states are invalid")
    result: dict[str, str] = {}
    for item in document:
        if not isinstance(item, dict) or item.get("entity_id") not in entity_ids:
            continue
        state = item.get("state")
        if not isinstance(state, str) or state not in {
            "on", "off", "unknown", "unavailable",
        }:
            raise AutomationRecoveryError("Home Assistant state is invalid")
        entity_id = str(item["entity_id"])
        if entity_id in result:
            raise AutomationRecoveryError("Home Assistant state is ambiguous")
        result[entity_id] = state
    return result


def _entry_facts(
    inventory: dict[str, Any], target_entity_id: str | None
) -> tuple[str, str]:
    if target_entity_id is None:
        return "unknown", "unknown"
    entities = inventory.get("entities")
    entries = inventory.get("config_entries")
    if not isinstance(entities, list) or not isinstance(entries, list):
        raise AutomationRecoveryError("private inventory is invalid")
    matches = [
        item for item in entities
        if isinstance(item, dict) and item.get("entity_id") == target_entity_id
    ]
    if len(matches) != 1:
        return "unknown", "unknown"
    entry_ids = matches[0].get("config_entry_ids")
    if not isinstance(entry_ids, list) or len(entry_ids) != 1:
        return "unknown", "unknown"
    entry_matches = [
        item for item in entries
        if isinstance(item, dict)
        and item.get("entry_id") == entry_ids[0]
        and item.get("domain") == "yandex_station"
    ]
    if len(entry_matches) != 1:
        return "unknown", "unknown"
    return "known", (
        "healthy" if entry_matches[0].get("state") == "loaded" else "unhealthy"
    )


def runtime_facts(
    config: ha_read.AdapterConfig,
    inventory: dict[str, Any],
    incident: dict[str, object],
    *,
    raw_state_reader: Callable[[ha_read.AdapterConfig, str], Any] = ha_read.request_json,
    cloud_probe: Callable[[], bool] = probe_yandex_cloud,
) -> tuple[dict[str, str], dict[str, str]]:
    automation_id = str(incident["automation_entity_id"])
    target = incident.get("target_entity_id")
    target_id = str(target) if target is not None else None
    reviewed = REVIEWED_AUTOMATIONS.get(automation_id)
    requested = {target_id} if target_id is not None else set()
    if reviewed is not None:
        requested.update(reviewed.values())
    states = _read_states(config, requested, raw_state_reader) if requested else {}
    entry_known, integration = _entry_facts(inventory, target_id)
    if target_id is not None and states.get(target_id) in {"unknown", "unavailable"}:
        integration = "unhealthy"
    yandex_cloud = "unknown"
    if incident.get("cause_code") == "yandex_cloud_unreachable":
        yandex_cloud = "reachable" if cloud_probe() else "unreachable"
    facts = {
        "yandex_cloud": yandex_cloud,
        "integration": integration,
        "config_entry": entry_known,
        "intent": "unknown",
        "target_state": "unknown",
        "helper_state": "unknown",
    }
    action = str(incident.get("action_code"))
    action_contract = EXACT_ACTIONS.get(action)
    if target_id is not None and action_contract is not None:
        current_target = states.get(target_id)
        facts["target_state"] = (
            "matched" if current_target == action_contract[1]
            else "mismatched" if current_target in {"on", "off"}
            else "unknown"
        )
    if reviewed is not None and target_id == reviewed["target"]:
        motion = states.get(reviewed["motion"])
        helper = states.get(reviewed["helper"])
        target_state = states.get(reviewed["target"])
        if action.endswith("turn_on"):
            facts["intent"] = "current" if motion == "on" else "obsolete" if motion == "off" else "unknown"
        elif action.endswith("turn_off"):
            facts["intent"] = "current" if motion == "off" else "obsolete" if motion == "on" else "unknown"
        if motion == "off" and target_state == "off":
            facts["helper_state"] = (
                "desynchronized" if helper == "on"
                else "consistent" if helper == "off" else "unknown"
            )
        elif helper in {"on", "off"}:
            facts["helper_state"] = "consistent"
    return facts, states


def _verify_state(
    config: ha_read.AdapterConfig,
    entity_id: str,
    expected: str,
    *,
    raw_state_reader: Callable[[ha_read.AdapterConfig, str], Any],
    sleeper: Callable[[float], None],
) -> tuple[bool, int, str]:
    previous = 0
    observed = "unknown"
    for offset in VERIFY_OFFSETS_SECONDS:
        sleeper(offset - previous)
        previous = offset
        observed = _read_states(config, {entity_id}, raw_state_reader).get(
            entity_id, "unknown"
        )
        if observed == expected:
            return True, VERIFY_OFFSETS_SECONDS.index(offset) + 1, observed
    return False, len(VERIFY_OFFSETS_SECONDS), observed


def _verify_available(
    config: ha_read.AdapterConfig,
    entity_id: str,
    *,
    raw_state_reader: Callable[[ha_read.AdapterConfig, str], Any],
    sleeper: Callable[[float], None],
) -> tuple[bool, int, str]:
    previous = 0
    observed = "unknown"
    for offset in VERIFY_OFFSETS_SECONDS:
        sleeper(offset - previous)
        previous = offset
        observed = _read_states(config, {entity_id}, raw_state_reader).get(
            entity_id, "unknown"
        )
        if observed in {"on", "off"}:
            return True, VERIFY_OFFSETS_SECONDS.index(offset) + 1, observed
    return False, len(VERIFY_OFFSETS_SECONDS), observed


def _post_helper_off(config: ha_read.AdapterConfig, entity_id: str) -> None:
    reviewed_helpers = {item["helper"] for item in REVIEWED_AUTOMATIONS.values()}
    if entity_id not in reviewed_helpers:
        raise AutomationRecoveryError("helper is not reviewed")
    body = json.dumps(
        {"entity_id": entity_id}, ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")
    ha_recovery._post_service(
        config, "/api/services/input_boolean/turn_off", body
    )


def _next_backoff(store: incident_monitor.IncidentStore, incident_id: int) -> int:
    attempt = store.operational_attempt_count(incident_id)
    return BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]


def run_once(
    store: incident_monitor.IncidentStore,
    inventory: dict[str, Any],
    *,
    now: int | None = None,
    live: bool,
    config_loader: Callable[[], ha_read.AdapterConfig] = ha_read.load_config,
    raw_state_reader: Callable[[ha_read.AdapterConfig, str], Any] = ha_read.request_json,
    cloud_probe: Callable[[], bool] = probe_yandex_cloud,
    plan_fn: Callable[..., dict[str, object]] = recovery_planner.plan_one,
    action_caller: Callable[[ha_read.AdapterConfig, str, str], None] = ha_control.post_service,
    helper_caller: Callable[[ha_read.AdapterConfig, str], None] = _post_helper_off,
    reload_caller: Callable[[ha_read.AdapterConfig, str], None] = ha_recovery.post_tuya_local_reload,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    current = int(time.time()) if now is None else now
    incidents = store.operational_incident_candidates()
    if not incidents:
        return {"schema_version": 1, "mode": "live" if live else "dry_run", "incidents": 0, "service_calls": 0}
    incident = incidents[0]
    incident_id = int(incident["incident_id"])
    next_allowed = store.operational_next_allowed_epoch(incident_id)
    if next_allowed is not None and current < next_allowed:
        return {"schema_version": 1, "mode": "live" if live else "dry_run", "incidents": 1, "outcome": "cooldown", "next_allowed_epoch": next_allowed, "service_calls": 0}
    config = config_loader()
    runtime, states = runtime_facts(
        config, inventory, incident,
        raw_state_reader=raw_state_reader, cloud_probe=cloud_probe,
    )
    runtime["retry_budget"] = (
        "available"
        if store.operational_attempt_count(
            incident_id, "retry_original_intent_once"
        ) == 0
        else "exhausted"
    )
    facts = recovery_planner.build_facts(incident, runtime)
    candidates = recovery_planner.build_candidates(facts)
    if not live:
        return {
            "schema_version": 1, "mode": "dry_run", "incidents": 1,
            "candidate_ids": [item["id"] for item in candidates],
            "runtime_facts": runtime, "service_calls": 0,
        }
    decision = plan_fn(store, incident, runtime, now=current)
    candidate = str(decision["candidate_id"])
    decision_id = str(decision["decision_id"])
    target = incident.get("target_entity_id")
    target_id = str(target) if target is not None else None
    before = states.get(target_id, "unknown") if target_id else "unknown"
    status = "no_action"
    calls = 0
    checks = 0
    after = before
    evidence = "observation_recorded"
    next_epoch = current + OBSERVE_COOLDOWN_SECONDS
    resolve_code: str | None = None

    if candidate == "wait_yandex_backoff":
        wait_count = store.operational_candidate_total(incident_id, candidate)
        next_epoch = current + BACKOFF_SECONDS[
            min(wait_count, len(BACKOFF_SECONDS) - 1)
        ]
        evidence = "cloud_backoff"
    elif candidate in {"close_obsolete_intent", "close_verified_state"}:
        status = "verified"
        next_epoch = current
        evidence = candidate
        resolve_code = "target_state_confirmed"
    elif candidate == "retry_original_intent_once":
        contract = EXACT_ACTIONS.get(str(incident["action_code"]))
        if (
            target_id is None or contract is None
            or runtime["yandex_cloud"] != "reachable"
            or runtime["intent"] != "current"
            or runtime["target_state"] != "mismatched"
            or incident["safety_class"] not in {"light", "ordinary_relay"}
            or store.operational_attempt_count(incident_id, candidate) > 0
        ):
            status = "rejected"
            evidence = "retry_guard_rejected"
        else:
            calls = 1
            try:
                action_caller(config, target_id, contract[0])
                verified, checks, after = _verify_state(
                    config, target_id, contract[1],
                    raw_state_reader=raw_state_reader, sleeper=sleeper,
                )
                status = "verified" if verified else "failed"
                evidence = "target_state_confirmed" if verified else "target_state_not_confirmed"
                resolve_code = "target_state_confirmed" if verified else None
            except Exception as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                status = "delivery_unknown"
                evidence = "action_delivery_unknown"
            next_epoch = current if status == "verified" else current + _next_backoff(store, incident_id)
    elif candidate == "repair_helper_state":
        reviewed = REVIEWED_AUTOMATIONS.get(str(incident["automation_entity_id"]))
        helper = reviewed.get("helper") if reviewed is not None else None
        if helper is None or runtime["helper_state"] != "desynchronized":
            status = "rejected"
            evidence = "helper_guard_rejected"
        else:
            before = states.get(helper, "unknown")
            calls = 1
            try:
                helper_caller(config, helper)
                verified, checks, after = _verify_state(
                    config, helper, "off",
                    raw_state_reader=raw_state_reader, sleeper=sleeper,
                )
                status = "verified" if verified else "failed"
                evidence = "helper_state_confirmed" if verified else "helper_state_not_confirmed"
                resolve_code = "target_state_confirmed" if verified else None
            except Exception as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                status = "delivery_unknown"
                evidence = "helper_delivery_unknown"
            next_epoch = current if status == "verified" else current + _next_backoff(store, incident_id)
    elif candidate == "reload_yandex_entry_once":
        last_reload = store.last_operational_candidate_epoch(candidate)
        if (
            target_id is None
            or runtime["yandex_cloud"] != "reachable"
            or runtime["integration"] != "unhealthy"
            or runtime["config_entry"] != "known"
            or last_reload is not None
            and current - last_reload < RELOAD_COOLDOWN_SECONDS
        ):
            status = "cooldown" if last_reload is not None else "rejected"
            evidence = "reload_guard_rejected"
            next_epoch = max(current, (last_reload or current) + RELOAD_COOLDOWN_SECONDS)
        else:
            calls = 1
            try:
                reload_caller(config, target_id)
                verified, checks, after = _verify_available(
                    config, target_id,
                    raw_state_reader=raw_state_reader, sleeper=sleeper,
                )
                status = "verified" if verified else "failed"
                evidence = "integration_readback_confirmed" if verified else "integration_readback_failed"
            except Exception as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                status = "delivery_unknown"
                evidence = "reload_delivery_unknown"
            next_epoch = current + RELOAD_COOLDOWN_SECONDS
    elif candidate != "observe_and_notify":
        status = "rejected"
        evidence = "candidate_not_executable"

    store.record_operational_attempt(
        operational_incident_id=incident_id,
        decision_id=decision_id,
        candidate_id=candidate,
        attempted_epoch=current,
        status=status,
        service_calls=calls,
        verification_checks=checks,
        before_state=before,
        after_state=after,
        next_allowed_epoch=next_epoch,
        evidence_code=evidence,
    )
    if candidate == "retry_original_intent_once" and status == "failed":
        store.escalate_operational_incident(
            incident_id,
            current,
            cause_code="command_not_confirmed",
            cause_confidence="confirmed",
            evidence_code="target_state_not_confirmed_after_three_checks",
        )
    if status == "verified" and resolve_code is not None:
        store.resolve_operational_incident(incident_id, current, resolve_code)
    return {
        "schema_version": 1, "mode": "live", "incidents": 1,
        "incident_id": incident_id, "candidate_id": candidate,
        "decision_source": decision["source"], "outcome": status,
        "service_calls": calls, "verification_checks": checks,
        "next_allowed_epoch": next_epoch,
    }


def main() -> int:
    live = os.environ.get("HOME_BUTLER_AUTOMATION_RECOVERY_MODE", "dry-run") == "live"
    state_dir = incident_monitor._state_dir()
    try:
        incident_monitor._validate_directory(state_dir)
        inventory = ha_recovery._load_inventory_document(
            state_dir / ha_inventory.INVENTORY_NAME
        )
        store = incident_monitor.IncidentStore(
            state_dir / incident_monitor.DATABASE_NAME
        )
        try:
            result = run_once(store, inventory, live=live)
        finally:
            store.close()
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 0
    except (
        AutomationRecoveryError,
        incident_monitor.MonitorError,
        recovery_planner.PlannerError,
        ha_read.AdapterError,
        ha_recovery.RecoveryError,
        OSError,
        sqlite3.Error,
    ):
        print("HOME_ASSISTANT_AUTOMATION_RECOVERY_FAILED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
