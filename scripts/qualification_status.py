#!/usr/bin/env python3
"""Build a sanitized proof checklist for real Home Butler qualification tests."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Any


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_read as ha_read  # noqa: E402
import dialogue_qualification  # noqa: E402
import incident_monitor  # noqa: E402
import incident_status  # noqa: E402
import startup_self_check  # noqa: E402


WINDOWS_PROOF_PATH = Path(
    "/mnt/h/WSL/Ubuntu/windows-runtime/startup-proof-history.json"
)
MAX_WINDOWS_PROOF_BYTES = 1_048_576
MAX_ACCEPTED_ALERT_SECONDS = 60
MIN_ACCEPTED_ALERT_SECONDS = 15
REBOOT_TARGET = 3
WSL_BOOT_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
TARGETS: tuple[tuple[str, str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "motion_sensor",
        "Датчик движения",
        (
            re.compile(r"(?:24g.*presence|presence.*sensor|datchik.*dvizhen|датчик.*движ)", re.I),
        ),
    ),
    (
        "mirror",
        "Зеркало",
        (re.compile(r"(?:zerkalo|зеркал)", re.I),),
    ),
    (
        "humidifier",
        "Увлажнитель",
        (re.compile(r"(?:humidifier|uvlazhn|увлажн)", re.I),),
    ),
)


class QualificationError(RuntimeError):
    """A fixed, secret-free qualification status failure."""


def read_reboot_count(path: Path = WINDOWS_PROOF_PATH) -> int:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > MAX_WINDOWS_PROOF_BYTES
        ):
            raise QualificationError("Windows proof history is unsafe")
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise QualificationError("Windows proof history is unavailable") from error
    if not isinstance(document, dict) or set(document) != {
        "schema_version", "baseline_boot_id", "verified_reboot_count", "entries"
    }:
        raise QualificationError("Windows proof history is invalid")
    entries = document.get("entries")
    count = document.get("verified_reboot_count")
    if (
        document.get("schema_version") != 2
        or not isinstance(entries, list)
        or not 1 <= len(entries) <= 20
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count != len(entries) - 1
        or count < 0
    ):
        raise QualificationError("Windows proof history is invalid")
    seen_windows_boots: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "windows_boot_id", "wsl_boot_id", "verified_at", "accelerator",
            "startup_self_check_ready", "alice_public_ready",
            "dialogue_qualification_ready",
        }:
            raise QualificationError("Windows proof history is invalid")
        windows_boot_id = entry.get("windows_boot_id")
        wsl_boot_id = entry.get("wsl_boot_id")
        if (
            not isinstance(windows_boot_id, str)
            or not 10 <= len(windows_boot_id) <= 64
            or windows_boot_id in seen_windows_boots
            or not isinstance(wsl_boot_id, str)
            or WSL_BOOT_ID_RE.fullmatch(wsl_boot_id) is None
            or entry.get("accelerator") not in {"gpu", "cpu_fallback"}
            or entry.get("startup_self_check_ready") is not True
            or entry.get("alice_public_ready") is not True
            or entry.get("dialogue_qualification_ready") is not True
        ):
            raise QualificationError("Windows proof history is invalid")
        seen_windows_boots.add(windows_boot_id)
    return count


def _target_for(text: str) -> tuple[str, str] | None:
    for key, label, patterns in TARGETS:
        if any(pattern.search(text) for pattern in patterns):
            return key, label
    return None


def _device_proofs(
    database_path: Path,
    *,
    expected_uid: int,
) -> dict[str, dict[str, Any]]:
    incident_status._validate_path(database_path, expected_uid)
    try:
        connection = sqlite3.connect(
            f"file:{database_path}?mode=ro", uri=True, timeout=3
        )
        connection.row_factory = sqlite3.Row
        try:
            policy = connection.execute(
                "SELECT enabled_epoch FROM notification_policies WHERE name=?",
                (incident_monitor.DEVICE_NOTIFICATION_POLICY,),
            ).fetchone()
            if policy is None:
                raise QualificationError("device notification policy is unavailable")
            rows = connection.execute(
                """
                SELECT d.id,d.display_name,d.status,d.first_observed_epoch,
                       d.confirmed_epoch,d.resolved_epoch,
                       GROUP_CONCAT(m.entity_id,' ') AS entity_ids,
                       COUNT(m.entity_id) AS member_count
                FROM device_incidents AS d
                LEFT JOIN device_incident_members AS m
                  ON m.device_incident_id=d.id
                WHERE d.baseline=0 AND d.first_observed_epoch>=?
                GROUP BY d.id
                ORDER BY d.id DESC
                """,
                (int(policy["enabled_epoch"]),),
            ).fetchall()
            notifications = connection.execute(
                """
                SELECT device_incident_id,phase,status,attempts,accepted_epoch
                FROM device_incident_notifications
                WHERE device_incident_id IN (
                    SELECT id FROM device_incidents WHERE first_observed_epoch>=?
                )
                """,
                (int(policy["enabled_epoch"]),),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise QualificationError("qualification ledger is unavailable") from error

    notices: dict[int, dict[str, sqlite3.Row]] = {}
    for row in notifications:
        notices.setdefault(int(row["device_incident_id"]), {})[
            str(row["phase"])
        ] = row
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        display_name = ha_read.sanitize_friendly_name(row["display_name"])
        entity_ids = str(row["entity_ids"] or "")
        if display_name is None:
            continue
        target = _target_for(f"{display_name} {entity_ids}")
        if target is None or target[0] in result:
            continue
        key, label = target
        incident_notices = notices.get(int(row["id"]), {})
        confirmed_notice = incident_notices.get("confirmed")
        resolved_notice = incident_notices.get("resolved")
        confirmed_epoch = (
            int(row["confirmed_epoch"])
            if row["confirmed_epoch"] is not None else None
        )
        resolved_epoch = (
            int(row["resolved_epoch"])
            if row["resolved_epoch"] is not None else None
        )
        confirmed_accepted = (
            confirmed_notice is not None
            and confirmed_notice["status"] == "accepted"
            and confirmed_notice["accepted_epoch"] is not None
            and confirmed_epoch is not None
            and int(confirmed_notice["accepted_epoch"]) >= confirmed_epoch
        )
        resolved_accepted = (
            resolved_notice is not None
            and resolved_notice["status"] == "accepted"
            and resolved_notice["accepted_epoch"] is not None
            and confirmed_accepted
            and resolved_epoch is not None
            and int(resolved_notice["accepted_epoch"]) >= resolved_epoch
        )
        alert_seconds = (
            int(confirmed_notice["accepted_epoch"]) - int(row["first_observed_epoch"])
            if confirmed_accepted
            else None
        )
        latency_ok = (
            isinstance(alert_seconds, int)
            and MIN_ACCEPTED_ALERT_SECONDS
            <= alert_seconds
            <= MAX_ACCEPTED_ALERT_SECONDS
        )
        if confirmed_notice is not None and confirmed_notice["status"] in {
            "abandoned", "delivery_unknown"
        }:
            state = "delivery_problem"
        elif confirmed_accepted and resolved_accepted and latency_ok:
            state = "passed"
        elif confirmed_accepted and row["status"] == "resolved":
            state = "waiting_recovery_notice"
        elif confirmed_accepted:
            state = "waiting_recovery"
        elif row["confirmed_epoch"] is not None:
            state = "waiting_alert"
        else:
            state = "confirming"
        result[key] = {
            "key": key,
            "label": label,
            "state": state,
            "member_count": int(row["member_count"]),
            "one_outage_notice": bool(confirmed_accepted),
            "one_recovery_notice": bool(resolved_accepted),
            "alert_seconds": alert_seconds,
            "latency_ok": latency_ok,
        }
    for key, label, _patterns in TARGETS:
        result.setdefault(key, {
            "key": key,
            "label": label,
            "state": "pending",
            "member_count": 0,
            "one_outage_notice": False,
            "one_recovery_notice": False,
            "alert_seconds": None,
            "latency_ok": False,
        })
    return result


def _dialogue_proof(
    *,
    state_dir: Path | None = None,
    current_boot_id: str | None = None,
) -> dict[str, Any]:
    pending = {
        "state": "pending",
        "local_chat_ready": False,
        "alice_public_ready": False,
        "history_verified": False,
        "free_dialogue_verified": False,
        "fake_tool_claim_absent": False,
    }
    try:
        boot_id = current_boot_id or startup_self_check.read_boot_id()
        document = dialogue_qualification.read_status(
            state_dir, current_boot_id=boot_id
        )
    except (
        dialogue_qualification.DialogueQualificationError,
        startup_self_check.SelfCheckError,
    ):
        return pending
    return {
        "state": "passed",
        "local_chat_ready": bool(document["local_chat_ready"]),
        "alice_public_ready": bool(document["alice_public_ready"]),
        "history_verified": bool(document["history_verified"]),
        "free_dialogue_verified": bool(document["free_dialogue_verified"]),
        "fake_tool_claim_absent": bool(document["fake_tool_claim_absent"]),
    }


def read_status(
    *,
    reboot_path: Path = WINDOWS_PROOF_PATH,
    database_path: Path | None = None,
    expected_uid: int | None = None,
    dialogue_state_dir: Path | None = None,
    current_boot_id: str | None = None,
) -> dict[str, Any]:
    path = database_path or (
        Path(os.environ.get(
            "HOME_BUTLER_INCIDENT_STATE_DIR",
            "/home/homebutler/.local/state/home-butler/incidents",
        ))
        / incident_monitor.DATABASE_NAME
    )
    try:
        reboot_count = read_reboot_count(reboot_path)
    except QualificationError:
        reboot_count = 0
    owner_uid = incident_status._expected_uid() if expected_uid is None else expected_uid
    devices = _device_proofs(path, expected_uid=owner_uid)
    ordered_devices = [devices[key] for key, _label, _patterns in TARGETS]
    dialogue = _dialogue_proof(
        state_dir=dialogue_state_dir,
        current_boot_id=current_boot_id,
    )
    hardware_complete = (
        reboot_count >= REBOOT_TARGET
        and all(item["state"] == "passed" for item in ordered_devices)
    )
    dialogue_complete = dialogue["state"] == "passed"
    return {
        "schema_version": 2,
        "verified_reboots": reboot_count,
        "required_reboots": REBOOT_TARGET,
        "reboots_passed": reboot_count >= REBOOT_TARGET,
        "devices": ordered_devices,
        "hardware_proof_complete": hardware_complete,
        "dialogue": dialogue,
        "dialogue_proof_complete": dialogue_complete,
        "qualification_complete": hardware_complete and dialogue_complete,
    }


def main() -> int:
    try:
        document = read_status()
    except (QualificationError, incident_status.IncidentStatusError):
        print("QUALIFICATION_STATUS_UNAVAILABLE", file=sys.stderr)
        return 2
    print(json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
