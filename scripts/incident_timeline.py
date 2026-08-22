#!/usr/bin/env python3
"""Build a sanitized 24-hour incident timeline from the private ledger."""

from __future__ import annotations

import re
import sqlite3
from typing import Any


MAX_TIMELINE_INCIDENTS = 512
CAUSE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
ACTION_RE = re.compile(r"^[a-z0-9_.]{1,64}$")


class TimelineError(RuntimeError):
    """Fixed, secret-free timeline failure."""


def _safe_name(value: Any) -> str:
    if not isinstance(value, str):
        raise TimelineError("incident timeline is invalid")
    normalized = " ".join(value.strip().split())
    if (
        not 1 <= len(normalized) <= 100
        or any(ord(character) < 32 for character in normalized)
    ):
        raise TimelineError("incident timeline is invalid")
    return normalized


def collect(
    connection: sqlite3.Connection,
    *,
    now: int,
    window_seconds: int = 86_400,
) -> dict[str, object]:
    if now < 0 or not 60 <= window_seconds <= 7 * 86_400:
        raise TimelineError("invalid incident timeline window")
    since = now - window_seconds
    incidents: list[dict[str, object]] = []
    device_rows = connection.execute(
        """
        SELECT d.*,
               EXISTS(
                   SELECT 1 FROM device_incident_members AS dm
                   JOIN recovery_actions AS r ON r.incident_id=dm.entity_incident_id
                   WHERE dm.device_incident_id=d.id AND r.status='verified'
               ) AS agent_recovered,
               (SELECT COUNT(*) FROM device_incident_members AS dm
                JOIN recovery_actions AS r ON r.incident_id=dm.entity_incident_id
                WHERE dm.device_incident_id=d.id) AS recovery_attempts,
               (SELECT COALESCE(SUM(r.verification_checks),0)
                FROM device_incident_members AS dm
                JOIN recovery_actions AS r ON r.incident_id=dm.entity_incident_id
                WHERE dm.device_incident_id=d.id) AS verification_checks,
               COALESCE((
                   SELECT r.action FROM device_incident_members AS dm
                   JOIN recovery_actions AS r ON r.incident_id=dm.entity_incident_id
                   WHERE dm.device_incident_id=d.id
                   ORDER BY r.attempted_epoch DESC LIMIT 1
               ),'none') AS recovery_action_code,
               (
                   EXISTS(
                       SELECT 1 FROM device_incident_notifications AS dn
                       WHERE dn.device_incident_id=d.id AND dn.status='accepted'
                   )
                   OR EXISTS(
                       SELECT 1 FROM device_incident_members AS dm
                       JOIN incident_notifications AS n
                         ON n.incident_id=dm.entity_incident_id
                       WHERE dm.device_incident_id=d.id AND n.status='accepted'
                   )
               ) AS announced,
               COALESCE((
                   SELECT em.physical_device_hash
                   FROM device_incident_members AS dm
                   JOIN entity_device_map AS em ON em.entity_id=dm.entity_id
                   WHERE dm.device_incident_id=d.id
                   ORDER BY em.physical_device_hash LIMIT 1
               ),d.physical_device_hash) AS canonical_physical_hash
        FROM device_incidents AS d
        WHERE d.first_observed_epoch<=?
          AND COALESCE(d.resolved_epoch,?)>=?
        ORDER BY d.first_observed_epoch
        LIMIT ?
        """,
        (now, now, since, MAX_TIMELINE_INCIDENTS),
    ).fetchall()
    for row in _merge_device_rows(device_rows, now=now):
        incidents.append(_render_row(
            kind="device_outage",
            display_name=row["display_name"],
            status=row["status"],
            cause_code=row["cause_code"],
            first_epoch=row["first_observed_epoch"],
            resolved_epoch=row["resolved_epoch"],
            occurrences=1,
            agent_recovered=bool(row["agent_recovered"]),
            action_code="availability_check",
            recovery_action_code=row["recovery_action_code"],
            recovery_attempts=row["recovery_attempts"],
            verification_checks=row["verification_checks"],
            announced=bool(row["announced"]),
            since=since,
            now=now,
        ))
    remaining = MAX_TIMELINE_INCIDENTS - len(incidents)
    operational_rows = connection.execute(
        """
        SELECT o.*,
               EXISTS(
                   SELECT 1 FROM operational_recovery_attempts AS a
                   WHERE a.operational_incident_id=o.id AND a.status='verified'
               ) AS agent_recovered,
               COALESCE((
                   SELECT a.action_code FROM automation_runs AS a
                   WHERE a.automation_entity_id=o.automation_entity_id
                     AND (
                       a.physical_device_hash=o.physical_device_hash
                       OR (a.physical_device_hash IS NULL
                           AND o.physical_device_hash IS NULL)
                     )
                   ORDER BY a.observed_epoch DESC LIMIT 1
               ),'service_action') AS action_code,
               (SELECT COUNT(*) FROM operational_recovery_attempts AS a
                WHERE a.operational_incident_id=o.id) AS recovery_attempts,
               (SELECT COALESCE(SUM(a.verification_checks),0)
                FROM operational_recovery_attempts AS a
                WHERE a.operational_incident_id=o.id) AS verification_checks,
               COALESCE((
                   SELECT a.candidate_id FROM operational_recovery_attempts AS a
                   WHERE a.operational_incident_id=o.id
                   ORDER BY a.attempted_epoch DESC,a.id DESC LIMIT 1
               ),'none') AS recovery_action_code,
               EXISTS(
                   SELECT 1 FROM operational_incident_notifications AS n
                   WHERE n.operational_incident_id=o.id AND n.status='accepted'
               ) AS announced
        FROM operational_incidents AS o
        WHERE o.first_observed_epoch<=?
          AND COALESCE(o.resolved_epoch,?)>=?
        ORDER BY o.first_observed_epoch
        LIMIT ?
        """,
        (now, now, since, max(0, remaining)),
    ).fetchall()
    for row in operational_rows:
        source_kind = {
            "automation": "automation_failure",
            "integration": "integration_failure",
            "service_call": "service_failure",
            "system_log": "service_failure",
        }.get(str(row["source_type"]))
        if source_kind is None:
            raise TimelineError("incident timeline is invalid")
        incidents.append(_render_row(
            kind=source_kind,
            display_name=row["display_name"],
            status=row["status"],
            cause_code=row["cause_code"],
            first_epoch=row["first_observed_epoch"],
            resolved_epoch=row["resolved_epoch"],
            occurrences=row["occurrences"],
            agent_recovered=bool(row["agent_recovered"]),
            action_code=row["action_code"],
            recovery_action_code=row["recovery_action_code"],
            recovery_attempts=row["recovery_attempts"],
            verification_checks=row["verification_checks"],
            announced=bool(row["announced"]),
            since=since,
            now=now,
        ))
    remaining = MAX_TIMELINE_INCIDENTS - len(incidents)
    system_rows = connection.execute(
        """
        SELECT i.*,
               EXISTS(
                   SELECT 1 FROM core_recovery_actions AS c
                   WHERE c.incident_id=i.id AND c.status='verified'
                   UNION ALL
                   SELECT 1 FROM out_of_band_recovery_actions AS o
                   WHERE o.incident_id=i.id AND o.status='verified'
               ) AS agent_recovered,
               ((SELECT COUNT(*) FROM core_recovery_actions AS c
                 WHERE c.incident_id=i.id)
                +(SELECT COUNT(*) FROM out_of_band_recovery_actions AS o
                  WHERE o.incident_id=i.id)) AS recovery_attempts,
               CASE
                 WHEN EXISTS(SELECT 1 FROM out_of_band_recovery_actions AS o
                             WHERE o.incident_id=i.id)
                   THEN 'out_of_band_restart'
                 WHEN EXISTS(SELECT 1 FROM core_recovery_actions AS c
                             WHERE c.incident_id=i.id)
                   THEN 'homeassistant.restart'
                 ELSE 'none'
               END AS recovery_action_code,
               EXISTS(
                   SELECT 1 FROM incident_notifications AS n
                   WHERE n.incident_id=i.id AND n.status='accepted'
               ) AS announced
        FROM incidents AS i
        WHERE i.kind='system' AND i.subject='home_assistant.core'
          AND i.baseline=0 AND i.first_observed_epoch<=?
          AND COALESCE(i.resolved_epoch,?)>=?
        ORDER BY i.first_observed_epoch
        LIMIT ?
        """,
        (now, now, since, max(0, remaining)),
    ).fetchall()
    for row in system_rows:
        incidents.append(_render_row(
            kind="home_assistant_outage",
            display_name="Home Assistant",
            status=row["status"],
            cause_code="home_assistant_unreachable",
            first_epoch=row["first_observed_epoch"],
            resolved_epoch=row["resolved_epoch"],
            occurrences=row["occurrences"],
            agent_recovered=bool(row["agent_recovered"]),
            action_code="availability_check",
            recovery_action_code=row["recovery_action_code"],
            recovery_attempts=row["recovery_attempts"],
            verification_checks=0,
            announced=bool(row["announced"]),
            since=since,
            now=now,
        ))
    incidents.sort(key=lambda item: (int(item["started_epoch"]), str(item["kind"])))
    summary = {
        "total_incidents": len(incidents),
        "device_outages": sum(item["kind"] == "device_outage" for item in incidents),
        "automation_failures": sum(item["kind"] == "automation_failure" for item in incidents),
        "integration_failures": sum(item["kind"] == "integration_failure" for item in incidents),
        "service_failures": sum(item["kind"] == "service_failure" for item in incidents),
        "home_assistant_outages": sum(
            item["kind"] == "home_assistant_outage" for item in incidents
        ),
        "resolved": sum(item["status"] == "resolved" for item in incidents),
        "unresolved": sum(item["status"] != "resolved" for item in incidents),
        "agent_recovered": sum(item["recovery_mode"] == "agent" for item in incidents),
        "self_recovered": sum(item["recovery_mode"] == "self" for item in incidents),
        "downtime_seconds": sum(int(item["duration_seconds"]) for item in incidents),
        "recovery_attempts": sum(int(item["recovery_attempts"]) for item in incidents),
        "verification_checks": sum(
            int(item["verification_checks"]) for item in incidents
        ),
    }
    return {
        "schema_version": 1,
        "window_start_epoch": since,
        "window_end_epoch": now,
        "summary": summary,
        "incidents": incidents,
    }


