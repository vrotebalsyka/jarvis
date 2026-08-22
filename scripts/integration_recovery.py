#!/usr/bin/env python3
"""Execute one profile-authorized integration recovery with readback."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import automation_recovery  # noqa: E402
import home_assistant_inventory as ha_inventory  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402
import home_assistant_recovery as ha_recovery  # noqa: E402
import incident_monitor  # noqa: E402
import recovery_planner  # noqa: E402


VERIFY_OFFSETS_SECONDS = (20, 40, 60)
OBSERVE_COOLDOWN_SECONDS = 600
RELOAD_COOLDOWN_SECONDS = 3600
ENTRY_RELOAD_PATH = "/api/services/homeassistant/reload_config_entry"
ALLOWED_AUTOMATIC_MODES = {
    "local_rebind_reload",
    "entry_reload",
    "idle_entry_reload",
    "cloud_backoff_entry_reload",
}
ACTIVE_STATES = {
    "on", "active", "running", "washing", "drying", "heating",
    "cleaning", "starting", "paused",
}
IDLE_STATES = {
    "off", "idle", "standby", "finished", "complete", "completed", "ready",
}


class IntegrationRecoveryError(RuntimeError):
    """Secret-free universal integration recovery failure."""


def _entry_states(
    config: ha_read.AdapterConfig,
    *,
    connector: Callable[[ha_read.AdapterConfig], Any] = incident_monitor._connect,
) -> dict[str, str]:
    socket = connector(config)
    try:
        incident_monitor.authenticate(socket, config.token)
        document = ha_inventory._command(socket, 31, "config_entries/get")
    finally:
        try:
            socket.close()
        except Exception:
            pass
    if not isinstance(document, list) or len(document) > 2048:
        raise IntegrationRecoveryError("Home Assistant integration state is invalid")
    result: dict[str, str] = {}
    for item in document:
        if not isinstance(item, dict):
            raise IntegrationRecoveryError("Home Assistant integration state is invalid")
        entry_id = ha_inventory._valid_entry_id(item.get("entry_id"))
        state = item.get("state")
        if entry_id is None or not isinstance(state, str):
            continue
        if not re.fullmatch(r"[a-z_]{2,32}", state):
            state = "other"
        result[entry_id] = state
    return result


def _integration_context(
    inventory: dict[str, Any], domain: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    profiles = inventory.get("integration_profiles")
    entries = inventory.get("config_entries")
    if not isinstance(profiles, list) or not isinstance(entries, list):
        raise IntegrationRecoveryError("private inventory is invalid")
    profile_matches = [
        item for item in profiles
        if isinstance(item, dict) and item.get("domain") == domain
    ]
    if len(profile_matches) != 1:
        raise IntegrationRecoveryError("integration profile is ambiguous")
    profile = profile_matches[0]
    mode = profile.get("recovery_mode")
    if not isinstance(mode, str) or mode not in {
        "diagnose_only", "local_rebind_reload", "entry_reload",
        "idle_entry_reload", "cloud_backoff_entry_reload", "cloud_backoff",
        "permissioned_entry_reload",
    }:
        raise IntegrationRecoveryError("integration profile is invalid")
    matching: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict) or item.get("domain") != domain:
            continue
        entry_id = ha_inventory._valid_entry_id(item.get("entry_id"))
        if entry_id is None:
            raise IntegrationRecoveryError("integration entry is invalid")
        matching.append(item)
    return profile, matching


def _device_activity(
    inventory: dict[str, Any],
    entry_id: str,
    config: ha_read.AdapterConfig,
    *,
    raw_state_reader: Callable[[ha_read.AdapterConfig, str], Any] = ha_read.request_json,
) -> str:
    devices = inventory.get("physical_devices")
    if not isinstance(devices, list):
        raise IntegrationRecoveryError("private device inventory is invalid")
    entity_ids: set[str] = set()
    for item in devices:
        if not isinstance(item, dict):
            raise IntegrationRecoveryError("private device inventory is invalid")
        entry_ids = item.get("config_entry_ids")
        members = item.get("entity_ids")
        if not isinstance(entry_ids, list) or entry_id not in entry_ids:
            continue
        if not isinstance(members, list) or len(members) > 512:
            raise IntegrationRecoveryError("private device inventory is invalid")
        entity_ids.update(ha_read._validate_entity_id(value) for value in members)
    if not entity_ids:
        return "unknown"
    document = raw_state_reader(config, "/api/states")
    if not isinstance(document, list) or len(document) > 8192:
        raise IntegrationRecoveryError("Home Assistant device state is invalid")
    observed: dict[str, str] = {}
    for item in document:
        if not isinstance(item, dict) or item.get("entity_id") not in entity_ids:
            continue
        state = item.get("state")
        if not isinstance(state, str) or len(state) > 64:
            continue
        observed[str(item["entity_id"])] = state.casefold()
    if any(
        state in ACTIVE_STATES
        for state in observed.values()
    ):
        return "active"
    if any(
        entity_id.startswith(("switch.", "light.")) and state == "on"
        for entity_id, state in observed.items()
    ):
        return "active"
    status_states = [
        state for entity_id, state in observed.items()
        if entity_id.startswith(("sensor.", "select."))
    ]
    if any(state in IDLE_STATES for state in status_states):
        return "idle"
    return "unknown"


def post_entry_reload(config: ha_read.AdapterConfig, entry_id: str) -> None:
    normalized = ha_inventory._valid_entry_id(entry_id)
    if normalized is None:
        raise IntegrationRecoveryError("integration entry is invalid")
    body = json.dumps(
        {"entry_id": normalized}, ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")
    ha_recovery._post_service(config, ENTRY_RELOAD_PATH, body)


def _verify_entry(
    config: ha_read.AdapterConfig,
    entry_id: str,
    *,
    state_reader: Callable[[ha_read.AdapterConfig], dict[str, str]],
    sleeper: Callable[[float], None],
) -> tuple[bool, int, str]:
    previous = 0
    observed = "unknown"
    for offset in VERIFY_OFFSETS_SECONDS:
        sleeper(offset - previous)
        previous = offset
        observed = state_reader(config).get(entry_id, "unknown")
        if observed == "loaded":
            return True, VERIFY_OFFSETS_SECONDS.index(offset) + 1, observed
    return False, len(VERIFY_OFFSETS_SECONDS), observed


def run_once(
    store: incident_monitor.IncidentStore,
    inventory: dict[str, Any],
    *,
    now: int | None = None,
    live: bool,
    config_loader: Callable[[], ha_read.AdapterConfig] = ha_read.load_config,
    state_reader: Callable[[ha_read.AdapterConfig], dict[str, str]] = _entry_states,
    raw_state_reader: Callable[[ha_read.AdapterConfig, str], Any] = ha_read.request_json,
    cloud_probe: Callable[[], bool] = automation_recovery.probe_yandex_cloud,
    plan_fn: Callable[..., dict[str, object]] = recovery_planner.plan_one,
    entry_reload_caller: Callable[[ha_read.AdapterConfig, str], None] = post_entry_reload,
    local_reload_caller: Callable[[ha_read.AdapterConfig], None] = ha_recovery.post_localtuya_reload,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    current = int(time.time()) if now is None else now
    incidents = [
        item for item in store.operational_incident_candidates()
        if item.get("source_type") == "integration"
        and item.get("cause_code") == "integration_not_loaded"
    ]
    if not incidents:
        return {
            "schema_version": 1,
            "mode": "live" if live else "dry_run",
            "incidents": 0,
            "service_calls": 0,
        }
    incident = incidents[0]
    incident_id = int(incident["incident_id"])
    next_allowed = store.operational_next_allowed_epoch(incident_id)
    if next_allowed is not None and current < next_allowed:
        return {
            "schema_version": 1,
            "mode": "live" if live else "dry_run",
            "incidents": 1,
            "outcome": "cooldown",
            "next_allowed_epoch": next_allowed,
            "service_calls": 0,
        }
    domain = str(incident["automation_entity_id"])
    if re.fullmatch(r"[a-z0-9_]{1,64}", domain) is None:
        raise IntegrationRecoveryError("integration incident is invalid")
    profile, entries = _integration_context(inventory, domain)
    config = config_loader()
    current_states = state_reader(config)
    failed_entries = [
        item for item in entries
        if current_states.get(str(item["entry_id"]), "unknown") != "loaded"
    ]
    if not failed_entries:
        store.resolve_operational_incident(
            incident_id, current, "integration_healthy"
        )
        return {
            "schema_version": 1,
            "mode": "live" if live else "dry_run",
            "incidents": 1,
            "outcome": "already_healthy",
            "service_calls": 0,
        }
    entry_match = "single" if len(failed_entries) == 1 else "ambiguous"
    entry_id = (
        str(failed_entries[0]["entry_id"])
        if len(failed_entries) == 1 else None
    )
    mode = str(profile["recovery_mode"])
    automatic_allowed = profile.get("automatic_recovery_allowed") is True
    activity = "unknown"
    if mode == "idle_entry_reload" and entry_id is not None:
        activity = _device_activity(
            inventory, entry_id, config, raw_state_reader=raw_state_reader
        )
    cloud = "unknown"
    if mode == "cloud_backoff_entry_reload":
        cloud = "reachable" if cloud_probe() else "unreachable"
    runtime = {
        "integration_profile": mode if automatic_allowed else "diagnose_only",
        "entry_match": entry_match,
        "device_activity": activity,
        "yandex_cloud": cloud,
        "retry_budget": (
            "available"
            if store.operational_attempt_count(incident_id) == 0
            else "exhausted"
        ),
        "integration": "unhealthy",
        "config_entry": "known" if entry_id is not None else "unknown",
    }
    facts = recovery_planner.build_facts(incident, runtime)
    candidates = recovery_planner.build_candidates(facts)
    if not live:
        return {
            "schema_version": 1,
            "mode": "dry_run",
            "incidents": 1,
            "candidate_ids": [str(item["id"]) for item in candidates],
            "runtime_facts": runtime,
            "service_calls": 0,
        }
    decision = plan_fn(store, incident, runtime, now=current)
    candidate = str(decision["candidate_id"])
    decision_id = str(decision["decision_id"])
    before = (
        current_states.get(entry_id, "unknown") if entry_id is not None
        else "unknown"
    )
    after = before
    status = "no_action"
    calls = 0
    checks = 0
    evidence = "observation_recorded"
    next_epoch = current + OBSERVE_COOLDOWN_SECONDS
    allowed_candidate = candidate in {
        "reload_integration_entry_once", "reload_local_integration_once"
    }
    guard_ok = bool(
        allowed_candidate
        and automatic_allowed
        and mode in ALLOWED_AUTOMATIC_MODES
        and entry_id is not None
        and len(failed_entries) == 1
        and store.operational_attempt_count(incident_id) == 0
        and (mode != "idle_entry_reload" or activity == "idle")
        and (mode != "cloud_backoff_entry_reload" or cloud == "reachable")
        and (
            candidate == "reload_local_integration_once"
            if mode == "local_rebind_reload"
            else candidate == "reload_integration_entry_once"
        )
    )
    if allowed_candidate and not guard_ok:
        status = "rejected"
        evidence = "integration_reload_guard_rejected"
    elif guard_ok:
        calls = 1
        try:
            if candidate == "reload_local_integration_once":
                local_reload_caller(config)
            else:
                entry_reload_caller(config, entry_id)
            verified, checks, after = _verify_entry(
                config,
                entry_id,
                state_reader=state_reader,
                sleeper=sleeper,
            )
            status = "verified" if verified else "failed"
            evidence = (
                "integration_readback_confirmed"
                if verified else "integration_readback_failed"
            )
        except Exception as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            status = "delivery_unknown"
            evidence = "integration_reload_delivery_unknown"
        next_epoch = current + RELOAD_COOLDOWN_SECONDS
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
    if status == "verified":
        store.resolve_operational_incident(
            incident_id, current, "integration_healthy"
        )
    elif status == "failed":
        store.escalate_operational_incident(
            incident_id,
            current,
            cause_code="integration_not_loaded",
            cause_confidence="confirmed",
            evidence_code="integration_not_loaded_after_three_checks",
        )
    return {
        "schema_version": 1,
        "mode": "live",
        "incidents": 1,
        "incident_id": incident_id,
        "candidate_id": candidate,
        "decision_source": decision["source"],
        "outcome": status,
        "service_calls": calls,
        "verification_checks": checks,
        "next_allowed_epoch": next_epoch,
    }


def main() -> int:
    live = os.environ.get(
        "HOME_BUTLER_INTEGRATION_RECOVERY_MODE", "dry-run"
    ) == "live"
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
        IntegrationRecoveryError,
        incident_monitor.MonitorError,
        recovery_planner.PlannerError,
        ha_read.AdapterError,
        ha_recovery.RecoveryError,
        OSError,
        sqlite3.Error,
    ):
        print("HOME_ASSISTANT_INTEGRATION_RECOVERY_FAILED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
