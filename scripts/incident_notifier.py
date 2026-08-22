#!/usr/bin/env python3
"""Send deduplicated critical and new-sensor notices through YandexStation."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_notify as ha_notify  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402
import home_assistant_inventory as ha_inventory  # noqa: E402
import home_assistant_recovery as ha_recovery  # noqa: E402
import incident_monitor  # noqa: E402


MAX_ATTEMPTS = 3
RETRY_SECONDS = 30


def _live_mode() -> bool:
    return os.environ.get("HOME_BUTLER_ALICE_NOTIFY", "dry-run") == "live"


def run_once(store: incident_monitor.IncidentStore, *, now: int | None = None) -> dict[str, object]:
    observed_epoch = int(time.time()) if now is None else now
    live = _live_mode()
    rollup = store.reconcile_device_incidents(observed_epoch)
    diagnosis = {"candidates": 0, "devices": 0, "service_calls": 0}
    try:
        inventory_path = incident_monitor._state_dir() / ha_inventory.INVENTORY_NAME
        diagnosis = ha_recovery.diagnose_open_device_incidents(
            store,
            ha_recovery.load_platform_map(inventory_path),
            ip_drift_map=ha_recovery.load_ip_drift_map(inventory_path),
        )
    except (ha_recovery.RecoveryError, incident_monitor.MonitorError, OSError):
        # Notification must remain available if optional private inventory is stale.
        pass
    candidates = store.notification_candidates(
        observed_epoch,
        retry_seconds=RETRY_SECONDS,
        max_attempts=MAX_ATTEMPTS,
        include_sensor=False,
    )
    candidates.extend(store.device_notification_candidates(
        observed_epoch, retry_seconds=RETRY_SECONDS, max_attempts=MAX_ATTEMPTS
    ))
    operational_candidates = store.operational_notification_candidates(
        observed_epoch,
        retry_seconds=RETRY_SECONDS,
        max_attempts=MAX_ATTEMPTS,
    )
    for item in operational_candidates:
        item["notification_kind"] = "operational"
    candidates.extend(operational_candidates)
    accepted = 0
    failed = 0
    for candidate in candidates:
        speaker: str | None = None
        success = False
        operational = candidate.get("notification_kind") == "operational"
        operational_attempt = 0
        if live and operational:
            operational_attempt = store.claim_operational_notification(
                int(candidate["operational_incident_id"]),
                str(candidate["phase"]),
                observed_epoch,
                retry_seconds=RETRY_SECONDS,
                max_attempts=MAX_ATTEMPTS,
            )
            if not operational_attempt:
                continue
        try:
            if operational:
                resolved_epoch = candidate.get("resolved_epoch")
                duration_seconds = (
                    int(resolved_epoch) - int(candidate["first_observed_epoch"])
                    if isinstance(resolved_epoch, int) else None
                )
                result = ha_notify.send_operational_incident(
                    str(candidate["display_name"]),
                    str(candidate["phase"]),
                    cause_code=str(candidate["cause_code"]),
                    action_code=str(candidate["action_code"]),
                    duration_seconds=duration_seconds,
                    detected_was_announced=bool(candidate["detected_was_announced"]),
                    agent_recovered=bool(candidate["agent_recovered"]),
                    source_type=str(candidate["source_type"]),
                    live=live,
                )
            elif candidate.get("notification_kind") == "device":
                resolved_epoch = candidate.get("resolved_epoch")
                duration_seconds = (
                    int(resolved_epoch) - int(candidate["first_observed_epoch"])
                    if isinstance(resolved_epoch, int)
                    else None
                )
                result = ha_notify.send_device_incident(
                    str(candidate["display_name"]),
                    str(candidate["phase"]),
                    cause_code=str(candidate["cause_code"]),
                    duration_seconds=duration_seconds,
                    live=live,
                )
            else:
                result = ha_notify.send_incident(
                    str(candidate["subject"]), str(candidate["phase"]), live=live
                )
            speaker = str(result["speaker_entity_id"])
            success = bool(result["ok"])
        except ha_notify.NotifyDeliveryUnknown:
            if live:
                if operational:
                    store.finalize_operational_notification(
                        int(candidate["operational_incident_id"]),
                        str(candidate["phase"]),
                        observed_epoch,
                        status="delivery_unknown",
                        speaker_entity_id=speaker,
                    )
                elif candidate.get("notification_kind") == "device":
                    store.record_device_notification(
                        int(candidate["device_incident_id"]),
                        str(candidate["phase"]),
                        observed_epoch,
                        status="delivery_unknown",
                        speaker_entity_id=speaker,
                        max_attempts=1,
                    )
                else:
                    store.record_notification(
                        int(candidate["incident_id"]),
                        str(candidate["phase"]),
                        observed_epoch,
                        accepted=False,
                        speaker_entity_id=speaker,
                        max_attempts=1,
                    )
            failed += 1
            continue
        except (ha_notify.NotifyError, ha_read.AdapterError):
            if live and operational and operational_attempt >= MAX_ATTEMPTS:
                store.finalize_operational_notification(
                    int(candidate["operational_incident_id"]),
                    str(candidate["phase"]),
                    observed_epoch,
                    status="delivery_unknown",
                    speaker_entity_id=speaker,
                )
            success = False
        if live:
            if operational:
                if success:
                    store.finalize_operational_notification(
                        int(candidate["operational_incident_id"]),
                        str(candidate["phase"]),
                        observed_epoch,
                        status="accepted",
                        speaker_entity_id=speaker,
                    )
                elif operational_attempt >= MAX_ATTEMPTS:
                    store.finalize_operational_notification(
                        int(candidate["operational_incident_id"]),
                        str(candidate["phase"]),
                        observed_epoch,
                        status="delivery_unknown",
                        speaker_entity_id=speaker,
                    )
            elif candidate.get("notification_kind") == "device":
                store.record_device_notification(
                    int(candidate["device_incident_id"]),
                    str(candidate["phase"]),
                    observed_epoch,
                    status="accepted" if success else "failed",
                    speaker_entity_id=speaker,
                    max_attempts=MAX_ATTEMPTS,
                )
            else:
                store.record_notification(
                    int(candidate["incident_id"]),
                    str(candidate["phase"]),
                    observed_epoch,
                    accepted=success,
                    speaker_entity_id=speaker,
                    max_attempts=MAX_ATTEMPTS,
                )
        accepted += int(success)
        failed += int(not success)
    return {
        "schema_version": 1,
        "mode": "live" if live else "dry_run",
        "candidates": len(candidates),
        "accepted": accepted,
        "failed": failed,
        "service_calls": accepted if live else 0,
        "device_rollup": rollup,
        "device_diagnosis": diagnosis,
    }


def main() -> int:
    state_dir = incident_monitor._state_dir()
    try:
        incident_monitor._validate_directory(state_dir)
        store = incident_monitor.IncidentStore(state_dir / incident_monitor.DATABASE_NAME)
        try:
            print(json.dumps(run_once(store), ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        finally:
            store.close()
        return 0
    except (
        incident_monitor.MonitorError,
        ha_notify.NotifyError,
        ha_read.AdapterError,
        OSError,
        sqlite3.Error,
    ):
        print("INCIDENT_NOTIFIER_FAILED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