def _merge_device_rows(
    rows: list[sqlite3.Row], *, now: int
) -> list[dict[str, object]]:
    """Merge historical per-entity rows after a physical identity is learned."""
    events: list[dict[str, object]] = []
    for row in rows:
        events.append({
            "canonical_physical_hash": str(row["canonical_physical_hash"]),
            "display_name": row["display_name"],
            "status": row["status"],
            "cause_code": row["cause_code"],
            "first_observed_epoch": row["first_observed_epoch"],
            "resolved_epoch": row["resolved_epoch"],
            "occurrences": 1,
            "agent_recovered": bool(row["agent_recovered"]),
            "recovery_action_code": row["recovery_action_code"],
            "recovery_attempts": row["recovery_attempts"],
            "verification_checks": row["verification_checks"],
            "announced": bool(row["announced"]),
        })
    events.sort(key=lambda item: (
        str(item["canonical_physical_hash"]),
        int(item["first_observed_epoch"]),
    ))
    merged: list[dict[str, object]] = []
    for event in events:
        if not merged:
            merged.append(event)
            continue
        previous = merged[-1]
        previous_end = (
            int(previous["resolved_epoch"])
            if previous["resolved_epoch"] is not None else now
        )
        if (
            previous["canonical_physical_hash"]
            != event["canonical_physical_hash"]
            or int(event["first_observed_epoch"]) > previous_end + 180
        ):
            merged.append(event)
            continue
        previous["first_observed_epoch"] = min(
            int(previous["first_observed_epoch"]),
            int(event["first_observed_epoch"]),
        )
        if previous["resolved_epoch"] is None or event["resolved_epoch"] is None:
            previous["resolved_epoch"] = None
            open_statuses = [
                str(value)
                for value in (previous["status"], event["status"])
                if value != "resolved"
            ]
            previous["status"] = max(
                open_statuses,
                key={"observed": 0, "confirmed": 1, "escalated": 2}.get,
            )
        else:
            previous["resolved_epoch"] = max(
                int(previous["resolved_epoch"]), int(event["resolved_epoch"])
            )
            previous["status"] = "resolved"
        if previous["cause_code"] == "unknown" and event["cause_code"] != "unknown":
            previous["cause_code"] = event["cause_code"]
        if (
            previous["recovery_action_code"] == "none"
            and event["recovery_action_code"] != "none"
        ):
            previous["recovery_action_code"] = event["recovery_action_code"]
        previous["agent_recovered"] = bool(
            previous["agent_recovered"] or event["agent_recovered"]
        )
        previous["recovery_attempts"] = (
            int(previous["recovery_attempts"])
            + int(event["recovery_attempts"])
        )
        previous["verification_checks"] = (
            int(previous["verification_checks"])
            + int(event["verification_checks"])
        )
        previous["announced"] = bool(previous["announced"] or event["announced"])
    return merged


