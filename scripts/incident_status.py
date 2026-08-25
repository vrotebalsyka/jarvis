#!/usr/bin/env python3
"""Read a sanitized, private summary of Home Butler incidents."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import sqlite3
import stat
import sys
import time
from pathlib import Path
from typing import Any


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_read as ha_read  # noqa: E402
import home_assistant_inventory as ha_inventory  # noqa: E402
import incident_monitor  # noqa: E402
import incident_timeline  # noqa: E402


MAX_DATABASE_BYTES = 128 * 1_048_576
MAX_INVENTORY_BYTES = 8 * 1_048_576


class IncidentStatusError(RuntimeError):
    """A fixed, secret-free incident status failure."""


def _expected_uid() -> int:
    try:
        return int(pwd.getpwnam("homebutler").pw_uid)
    except (KeyError, OSError) as error:
        raise IncidentStatusError("service account is unavailable") from error


def _validate_path(path: Path, expected_uid: int) -> None:
    try:
        directory = path.parent.lstat()
        database = path.lstat()
    except OSError as error:
        raise IncidentStatusError("incident database is unavailable") from error
    if (
        not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != expected_uid
        or directory.st_mode & 0o077
        or not stat.S_ISREG(database.st_mode)
        or database.st_uid != expected_uid
        or database.st_mode & 0o077
        or database.st_size <= 0
        or database.st_size > MAX_DATABASE_BYTES
    ):
        raise IncidentStatusError("incident database is unsafe")


def _safe_subject(subject: Any, kind: Any) -> str:
    if subject == incident_monitor.RESERVED_SUBJECT and kind == "system":
        return subject
    if kind != "entity":
        raise IncidentStatusError("incident database is invalid")
    try:
        return ha_read._validate_entity_id(subject)
    except ha_read.AdapterError as error:
        raise IncidentStatusError("incident database is invalid") from error


def _actionable_platforms(
    inventory_path: Path,
    expected_uid: int,
    actionable_subjects: set[str],
) -> list[dict[str, object]]:
    if not actionable_subjects:
        return []
    try:
        metadata = inventory_path.lstat()
    except FileNotFoundError:
        return []
    except OSError as error:
        raise IncidentStatusError("private inventory is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or metadata.st_mode & 0o077
        or metadata.st_size <= 0
        or metadata.st_size > MAX_INVENTORY_BYTES
    ):
        raise IncidentStatusError("private inventory is unsafe")
    try:
        document = ha_read.strict_json_loads(inventory_path.read_bytes())
    except (OSError, ha_read.AdapterError) as error:
        raise IncidentStatusError("private inventory is invalid") from error
    if not isinstance(document, dict):
        raise IncidentStatusError("private inventory is invalid")
    entities = document.get("entities")
    config_entries = document.get("config_entries", [])
    capabilities = document.get("integration_capabilities", {})
    identity_bindings = document.get("identity_bindings", [])
    if (
        document.get("schema_version") not in {1, 2, 3}
        or not isinstance(entities, list)
        or not isinstance(config_entries, list)
        or not isinstance(capabilities, dict)
        or not isinstance(identity_bindings, list)
    ):
        raise IncidentStatusError("private inventory is invalid")

    xiaomi_entry_ids: set[str] = set()
    for entry in config_entries:
        if not isinstance(entry, dict):
            raise IncidentStatusError("private inventory is invalid")
        entry_id = entry.get("entry_id")
        if entry.get("domain") == "xiaomi_miot":
            if not isinstance(entry_id, str) or not ha_inventory.ENTRY_ID_RE.fullmatch(entry_id):
                raise IncidentStatusError("private inventory is invalid")
            xiaomi_entry_ids.add(entry_id)
    xiaomi_capability = capabilities.get("xiaomi_miot")
    xiaomi_reviewed = (
        isinstance(xiaomi_capability, dict)
        and xiaomi_capability.get("bounded_config_entry_reload") is True
        and xiaomi_capability.get("automatic_recovery_enabled") is False
    )
    observed_xiaomi_devices: set[str] = set()
    for binding in identity_bindings:
        if not isinstance(binding, dict):
            raise IncidentStatusError("private inventory is invalid")
        if binding.get("platform") != "xiaomi_miot":
            continue
        device_id = binding.get("device_id")
        binding_status = binding.get("status")
        if (
            not isinstance(device_id, str)
            or not ha_inventory.DEVICE_ID_RE.fullmatch(device_id)
            or binding_status not in {"stable", "ip_changed", "not_observed"}
        ):
            raise IncidentStatusError("private inventory is invalid")
        if binding_status != "not_observed":
            observed_xiaomi_devices.add(device_id)

    by_platform: dict[str, dict[str, object]] = {}
    seen_subjects: set[str] = set()
    for item in entities:
        if not isinstance(item, dict):
            raise IncidentStatusError("private inventory is invalid")
        try:
            entity_id = ha_read._validate_entity_id(item.get("entity_id"))
        except ha_read.AdapterError as error:
            raise IncidentStatusError("private inventory is invalid") from error
        if entity_id not in actionable_subjects:
            continue
        platform = item.get("platform")
        device_id = item.get("device_id")
        if (
            not isinstance(platform, str)
            or not ha_inventory.PLATFORM_RE.fullmatch(platform)
            or (
                device_id is not None
                and (
                    not isinstance(device_id, str)
                    or not ha_inventory.DEVICE_ID_RE.fullmatch(device_id)
                )
            )
            or entity_id in seen_subjects
        ):
            raise IncidentStatusError("private inventory is invalid")
        seen_subjects.add(entity_id)
        group = by_platform.setdefault(
            platform,
            {
                "entity_count": 0,
                "device_ids": set(),
                "unmapped_count": 0,
                "recovery_entry_ids": set(),
                "recovery_mapping_complete": True,
            },
        )
        group["entity_count"] = int(group["entity_count"]) + 1
        if device_id is None:
            group["unmapped_count"] = int(group["unmapped_count"]) + 1
        else:
            device_ids = group["device_ids"]
            if not isinstance(device_ids, set):
                raise IncidentStatusError("private inventory is invalid")
            device_ids.add(device_id)
        if platform == "xiaomi_miot":
            recovery_entry_ids = group["recovery_entry_ids"]
            entry_ids = item.get("config_entry_ids")
            if not isinstance(recovery_entry_ids, set):
                raise IncidentStatusError("private inventory is invalid")
            if (
                not isinstance(entry_ids, list)
                or len(entry_ids) != 1
                or entry_ids[0] not in xiaomi_entry_ids
            ):
                group["recovery_mapping_complete"] = False
            else:
                recovery_entry_ids.add(entry_ids[0])

    result = []
    for platform in sorted(by_platform):
        group = by_platform[platform]
        device_ids = group["device_ids"]
        if not isinstance(device_ids, set):
            raise IncidentStatusError("private inventory is invalid")
        rendered = {
            "platform": platform,
            "entity_count": int(group["entity_count"]),
            "device_count": len(device_ids),
            "unmapped_entity_count": int(group["unmapped_count"]),
        }
        if platform == "xiaomi_miot":
            recovery_entry_ids = group["recovery_entry_ids"]
            if not isinstance(recovery_entry_ids, set):
                raise IncidentStatusError("private inventory is invalid")
            ready = (
                xiaomi_reviewed
                and bool(group["recovery_mapping_complete"])
                and len(recovery_entry_ids) == 1
            )
            rendered.update({
                "recovery_status": "permission_required" if ready else "unavailable",
                "recovery_config_entry_count": len(recovery_entry_ids),
                "lan_observed_device_count": len(device_ids & observed_xiaomi_devices),
            })
        result.append(rendered)
    return result


def read_summary(
    path: Path | None = None,
    *,
    expected_uid: int | None = None,
    inventory_path: Path | None = None,
) -> dict[str, object]:
    database_path = path or (
        Path(os.environ.get(
            "HOME_BUTLER_INCIDENT_STATE_DIR",
            "/home/homebutler/.local/state/home-butler/incidents",
        ))
        / incident_monitor.DATABASE_NAME
    )
    owner_uid = _expected_uid() if expected_uid is None else expected_uid
    _validate_path(database_path, owner_uid)
    try:
        connection = sqlite3.connect(
            f"file:{database_path}?mode=ro", uri=True, timeout=3
        )
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT id,subject,kind,status,severity,last_state,baseline,
                       first_observed_epoch,last_observed_epoch
                FROM incidents
                WHERE status IN ('observed','confirmed','escalated')
                ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END,id DESC
                LIMIT 50
                """
            ).fetchall()
            operational_rows = connection.execute(
                """
                SELECT o.id,o.source_type,o.display_name,o.status,o.first_observed_epoch,
                       o.last_observed_epoch,o.cause_code,o.cause_confidence,
                       o.occurrences,
                       COALESCE((
                           SELECT a.action_code FROM automation_runs AS a
                           WHERE a.automation_entity_id=o.automation_entity_id
                             AND (
                               a.physical_device_hash=o.physical_device_hash
                               OR (a.physical_device_hash IS NULL
                                   AND o.physical_device_hash IS NULL)
                             )
                           ORDER BY a.observed_epoch DESC LIMIT 1
                       ),'service_action') AS action_code
                FROM operational_incidents AS o
                WHERE o.status IN ('confirmed','escalated')
                ORDER BY o.last_observed_epoch DESC
                LIMIT 50
                """
            ).fetchall()
            device_rows = connection.execute(
                """
                SELECT id,display_name,status,first_observed_epoch,
                       last_observed_epoch,cause_code,cause_confidence,
                       safety_class
                FROM device_incidents
                WHERE baseline=0 AND status IN ('observed','confirmed','escalated')
                ORDER BY CASE status WHEN 'escalated' THEN 0
                                     WHEN 'confirmed' THEN 1 ELSE 2 END,
                         last_observed_epoch DESC
                LIMIT 50
                """
            ).fetchall()
            device_member_rows = connection.execute(
                """
                SELECT members.device_incident_id,members.entity_id
                FROM device_incident_members AS members
                JOIN device_incidents AS device
                  ON device.id=members.device_incident_id
                WHERE device.baseline=0
                  AND device.status IN ('observed','confirmed','escalated')
                ORDER BY members.device_incident_id,members.entity_id
                """
            ).fetchall()
            device_policy_row = connection.execute(
                "SELECT enabled_epoch FROM notification_policies WHERE name=?",
                (incident_monitor.DEVICE_NOTIFICATION_POLICY,),
            ).fetchone()
            device_notification_enabled_epoch = (
                int(device_policy_row["enabled_epoch"])
                if device_policy_row is not None
                else int(time.time())
            )
            timeline_24h = incident_timeline.collect(
                connection, now=int(time.time()), window_seconds=86_400
            )
            action_counts = {
                "device_recovery": int(connection.execute(
                    "SELECT COUNT(*) FROM recovery_actions"
                ).fetchone()[0]),
                "core_recovery": int(connection.execute(
                    "SELECT COUNT(*) FROM core_recovery_actions"
                ).fetchone()[0]),
                "out_of_band_recovery": int(connection.execute(
                    "SELECT COUNT(*) FROM out_of_band_recovery_actions "
                    "WHERE status='verified'"
                ).fetchone()[0]),
                "notifications": int(connection.execute(
                    "SELECT COUNT(*) FROM incident_notifications WHERE status='accepted'"
                ).fetchone()[0]),
                "ip_change_events": int(connection.execute(
                    "SELECT COUNT(*) FROM network_identity_events WHERE event_type='ip_changed'"
                ).fetchone()[0]),
                "active_ip_changes": int(connection.execute(
                    "SELECT COUNT(*) FROM network_identity_observations WHERE status='ip_changed'"
                ).fetchone()[0]),
                "converged_ip_changes": int(connection.execute(
                    "SELECT COUNT(*) FROM network_identity_events WHERE event_type='converged'"
                ).fetchone()[0]),
                "voice_intents": int(connection.execute(
                    "SELECT COUNT(*) FROM voice_intent_actions WHERE status='completed'"
                ).fetchone()[0]),
                "voice_control_actions": int(connection.execute(
                    "SELECT COUNT(*) FROM voice_intent_actions "
                    "WHERE status='completed' AND action_kind='control'"
                ).fetchone()[0]),
                "automation_failures": int(connection.execute(
                    "SELECT COUNT(*) FROM automation_runs WHERE outcome='failed'"
                ).fetchone()[0]),
                "operational_recovery": int(connection.execute(
                    "SELECT COUNT(*) FROM operational_recovery_attempts "
                    "WHERE status='verified'"
                ).fetchone()[0]),
            }
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise IncidentStatusError("incident database read failed") from error

    incidents = []
    for row in rows:
        subject = _safe_subject(row["subject"], row["kind"])
        status_value = str(row["status"])
        severity = str(row["severity"])
        last_state = str(row["last_state"])
        if status_value not in {"observed", "confirmed", "escalated"}:
            raise IncidentStatusError("incident database is invalid")
        if severity not in {"warning", "critical"}:
            raise IncidentStatusError("incident database is invalid")
        if last_state not in {"unknown", "unavailable", "unreachable", "stale"}:
            raise IncidentStatusError("incident database is invalid")
        incidents.append({
            "incident_id": int(row["id"]),
            "subject": subject,
            "status": status_value,
            "severity": severity,
            "last_state": last_state,
            "baseline": bool(row["baseline"]),
            "first_observed_epoch": int(row["first_observed_epoch"]),
            "last_observed_epoch": int(row["last_observed_epoch"]),
        })
    confirmed = sum(item["status"] in {"confirmed", "escalated"} for item in incidents)
    actionable = sum(
        item["status"] in {"confirmed", "escalated"} and not item["baseline"]
        for item in incidents
    )
    actionable_subjects = {
        str(item["subject"])
        for item in incidents
        if item["status"] in {"confirmed", "escalated"} and not item["baseline"]
    }
    operational_incidents: list[dict[str, object]] = []
    for row in operational_rows:
        display_name = " ".join(str(row["display_name"]).strip().split())
        cause_code = str(row["cause_code"])
        action_code = str(row["action_code"])
        if (
            not display_name
            or len(display_name) > 100
            or str(row["status"]) not in {"confirmed", "escalated"}
            or cause_code not in incident_monitor.CAUSE_CODES
            or action_code not in {
                "light.turn_on", "light.turn_off", "switch.turn_on",
                "switch.turn_off", "service_action", "integration.health",
            }
        ):
            raise IncidentStatusError("incident database is invalid")
        operational_incidents.append({
            "incident_id": int(row["id"]),
            "source_type": str(row["source_type"]),
            "display_name": display_name,
            "status": str(row["status"]),
            "cause_code": cause_code,
            "cause_confidence": str(row["cause_confidence"]),
            "action_code": action_code,
            "occurrences": int(row["occurrences"]),
            "first_observed_epoch": int(row["first_observed_epoch"]),
            "last_observed_epoch": int(row["last_observed_epoch"]),
        })
    members_by_device: dict[int, list[str]] = {}
    for row in device_member_rows:
        device_incident_id = int(row["device_incident_id"])
        subject = _safe_subject(row["entity_id"], "entity")
        members_by_device.setdefault(device_incident_id, []).append(subject)
    device_incidents: list[dict[str, object]] = []
    for row in device_rows:
        display_name = ha_read.sanitize_friendly_name(row["display_name"])
        status_value = str(row["status"])
        cause_code = str(row["cause_code"])
        confidence = str(row["cause_confidence"])
        safety_class = str(row["safety_class"])
        if (
            display_name is None
            or status_value not in {"observed", "confirmed", "escalated"}
            or cause_code not in incident_monitor.CAUSE_CODES
            or confidence not in incident_monitor.CAUSE_CONFIDENCE
            or safety_class not in incident_monitor.SAFETY_CLASSES
        ):
            raise IncidentStatusError("incident database is invalid")
        device_incidents.append({
            "incident_id": int(row["id"]),
            "display_name": display_name,
            "status": status_value,
            "cause_code": cause_code,
            "cause_confidence": confidence,
            "safety_class": safety_class,
            "first_observed_epoch": int(row["first_observed_epoch"]),
            "last_observed_epoch": int(row["last_observed_epoch"]),
            "member_subjects": members_by_device.get(int(row["id"]), []),
        })
    timeline_summary = timeline_24h.get("summary")
    timeline_incidents = timeline_24h.get("incidents")
    if not isinstance(timeline_summary, dict) or not isinstance(timeline_incidents, list):
        raise IncidentStatusError("incident database is invalid")
    return {
        "schema_version": 1,
        "source": "private_incident_ledger",
        "open_count": len(incidents) + len(operational_incidents),
        "confirmed_count": confirmed + len(operational_incidents),
        "actionable_count": actionable + len(operational_incidents),
        "baseline_count": sum(bool(item["baseline"]) for item in incidents),
        "actionable_platforms": _actionable_platforms(
            inventory_path or database_path.parent / ha_inventory.INVENTORY_NAME,
            owner_uid,
            actionable_subjects,
        ),
        "incidents": incidents,
        "device_incidents": device_incidents,
        "device_notification_enabled_epoch": device_notification_enabled_epoch,
        "operational_incidents": operational_incidents,
        "timeline_24h": timeline_24h,
        "completed_actions": action_counts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        document = read_summary()
    except IncidentStatusError:
        print("INCIDENT_STATUS_UNAVAILABLE", file=sys.stderr)
        return 2
    print(json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
