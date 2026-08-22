#!/usr/bin/env python3
"""Perform one bounded integration-supported Tuya recovery action."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import sqlite3
import stat
import sys
import time
from pathlib import Path
from typing import Any, Callable


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_inventory as ha_inventory  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402
import incident_monitor  # noqa: E402


LOCALTUYA_SERVICE_PATH = "/api/services/localtuya/reload"
LOCALTUYA_ACTION = "localtuya.reload"
LOCALTUYA_INTEGRATION = "localtuya"
TUYA_LOCAL_SERVICE_PATH = "/api/services/homeassistant/reload_config_entry"
TUYA_LOCAL_ACTION = "homeassistant.reload_config_entry"
TUYA_LOCAL_INTEGRATION = "tuya_local"
XIAOMI_MIOT_ACTION = "homeassistant.reload_config_entry"
XIAOMI_MIOT_INTEGRATION = "xiaomi_miot"
XIAOMI_APPROVAL_VALUE = "approved"
COOLDOWN_SECONDS = 3600
VERIFY_OFFSETS_SECONDS = (20, 40, 60)
REQUEST_TIMEOUT_SECONDS = 25
MAX_INVENTORY_BYTES = 4 * 1_048_576


class RecoveryError(RuntimeError):
    """Secret-free recovery failure."""


class RecoveryDeliveryUnknown(RecoveryError):
    """The reload request was sent but no response was received."""


def _connection(config: ha_read.AdapterConfig) -> http.client.HTTPConnection:
    return http.client.HTTPConnection(config.host, config.port, timeout=REQUEST_TIMEOUT_SECONDS)


def _post_service(
    config: ha_read.AdapterConfig,
    path: str,
    body: bytes,
    *,
    connection_factory: Callable[[ha_read.AdapterConfig], http.client.HTTPConnection] = _connection,
) -> None:
    request_sent = False
    try:
        connection = connection_factory(config)
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {config.token}",
                "Content-Type": "application/json",
                "Connection": "close",
            },
        )
        request_sent = True
        response = connection.getresponse()
        raw = response.read(ha_read.MAX_RESPONSE_BYTES + 1)
        if response.status != 200 or len(raw) > ha_read.MAX_RESPONSE_BYTES:
            raise RecoveryError("Home Assistant rejected bounded recovery")
        if not isinstance(ha_read.strict_json_loads(raw), (list, dict)):
            raise RecoveryError("Home Assistant returned an invalid recovery response")
    except RecoveryError:
        raise
    except (OSError, socket.timeout, TimeoutError, http.client.HTTPException) as error:
        if request_sent:
            raise RecoveryDeliveryUnknown("Tuya recovery delivery is unknown") from error
        raise RecoveryError("Tuya recovery failed") from error
    finally:
        try:
            connection.close()
        except (UnboundLocalError, OSError, http.client.HTTPException):
            pass


def post_localtuya_reload(
    config: ha_read.AdapterConfig,
    *,
    connection_factory: Callable[[ha_read.AdapterConfig], http.client.HTTPConnection] = _connection,
) -> None:
    _post_service(
        config,
        LOCALTUYA_SERVICE_PATH,
        b"{}",
        connection_factory=connection_factory,
    )


def post_tuya_local_reload(
    config: ha_read.AdapterConfig,
    entity_id: str,
    *,
    connection_factory: Callable[[ha_read.AdapterConfig], http.client.HTTPConnection] = _connection,
) -> None:
    normalized = ha_read._validate_entity_id(entity_id)
    body = json.dumps(
        {"entity_id": normalized}, ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")
    _post_service(
        config,
        TUYA_LOCAL_SERVICE_PATH,
        body,
        connection_factory=connection_factory,
    )


def load_platform_map(path: Path) -> dict[str, str]:
    document = _load_inventory_document(path)
    entities = document.get("entities")
    if not isinstance(entities, list):
        raise RecoveryError("inventory file is invalid")
    result: dict[str, str] = {}
    for item in entities:
        if not isinstance(item, dict):
            raise RecoveryError("inventory file is invalid")
        entity_id = item.get("entity_id")
        platform = item.get("platform")
        try:
            normalized = ha_read._validate_entity_id(entity_id)
        except ha_read.AdapterError as error:
            raise RecoveryError("inventory file is invalid") from error
        if not isinstance(platform, str) or not ha_inventory.PLATFORM_RE.fullmatch(platform):
            raise RecoveryError("inventory file is invalid")
        if normalized in result:
            raise RecoveryError("inventory file is ambiguous")
        result[normalized] = platform
    return result


def load_xiaomi_entry_map(path: Path) -> dict[str, str]:
    """Map Xiaomi entities only when inventory proves one exact config entry."""
    document = _load_inventory_document(path)
    entries = document.get("config_entries")
    entities = document.get("entities")
    if not isinstance(entries, list) or not isinstance(entities, list):
        raise RecoveryError("inventory file is invalid")
    valid_entries: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise RecoveryError("inventory file is invalid")
        entry_id = item.get("entry_id")
        if (
            item.get("domain") == XIAOMI_MIOT_INTEGRATION
            and isinstance(entry_id, str)
            and ha_inventory.ENTRY_ID_RE.fullmatch(entry_id)
        ):
            valid_entries.add(entry_id)
    result: dict[str, str] = {}
    for item in entities:
        if not isinstance(item, dict):
            raise RecoveryError("inventory file is invalid")
        if item.get("platform") != XIAOMI_MIOT_INTEGRATION:
            continue
        try:
            entity_id = ha_read._validate_entity_id(item.get("entity_id"))
        except ha_read.AdapterError as error:
            raise RecoveryError("inventory file is invalid") from error
        entry_ids = item.get("config_entry_ids")
        if (
            not isinstance(entry_ids, list)
            or len(entry_ids) != 1
            or entry_ids[0] not in valid_entries
            or entity_id in result
        ):
            continue
        result[entity_id] = entry_ids[0]
    return result


def _load_inventory_document(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_size <= 0
            or metadata.st_size > MAX_INVENTORY_BYTES
        ):
            raise RecoveryError("inventory file is unsafe")
        document = ha_read.strict_json_loads(path.read_bytes())
    except (OSError, ha_read.AdapterError) as error:
        raise RecoveryError("inventory file is unavailable") from error
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise RecoveryError("inventory file is invalid")
    return document


def load_ip_drift_map(path: Path) -> dict[str, str]:
    document = _load_inventory_document(path)
    entities = document.get("entities")
    bindings = document.get("identity_bindings", [])
    if not isinstance(entities, list) or not isinstance(bindings, list):
        raise RecoveryError("inventory file is invalid")
    status_by_device: dict[str, str] = {}
    for item in bindings:
        if not isinstance(item, dict):
            raise RecoveryError("inventory file is invalid")
        device_id = item.get("device_id")
        status_value = item.get("status")
        if (
            not isinstance(device_id, str)
            or not ha_inventory.DEVICE_ID_RE.fullmatch(device_id)
            or status_value not in {"stable", "ip_changed", "not_observed"}
            or device_id in status_by_device
        ):
            raise RecoveryError("inventory file is invalid")
        status_by_device[device_id] = str(status_value)
    result: dict[str, str] = {}
    for item in entities:
        if not isinstance(item, dict):
            raise RecoveryError("inventory file is invalid")
        try:
            entity_id = ha_read._validate_entity_id(item.get("entity_id"))
        except ha_read.AdapterError as error:
            raise RecoveryError("inventory file is invalid") from error
        device_id = item.get("device_id")
        if isinstance(device_id, str) and device_id in status_by_device:
            result[entity_id] = status_by_device[device_id]
    return result


def _snapshot_states(
    snapshot_reader: Callable[[str], tuple[dict[str, Any], int]],
) -> dict[str, str]:
    snapshot, exit_code = snapshot_reader("snapshot")
    entities = snapshot.get("entities") if isinstance(snapshot, dict) else None
    if exit_code != 0 or not isinstance(entities, list):
        raise RecoveryError("Home Assistant snapshot failed")
    return {
        item["entity_id"]: (
            "unavailable" if item.get("state_kind") == "unavailable" else "available"
        )
        for item in entities
        if isinstance(item, dict) and isinstance(item.get("entity_id"), str)
    }


def _verify_members_available(
    snapshot_reader: Callable[[str], tuple[dict[str, Any], int]],
    subjects: list[str],
    *,
    sleeper: Callable[[float], None],
) -> tuple[dict[str, str], int]:
    if not subjects:
        raise RecoveryError("physical device has no verification members")
    previous_offset = 0
    latest: dict[str, str] = {}
    checks = 0
    for offset in VERIFY_OFFSETS_SECONDS:
        sleeper(offset - previous_offset)
        previous_offset = offset
        checks += 1
        try:
            latest = _snapshot_states(snapshot_reader)
        except RecoveryError:
            latest = {}
        if all(latest.get(subject) == "available" for subject in subjects):
            break
    return latest, checks


def _classify_candidate_group(
    candidates: list[dict[str, object]],
    platform_map: dict[str, str],
    ip_drift_map: dict[str, str] | None = None,
) -> tuple[str, str, str, str]:
    """Classify one physical-device group without performing an action."""
    if not candidates:
        raise RecoveryError("recovery diagnosis has no candidates")
    drift = ip_drift_map or {}
    subjects = [str(item["subject"]) for item in candidates]
    platforms = {platform_map.get(subject) for subject in subjects}
    if any(drift.get(subject) == "ip_changed" for subject in subjects):
        return (
            "confirmed_ip_change",
            "confirmed_ip_change",
            "confirmed",
            "network_inventory",
        )
    if any(drift.get(subject) == "not_observed" for subject in subjects):
        return (
            "device_not_observed_on_lan",
            "device_not_observed_on_lan",
            "confirmed",
            "network_inventory",
        )
    if platforms & {LOCALTUYA_INTEGRATION, TUYA_LOCAL_INTEGRATION}:
        return (
            "integration_unavailable",
            "tuya_integration_unavailable",
            "probable",
            "integration_reload_candidate",
        )
    if XIAOMI_MIOT_INTEGRATION in platforms:
        return (
            "integration_unavailable",
            "integration_not_loaded",
            "probable",
            "integration_reload_candidate",
        )
    raise RecoveryError("recovery diagnosis platform is unsupported")


def diagnose_open_device_incidents(
    store: incident_monitor.IncidentStore,
    platform_map: dict[str, str],
    *,
    ip_drift_map: dict[str, str] | None = None,
) -> dict[str, int]:
    """Enrich every supported open device incident without HA service calls."""
    supported = {
        LOCALTUYA_INTEGRATION,
        TUYA_LOCAL_INTEGRATION,
        XIAOMI_MIOT_INTEGRATION,
    }
    groups: dict[str, list[dict[str, object]]] = {}
    for item in store.recovery_candidates():
        subject = str(item["subject"])
        if platform_map.get(subject) not in supported:
            continue
        physical_hash = store.physical_hash_for_entity(subject)
        groups.setdefault(physical_hash, []).append(item)
    diagnosed = 0
    candidates_seen = 0
    for candidates in groups.values():
        candidates_seen += len(candidates)
        _diagnosis, cause_code, confidence, evidence_code = (
            _classify_candidate_group(candidates, platform_map, ip_drift_map)
        )
        if store.diagnose_device_incident_for_subject(
            str(candidates[0]["subject"]),
            cause_code=cause_code,
            cause_confidence=confidence,
            evidence_code=evidence_code,
        ):
            diagnosed += 1
    return {"candidates": candidates_seen, "devices": diagnosed, "service_calls": 0}


def run_once(
    store: incident_monitor.IncidentStore,
    platform_map: dict[str, str],
    *,
    now: int | None = None,
    live: bool,
    snapshot_reader: Callable[[str], tuple[dict[str, Any], int]] = ha_read.execute_safely,
    localtuya_caller: Callable[[ha_read.AdapterConfig], None] = post_localtuya_reload,
    tuya_local_caller: Callable[[ha_read.AdapterConfig, str], None] = post_tuya_local_reload,
    xiaomi_caller: Callable[[ha_read.AdapterConfig, str], None] = post_tuya_local_reload,
    sleeper: Callable[[float], None] = time.sleep,
    ip_drift_map: dict[str, str] | None = None,
    xiaomi_entry_map: dict[str, str] | None = None,
    xiaomi_approved: bool = False,
) -> dict[str, object]:
    attempted_epoch = int(time.time()) if now is None else now
    store.reconcile_device_incidents(attempted_epoch)
    all_candidates = store.recovery_candidates()
    allowed_safety = {"sensor", "light", "ordinary_relay"}
    blocked_candidates = [
        item for item in all_candidates
        if store.device_safety_class_for_entity(str(item["subject"]))
        not in allowed_safety
    ]
    all_candidates = [
        item for item in all_candidates
        if store.device_safety_class_for_entity(str(item["subject"]))
        in allowed_safety
    ]
    drift = ip_drift_map or {}
    localtuya_candidates = sorted([
        item for item in all_candidates
        if platform_map.get(str(item["subject"])) == LOCALTUYA_INTEGRATION
    ], key=lambda item: (drift.get(str(item["subject"])) != "ip_changed", int(item["incident_id"])))
    tuya_local_candidates = [
        item for item in all_candidates
        if platform_map.get(str(item["subject"])) == TUYA_LOCAL_INTEGRATION
    ]
    xiaomi_entries = xiaomi_entry_map or {}
    xiaomi_groups: dict[str, list[dict[str, object]]] = {}
    for item in all_candidates:
        subject = str(item["subject"])
        entry_id = xiaomi_entries.get(subject)
        if (
            platform_map.get(subject) == XIAOMI_MIOT_INTEGRATION
            and entry_id is not None
        ):
            xiaomi_groups.setdefault(entry_id, []).append(item)
    if localtuya_candidates:
        seed = localtuya_candidates[0]
        action_subject = str(seed["subject"])
        physical_hash = store.physical_hash_for_entity(str(seed["subject"]))
        candidates = [
            item for item in all_candidates
            if store.physical_hash_for_entity(str(item["subject"])) == physical_hash
        ]
        integration = LOCALTUYA_INTEGRATION
        action = LOCALTUYA_ACTION
    elif tuya_local_candidates:
        seed = tuya_local_candidates[0]
        action_subject = str(seed["subject"])
        physical_hash = store.physical_hash_for_entity(str(seed["subject"]))
        candidates = [
            item for item in all_candidates
            if store.physical_hash_for_entity(str(item["subject"])) == physical_hash
        ]
        integration = TUYA_LOCAL_INTEGRATION
        action = TUYA_LOCAL_ACTION
    elif xiaomi_groups:
        _entry_id, candidates = min(
            xiaomi_groups.items(),
            key=lambda pair: min(int(item["incident_id"]) for item in pair[1]),
        )
        candidates = sorted(candidates, key=lambda item: int(item["incident_id"]))
        action_subject = str(candidates[0]["subject"])
        integration = XIAOMI_MIOT_INTEGRATION
        action = XIAOMI_MIOT_ACTION
    else:
        candidates = []
        action_subject = ""
        integration = LOCALTUYA_INTEGRATION
        action = LOCALTUYA_ACTION
    if not candidates and blocked_candidates:
        return {
            "schema_version": 1,
            "mode": "live" if live else "dry_run",
            "candidates": len(blocked_candidates),
            "service_calls": 0,
            "verified": 0,
            "outcome": "permission_required",
            "safety_class": "restricted_or_unknown",
        }
    last_attempt = store.last_recovery_epoch(integration, action)
    if last_attempt is not None and attempted_epoch - last_attempt < COOLDOWN_SECONDS:
        candidates = []
    if not candidates:
        return {
            "schema_version": 1, "mode": "live" if live else "dry_run",
            "candidates": 0, "service_calls": 0, "verified": 0,
        }
    before = _snapshot_states(snapshot_reader)
    candidates = [
        item for item in candidates
        if before.get(str(item["subject"])) == "unavailable"
    ]
    if not candidates:
        return {
            "schema_version": 1, "mode": "live" if live else "dry_run",
            "candidates": 0, "service_calls": 0, "verified": 0,
        }
    diagnosis, cause_code, confidence, evidence_code = _classify_candidate_group(
        candidates, platform_map, drift
    )
    store.diagnose_device_incident_for_subject(
        str(candidates[0]["subject"]),
        cause_code=cause_code,
        cause_confidence=confidence,
        evidence_code=evidence_code,
    )
    if integration == XIAOMI_MIOT_INTEGRATION and not xiaomi_approved:
        return {
            "schema_version": 1,
            "mode": "live" if live else "dry_run",
            "candidates": len(candidates),
            "service_calls": 0,
            "verified": 0,
            "outcome": "permission_required",
            "integration": integration,
            "action": action,
            "diagnosis": diagnosis,
            "recovery_scope": "one_config_entry",
        }
    if not live:
        return {
            "schema_version": 1, "mode": "dry_run",
            "candidates": len(candidates), "service_calls": 0, "verified": 0,
            "diagnosis": diagnosis,
        }

    group_seed = f"{attempted_epoch}:{','.join(str(item['incident_id']) for item in candidates)}"
    action_group_id = hashlib.sha256(group_seed.encode("ascii")).hexdigest()[:32]
    outcome = "accepted"
    service_calls = 1
    try:
        config = ha_read.load_config()
        if integration == LOCALTUYA_INTEGRATION:
            localtuya_caller(config)
        elif integration == TUYA_LOCAL_INTEGRATION:
            tuya_local_caller(config, action_subject)
        else:
            xiaomi_caller(config, action_subject)
    except RecoveryDeliveryUnknown:
        outcome = "delivery_unknown"
    except (RecoveryError, ha_read.AdapterError):
        outcome = "failed"
        service_calls = 0

    verification_subjects = sorted({
        subject
        for item in candidates
        for subject in store.physical_device_members(str(item["subject"]))
    })
    after: dict[str, str] = before
    verification_checks = 0
    if outcome == "accepted":
        after, verification_checks = _verify_members_available(
            snapshot_reader, verification_subjects, sleeper=sleeper
        )
    physical_device_verified = bool(verification_subjects) and all(
        after.get(subject) == "available" for subject in verification_subjects
    )
    verified = 0
    for index, item in enumerate(candidates):
        subject = str(item["subject"])
        after_state = after.get(subject, "unknown")
        item_status = "verified" if outcome == "accepted" and after_state == "available" else outcome
        verified += int(item_status == "verified")
        store.record_recovery(
            incident_id=int(item["incident_id"]),
            action_group_id=action_group_id,
            integration=integration,
            action=action,
            status=item_status,
            attempted_epoch=attempted_epoch,
            service_calls=service_calls if index == 0 else 0,
            verification_checks=verification_checks if index == 0 else 0,
            before_state="unavailable",
            after_state=after_state,
        )
    return {
        "schema_version": 1, "mode": "live", "candidates": len(candidates),
        "service_calls": service_calls, "verified": verified, "outcome": outcome,
        "integration": integration, "action": action,
        "diagnosis": diagnosis,
        "physical_device_verified": physical_device_verified,
        "verification_checks": verification_checks,
        "verified_member_count": sum(
            after.get(subject) == "available" for subject in verification_subjects
        ),
        "physical_member_count": len(verification_subjects),
    }


def main() -> int:
    live = os.environ.get("HOME_BUTLER_RECOVERY_MODE", "dry-run") == "live"
    state_dir = incident_monitor._state_dir()
    try:
        incident_monitor._validate_directory(state_dir)
        store = incident_monitor.IncidentStore(state_dir / incident_monitor.DATABASE_NAME)
        try:
            inventory_path = state_dir / ha_inventory.INVENTORY_NAME
            result = run_once(
                store,
                load_platform_map(inventory_path),
                live=live,
                ip_drift_map=load_ip_drift_map(inventory_path),
                xiaomi_entry_map=load_xiaomi_entry_map(inventory_path),
                xiaomi_approved=(
                    os.environ.get("HOME_BUTLER_XIAOMI_RECOVERY", "disabled")
                    == XIAOMI_APPROVAL_VALUE
                ),
            )
        finally:
            store.close()
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 0
    except (
        RecoveryError,
        incident_monitor.MonitorError,
        ha_read.AdapterError,
        OSError,
        sqlite3.Error,
    ):
        print("HOME_ASSISTANT_RECOVERY_FAILED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