def _render_row(
    *,
    kind: str,
    display_name: Any,
    status: Any,
    cause_code: Any,
    first_epoch: Any,
    resolved_epoch: Any,
    occurrences: Any,
    agent_recovered: bool,
    action_code: Any,
    recovery_action_code: Any,
    recovery_attempts: Any,
    verification_checks: Any,
    announced: Any,
    since: int,
    now: int,
) -> dict[str, object]:
    if (
        kind not in {
            "device_outage", "automation_failure", "integration_failure",
            "service_failure", "home_assistant_outage"
        }
        or status not in {"observed", "confirmed", "escalated", "resolved"}
        or not isinstance(cause_code, str)
        or CAUSE_RE.fullmatch(cause_code) is None
        or not isinstance(first_epoch, int)
        or first_epoch < 0
        or resolved_epoch is not None
        and (not isinstance(resolved_epoch, int) or resolved_epoch < first_epoch)
        or not isinstance(occurrences, int)
        or occurrences < 1
        or not isinstance(action_code, str)
        or ACTION_RE.fullmatch(action_code) is None
        or not isinstance(recovery_action_code, str)
        or ACTION_RE.fullmatch(recovery_action_code) is None
        or not isinstance(recovery_attempts, int)
        or recovery_attempts < 0
        or not isinstance(verification_checks, int)
        or verification_checks < 0
        or not isinstance(announced, bool)
    ):
        raise TimelineError("incident timeline is invalid")
    end = min(now, resolved_epoch if resolved_epoch is not None else now)
    start = max(since, first_epoch)
    recovery_mode = (
        "unresolved" if status != "resolved"
        else "agent" if agent_recovered else "self"
    )
    return {
        "kind": kind,
        "display_name": _safe_name(display_name),
        "status": str(status),
        "cause_code": cause_code,
        "action_code": action_code,
        "recovery_action_code": recovery_action_code,
        "recovery_attempts": recovery_attempts,
        "verification_checks": verification_checks,
        "announced": announced,
        "started_epoch": first_epoch,
        "resolved_epoch": resolved_epoch,
        "duration_seconds": max(0, end - start),
        "occurrences": occurrences,
        "recovery_mode": recovery_mode,
    }
