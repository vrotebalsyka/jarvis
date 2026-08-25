#!/usr/bin/env python3
"""Monitor Home Assistant state changes and persist monitor-only incidents."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import signal
import sqlite3
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_read as ha_read  # noqa: E402

try:
    import websocket  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - deployment preflight handles this
    websocket = None


DEFAULT_STATE_DIR = Path("/home/homebutler/.local/state/home-butler/incidents")
DATABASE_NAME = "incidents.sqlite3"
CONFIRM_AFTER_SECONDS = 20
DEVICE_NOTIFICATION_AFTER_SECONDS = 20
SENSOR_NOTIFICATION_AFTER_SECONDS = 120
SENSOR_NOTIFICATION_POLICY = "sensor_warning_tts"
DEVICE_NOTIFICATION_POLICY = "universal_device_warning_tts_v2"
SOCKET_TIMEOUT_SECONDS = 5
MAX_RECONNECT_SECONDS = 30
MAX_MESSAGE_BYTES = 4 * 1_048_576
BAD_ENTITY_STATES = {"unknown", "unavailable"}
SAFE_INCIDENT_STATES = BAD_ENTITY_STATES | {"stale", "reachable", "unreachable"}
RESERVED_SUBJECT = "home_assistant.core"
CAUSE_CODES = {
    "unknown",
    "device_not_observed_on_lan",
    "confirmed_ip_change",
    "tuya_integration_unavailable",
    "yandex_cloud_unreachable",
    "dns_resolution_failed",
    "upstream_timeout",
    "tls_failure",
    "integration_not_loaded",
    "integration_unavailable",
    "command_not_confirmed",
    "automation_action_failed",
    "home_assistant_unreachable",
    "stale_entity_data",
    "partial_entity_unavailable",
}
CAUSE_CONFIDENCE = {"unknown", "probable", "confirmed"}
SAFETY_CLASSES = {
    "sensor",
    "light",
    "ordinary_relay",
    "restricted",
    "unknown",
}
STOP_EVENT = threading.Event()


class MonitorError(RuntimeError):
    """Secret-free incident monitor failure."""


def _migrate_network_identity_schema(connection: sqlite3.Connection) -> None:
    """Atomically extend the private network ledger to Xiaomi identities."""
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='network_identity_observations'"
    ).fetchone()
    schema = row[0] if row is not None else None
    if not isinstance(schema, str) or "xiaomi_miot" in schema:
        return
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            ALTER TABLE network_identity_events
                RENAME TO network_identity_events_before_xiaomi;
            ALTER TABLE network_identity_observations
                RENAME TO network_identity_observations_before_xiaomi;
            CREATE TABLE network_identity_observations (
                identity_hash TEXT PRIMARY KEY,
                platform TEXT NOT NULL CHECK(platform IN (
                    'localtuya','tuya_local','xiaomi_miot'
                )),
                device_id TEXT NOT NULL,
                config_entry_id TEXT NOT NULL,
                configured_ip TEXT NOT NULL,
                observed_ip TEXT,
                mac TEXT,
                status TEXT NOT NULL CHECK(status IN (
                    'stable','ip_changed','not_observed'
                )),
                first_observed_epoch INTEGER NOT NULL,
                last_observed_epoch INTEGER NOT NULL,
                change_count INTEGER NOT NULL
            );
            INSERT INTO network_identity_observations
            SELECT * FROM network_identity_observations_before_xiaomi;
            CREATE TABLE network_identity_events (
                id INTEGER PRIMARY KEY,
                identity_hash TEXT NOT NULL REFERENCES
                    network_identity_observations(identity_hash),
                event_type TEXT NOT NULL CHECK(event_type IN (
                    'bound','ip_changed','converged','not_observed'
                )),
                observed_epoch INTEGER NOT NULL,
                configured_ip TEXT NOT NULL,
                observed_ip TEXT,
                mac TEXT
            );
            INSERT INTO network_identity_events
            SELECT * FROM network_identity_events_before_xiaomi;
            DROP TABLE network_identity_events_before_xiaomi;
            DROP TABLE network_identity_observations_before_xiaomi;
            COMMIT;
            """
        )
    except sqlite3.Error:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise sqlite3.IntegrityError("network identity migration failed")


def _migrate_operational_schema(connection: sqlite3.Connection) -> None:
    """Expand automation-only incidents to sanitized operational failures."""
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='operational_incidents'"
    ).fetchone()
    schema = row[0] if row is not None else None
    columns = {
        str(item[1])
        for item in connection.execute("PRAGMA table_info(operational_incidents)")
    }
    if (
        not isinstance(schema, str)
        or "source_type='automation'" not in schema
        and {"action_code", "error_code", "target_entity_id"} <= columns
    ):
        return
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE operational_incidents_universal (
                id INTEGER PRIMARY KEY,
                incident_key TEXT NOT NULL,
                source_type TEXT NOT NULL CHECK(source_type IN (
                    'automation','service_call','system_log','integration'
                )),
                automation_entity_id TEXT NOT NULL,
                physical_device_hash TEXT,
                representative_subject TEXT NOT NULL,
                display_name TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'confirmed','resolved','escalated'
                )),
                first_observed_epoch INTEGER NOT NULL,
                last_observed_epoch INTEGER NOT NULL,
                resolved_epoch INTEGER,
                occurrences INTEGER NOT NULL,
                cause_code TEXT NOT NULL,
                cause_confidence TEXT NOT NULL,
                safety_class TEXT NOT NULL,
                action_code TEXT NOT NULL,
                error_code TEXT NOT NULL,
                target_entity_id TEXT,
                evidence_json TEXT NOT NULL
            )
            """
        )
        rows = connection.execute(
            "SELECT * FROM operational_incidents ORDER BY id"
        ).fetchall()
        for current in rows:
            latest = connection.execute(
                """
                SELECT action_code,error_code,target_entity_id
                FROM automation_runs
                WHERE automation_entity_id=?
                  AND (
                    physical_device_hash=?
                    OR (physical_device_hash IS NULL AND ? IS NULL)
                  )
                ORDER BY observed_epoch DESC LIMIT 1
                """,
                (
                    current["automation_entity_id"],
                    current["physical_device_hash"],
                    current["physical_device_hash"],
                ),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO operational_incidents_universal(
                    id,incident_key,source_type,automation_entity_id,
                    physical_device_hash,representative_subject,display_name,
                    status,first_observed_epoch,last_observed_epoch,
                    resolved_epoch,occurrences,cause_code,cause_confidence,
                    safety_class,action_code,error_code,target_entity_id,
                    evidence_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    current["id"], current["incident_key"], "automation",
                    current["automation_entity_id"],
                    current["physical_device_hash"],
                    current["representative_subject"], current["display_name"],
                    current["status"], current["first_observed_epoch"],
                    current["last_observed_epoch"], current["resolved_epoch"],
                    current["occurrences"], current["cause_code"],
                    current["cause_confidence"], current["safety_class"],
                    latest["action_code"] if latest is not None else "service_action",
                    latest["error_code"] if latest is not None else "automation_action_failed",
                    latest["target_entity_id"] if latest is not None else None,
                    current["evidence_json"],
                ),
            )
        connection.execute("DROP TABLE operational_incidents")
        connection.execute(
            "ALTER TABLE operational_incidents_universal "
            "RENAME TO operational_incidents"
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX one_open_operational_incident
            ON operational_incidents(incident_key)
            WHERE status IN ('confirmed','escalated')
            """
        )
        connection.execute("COMMIT")
    except sqlite3.Error:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise sqlite3.IntegrityError("operational incident migration failed")


def _state_dir() -> Path:
    raw = os.environ.get("HOME_BUTLER_INCIDENT_STATE_DIR", "")
    return Path(raw) if raw else DEFAULT_STATE_DIR


def _validate_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise MonitorError("incident state directory is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise MonitorError("incident state directory is unsafe")


def _validate_database(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise MonitorError("incident database is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise MonitorError("incident database is unsafe")


def _validate_subject(subject: str, kind: str) -> str:
    if subject == RESERVED_SUBJECT and kind == "system":
        return subject
    try:
        return ha_read._validate_entity_id(subject)
    except ha_read.AdapterError as error:
        raise MonitorError("invalid incident subject") from error


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def emit(event: str, **fields: object) -> None:
    print(_json({"event": event, "mode": "monitor_only", **fields}), flush=True)


DEVICE_FIELD_SUFFIXES = (
    "closest_target_distance",
    "near_detection",
    "far_detection",
    "sensitivity",
    "temperature",
    "humidity",
    "occupancy",
    "dvizhenie",
    "batareia",
    "battery",
    "signal_strength",
)


def _device_display_name(subjects: list[str]) -> str:
    """Derive one speech-safe device label without storing HA attributes."""
    objects: list[str] = []
    for subject in sorted(set(subjects)):
        try:
            normalized = ha_read._validate_entity_id(subject)
        except ha_read.AdapterError as error:
            raise MonitorError("invalid device incident member") from error
        object_id = normalized.split(".", 1)[1]
        for suffix in DEVICE_FIELD_SUFFIXES:
            marker = f"_{suffix}"
            if object_id.endswith(marker) and len(object_id) > len(marker):
                object_id = object_id[: -len(marker)]
                break
        objects.append(object_id)
    if not objects:
        raise MonitorError("device incident has no members")
    common = os.path.commonprefix(objects).rstrip("_")
    selected = common if len(common) >= 3 else min(objects, key=len)
    rendered = " ".join(selected.replace("_", " ").split())[:100]
    if not rendered or any(ord(character) < 32 for character in rendered):
        raise MonitorError("invalid device display name")
    return rendered


def _device_safety_class(subjects: list[str]) -> str:
    domains = {subject.split(".", 1)[0] for subject in subjects}
    if domains & {"lock", "climate", "water_heater", "valve", "alarm_control_panel"}:
        return "restricted"
    if "light" in domains:
        return "light"
    if "switch" in domains:
        return "ordinary_relay"
    if domains and domains <= {"sensor", "binary_sensor", "number", "select"}:
        return "sensor"
    return "unknown"


class IncidentStore:
    """Durable incident state; deliberately contains no action executor."""

    def __init__(self, path: Path) -> None:
        _validate_directory(path.parent)
        _validate_database(path)
        self.path = path
        try:
            self.connection = sqlite3.connect(path, timeout=5)
            os.chmod(path, 0o600)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                    id INTEGER PRIMARY KEY,
                    subject TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('entity','system')),
                    status TEXT NOT NULL CHECK(status IN ('observed','confirmed','resolved','escalated')),
                    first_observed_epoch INTEGER NOT NULL,
                    last_observed_epoch INTEGER NOT NULL,
                    confirmed_epoch INTEGER,
                    resolved_epoch INTEGER,
                    occurrences INTEGER NOT NULL,
                    last_state TEXT NOT NULL,
                    severity TEXT NOT NULL CHECK(severity IN ('warning','critical')),
                    action_mode TEXT NOT NULL CHECK(action_mode = 'monitor_only'),
                    actions_attempted INTEGER NOT NULL CHECK(actions_attempted = 0),
                    evidence_json TEXT NOT NULL,
                    baseline INTEGER NOT NULL CHECK(baseline IN (0,1)) DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_open_incident_per_subject
                    ON incidents(subject)
                    WHERE status IN ('observed','confirmed','escalated');
                CREATE TABLE IF NOT EXISTS incident_events (
                    id INTEGER PRIMARY KEY,
                    incident_id INTEGER NOT NULL REFERENCES incidents(id),
                    event_type TEXT NOT NULL,
                    observed_epoch INTEGER NOT NULL,
                    evidence_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS incident_notifications (
                    id INTEGER PRIMARY KEY,
                    incident_id INTEGER NOT NULL REFERENCES incidents(id),
                    phase TEXT NOT NULL CHECK(phase IN ('confirmed','resolved')),
                    status TEXT NOT NULL CHECK(status IN ('failed','accepted','abandoned')),
                    attempts INTEGER NOT NULL,
                    last_attempt_epoch INTEGER NOT NULL,
                    accepted_epoch INTEGER,
                    speaker_entity_id TEXT,
                    UNIQUE(incident_id, phase)
                );
                CREATE TABLE IF NOT EXISTS notification_policies (
                    name TEXT PRIMARY KEY,
                    enabled_epoch INTEGER NOT NULL CHECK(enabled_epoch >= 0)
                );
                CREATE TABLE IF NOT EXISTS recovery_actions (
                    id INTEGER PRIMARY KEY,
                    incident_id INTEGER NOT NULL REFERENCES incidents(id),
                    action_group_id TEXT NOT NULL,
                    integration TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('failed','accepted','delivery_unknown','verified')),
                    attempted_epoch INTEGER NOT NULL,
                    service_calls INTEGER NOT NULL CHECK(service_calls IN (0,1)),
                    verification_checks INTEGER NOT NULL DEFAULT 0 CHECK(
                        verification_checks BETWEEN 0 AND 3
                    ),
                    before_state TEXT NOT NULL,
                    after_state TEXT NOT NULL,
                    UNIQUE(incident_id, action)
                );
                CREATE TABLE IF NOT EXISTS core_recovery_actions (
                    id INTEGER PRIMARY KEY,
                    incident_id INTEGER NOT NULL REFERENCES incidents(id),
                    action_group_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'check_failed','check_unknown','failed','accepted',
                        'delivery_unknown','verified'
                    )),
                    attempted_epoch INTEGER NOT NULL,
                    check_calls INTEGER NOT NULL CHECK(check_calls IN (0,1)),
                    restart_calls INTEGER NOT NULL CHECK(restart_calls IN (0,1)),
                    after_state TEXT NOT NULL CHECK(after_state IN ('reachable','unknown')),
                    UNIQUE(incident_id)
                );
                CREATE TABLE IF NOT EXISTS out_of_band_recovery_actions (
                    id INTEGER PRIMARY KEY,
                    incident_id INTEGER NOT NULL REFERENCES incidents(id),
                    action_group_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'failed','cooldown','healthy','verified'
                    )),
                    attempted_epoch INTEGER NOT NULL,
                    attempts INTEGER NOT NULL CHECK(attempts BETWEEN 1 AND 3),
                    ssh_calls INTEGER NOT NULL CHECK(ssh_calls IN (0,1)),
                    restart_calls INTEGER NOT NULL CHECK(restart_calls IN (0,1)),
                    after_state TEXT NOT NULL CHECK(after_state IN ('reachable','unknown')),
                    UNIQUE(incident_id)
                );
                CREATE TABLE IF NOT EXISTS network_identity_observations (
                    identity_hash TEXT PRIMARY KEY,
                    platform TEXT NOT NULL CHECK(platform IN (
                        'localtuya','tuya_local','xiaomi_miot'
                    )),
                    device_id TEXT NOT NULL,
                    config_entry_id TEXT NOT NULL,
                    configured_ip TEXT NOT NULL,
                    observed_ip TEXT,
                    mac TEXT,
                    status TEXT NOT NULL CHECK(status IN ('stable','ip_changed','not_observed')),
                    first_observed_epoch INTEGER NOT NULL,
                    last_observed_epoch INTEGER NOT NULL,
                    change_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS network_identity_events (
                    id INTEGER PRIMARY KEY,
                    identity_hash TEXT NOT NULL REFERENCES network_identity_observations(identity_hash),
                    event_type TEXT NOT NULL CHECK(event_type IN ('bound','ip_changed','converged','not_observed')),
                    observed_epoch INTEGER NOT NULL,
                    configured_ip TEXT NOT NULL,
                    observed_ip TEXT,
                    mac TEXT
                );
                CREATE TABLE IF NOT EXISTS device_network_observations (
                    physical_device_hash TEXT PRIMARY KEY,
                    device_ids_json TEXT NOT NULL,
                    config_entry_ids_json TEXT NOT NULL,
                    mac TEXT NOT NULL,
                    observed_ip TEXT,
                    last_known_ip TEXT,
                    status TEXT NOT NULL CHECK(status IN (
                        'stable','ip_changed','not_observed'
                    )),
                    first_observed_epoch INTEGER NOT NULL,
                    last_observed_epoch INTEGER NOT NULL,
                    change_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS device_network_events (
                    id INTEGER PRIMARY KEY,
                    physical_device_hash TEXT NOT NULL REFERENCES
                        device_network_observations(physical_device_hash),
                    event_type TEXT NOT NULL CHECK(event_type IN (
                        'bound','ip_changed','returned','not_observed'
                    )),
                    observed_epoch INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    evidence_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS voice_intent_actions (
                    id TEXT PRIMARY KEY,
                    route_id TEXT NOT NULL,
                    action_kind TEXT NOT NULL CHECK(action_kind IN ('status','incidents','control')),
                    speaker_entity_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('accepted','completed','failed')),
                    attempted_epoch INTEGER NOT NULL,
                    completed_epoch INTEGER,
                    control_service_calls INTEGER NOT NULL CHECK(control_service_calls IN (0,1)),
                    tts_service_calls INTEGER NOT NULL CHECK(tts_service_calls IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS entity_device_map (
                    entity_id TEXT PRIMARY KEY,
                    physical_device_hash TEXT NOT NULL,
                    device_id TEXT,
                    platform TEXT NOT NULL,
                    config_entry_ids_json TEXT NOT NULL,
                    observed_epoch INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS entity_freshness_observations (
                    entity_id TEXT PRIMARY KEY,
                    physical_device_hash TEXT NOT NULL,
                    first_observed_epoch INTEGER NOT NULL,
                    last_source_epoch INTEGER NOT NULL,
                    last_poll_epoch INTEGER NOT NULL,
                    interval_samples INTEGER NOT NULL CHECK(
                        interval_samples BETWEEN 0 AND 1000
                    ),
                    mean_interval_seconds INTEGER NOT NULL CHECK(
                        mean_interval_seconds BETWEEN 0 AND 31536000
                    ),
                    stale_threshold_seconds INTEGER NOT NULL CHECK(
                        stale_threshold_seconds BETWEEN 1800 AND 604800
                    ),
                    stale_active INTEGER NOT NULL CHECK(stale_active IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS device_health_observations (
                    physical_device_hash TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    health_status TEXT NOT NULL CHECK(health_status IN (
                        'healthy','partial','degraded','offline','unknown'
                    )),
                    cause_code TEXT NOT NULL,
                    cause_confidence TEXT NOT NULL,
                    safety_class TEXT NOT NULL,
                    network_status TEXT NOT NULL,
                    entity_count INTEGER NOT NULL,
                    available_entity_count INTEGER NOT NULL,
                    unavailable_entity_count INTEGER NOT NULL,
                    first_observed_epoch INTEGER NOT NULL,
                    last_observed_epoch INTEGER NOT NULL,
                    change_count INTEGER NOT NULL,
                    evidence_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS device_health_events (
                    id INTEGER PRIMARY KEY,
                    physical_device_hash TEXT NOT NULL REFERENCES
                        device_health_observations(physical_device_hash),
                    event_type TEXT NOT NULL CHECK(event_type IN (
                        'observed','changed','recovered'
                    )),
                    observed_epoch INTEGER NOT NULL,
                    health_status TEXT NOT NULL,
                    cause_code TEXT NOT NULL,
                    evidence_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS integration_health_observations (
                    domain TEXT PRIMARY KEY,
                    health_status TEXT NOT NULL CHECK(health_status IN (
                        'healthy','degraded'
                    )),
                    entry_count INTEGER NOT NULL,
                    loaded_entry_count INTEGER NOT NULL,
                    first_observed_epoch INTEGER NOT NULL,
                    last_observed_epoch INTEGER NOT NULL,
                    change_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS integration_health_events (
                    id INTEGER PRIMARY KEY,
                    domain TEXT NOT NULL REFERENCES
                        integration_health_observations(domain),
                    event_type TEXT NOT NULL CHECK(event_type IN (
                        'observed','changed','recovered'
                    )),
                    observed_epoch INTEGER NOT NULL,
                    health_status TEXT NOT NULL,
                    evidence_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS device_incidents (
                    id INTEGER PRIMARY KEY,
                    physical_device_hash TEXT NOT NULL,
                    representative_subject TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'observed','confirmed','resolved','escalated'
                    )),
                    first_observed_epoch INTEGER NOT NULL,
                    last_observed_epoch INTEGER NOT NULL,
                    confirmed_epoch INTEGER,
                    resolved_epoch INTEGER,
                    severity TEXT NOT NULL CHECK(severity IN ('warning','critical')),
                    cause_code TEXT NOT NULL,
                    cause_confidence TEXT NOT NULL,
                    safety_class TEXT NOT NULL,
                    baseline INTEGER NOT NULL CHECK(baseline IN (0,1)),
                    evidence_json TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_open_device_incident
                    ON device_incidents(physical_device_hash)
                    WHERE status IN ('observed','confirmed','escalated');
                CREATE TABLE IF NOT EXISTS device_incident_members (
                    device_incident_id INTEGER NOT NULL REFERENCES device_incidents(id),
                    entity_incident_id INTEGER NOT NULL UNIQUE REFERENCES incidents(id),
                    entity_id TEXT NOT NULL,
                    PRIMARY KEY(device_incident_id,entity_incident_id)
                );
                CREATE TABLE IF NOT EXISTS device_incident_notifications (
                    id INTEGER PRIMARY KEY,
                    device_incident_id INTEGER NOT NULL REFERENCES device_incidents(id),
                    phase TEXT NOT NULL CHECK(phase IN ('confirmed','resolved')),
                    status TEXT NOT NULL CHECK(status IN (
                        'failed','accepted','abandoned','delivery_unknown'
                    )),
                    attempts INTEGER NOT NULL,
                    last_attempt_epoch INTEGER NOT NULL,
                    accepted_epoch INTEGER,
                    speaker_entity_id TEXT,
                    UNIQUE(device_incident_id,phase)
                );
                CREATE TABLE IF NOT EXISTS automation_runs (
                    run_hash TEXT PRIMARY KEY,
                    automation_entity_id TEXT NOT NULL,
                    automation_item_hash TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK(outcome IN ('failed','succeeded')),
                    started_epoch INTEGER NOT NULL,
                    observed_epoch INTEGER NOT NULL,
                    error_code TEXT NOT NULL,
                    cause_code TEXT NOT NULL,
                    cause_confidence TEXT NOT NULL,
                    action_code TEXT NOT NULL,
                    target_entity_id TEXT,
                    physical_device_hash TEXT,
                    evidence_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS automation_runs_by_time
                    ON automation_runs(observed_epoch);
                CREATE TABLE IF NOT EXISTS operational_incidents (
                    id INTEGER PRIMARY KEY,
                    incident_key TEXT NOT NULL,
                    source_type TEXT NOT NULL CHECK(source_type IN (
                        'automation','service_call','system_log','integration'
                    )),
                    automation_entity_id TEXT NOT NULL,
                    physical_device_hash TEXT,
                    representative_subject TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'confirmed','resolved','escalated'
                    )),
                    first_observed_epoch INTEGER NOT NULL,
                    last_observed_epoch INTEGER NOT NULL,
                    resolved_epoch INTEGER,
                    occurrences INTEGER NOT NULL,
                    cause_code TEXT NOT NULL,
                    cause_confidence TEXT NOT NULL,
                    safety_class TEXT NOT NULL,
                    action_code TEXT NOT NULL,
                    error_code TEXT NOT NULL,
                    target_entity_id TEXT,
                    evidence_json TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_open_operational_incident
                    ON operational_incidents(incident_key)
                    WHERE status IN ('confirmed','escalated');
                CREATE TABLE IF NOT EXISTS operational_incident_events (
                    id INTEGER PRIMARY KEY,
                    operational_incident_id INTEGER NOT NULL REFERENCES
                        operational_incidents(id),
                    event_type TEXT NOT NULL CHECK(event_type IN (
                        'confirmed','repeated','resolved','escalated'
                    )),
                    observed_epoch INTEGER NOT NULL,
                    evidence_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recovery_decisions (
                    decision_id TEXT PRIMARY KEY,
                    operational_incident_id INTEGER NOT NULL REFERENCES
                        operational_incidents(id),
                    selected_candidate_id TEXT NOT NULL,
                    decision_source TEXT NOT NULL CHECK(decision_source IN (
                        'model','verified_fallback'
                    )),
                    fact_ids_json TEXT NOT NULL,
                    decided_epoch INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'pending','executed','rejected','expired'
                    ))
                );
                CREATE TABLE IF NOT EXISTS operational_recovery_attempts (
                    id INTEGER PRIMARY KEY,
                    operational_incident_id INTEGER NOT NULL REFERENCES
                        operational_incidents(id),
                    decision_id TEXT NOT NULL UNIQUE REFERENCES
                        recovery_decisions(decision_id),
                    candidate_id TEXT NOT NULL,
                    attempted_epoch INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'no_action','failed','delivery_unknown','verified',
                        'rejected','cooldown'
                    )),
                    service_calls INTEGER NOT NULL CHECK(service_calls IN (0,1)),
                    verification_checks INTEGER NOT NULL CHECK(
                        verification_checks BETWEEN 0 AND 3
                    ),
                    before_state TEXT NOT NULL,
                    after_state TEXT NOT NULL,
                    next_allowed_epoch INTEGER NOT NULL,
                    evidence_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS operational_attempts_by_incident
                    ON operational_recovery_attempts(
                        operational_incident_id,candidate_id,attempted_epoch
                    );
                CREATE TABLE IF NOT EXISTS operational_incident_notifications (
                    id INTEGER PRIMARY KEY,
                    operational_incident_id INTEGER NOT NULL REFERENCES
                        operational_incidents(id),
                    phase TEXT NOT NULL CHECK(phase IN ('detected','resolved')),
                    status TEXT NOT NULL CHECK(status IN (
                        'claimed','accepted','delivery_unknown'
                    )),
                    claimed_epoch INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_epoch INTEGER,
                    completed_epoch INTEGER,
                    speaker_entity_id TEXT,
                    UNIQUE(operational_incident_id,phase)
                );
                CREATE TABLE IF NOT EXISTS service_call_observations (
                    event_hash TEXT PRIMARY KEY,
                    context_hash TEXT,
                    domain TEXT NOT NULL,
                    service TEXT NOT NULL,
                    entity_ids_json TEXT NOT NULL,
                    observed_epoch INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS service_calls_by_time
                    ON service_call_observations(observed_epoch);
                CREATE TABLE IF NOT EXISTS operational_observations (
                    event_hash TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL CHECK(source_type IN (
                        'service_call','system_log','integration'
                    )),
                    source_ref TEXT NOT NULL,
                    observed_epoch INTEGER NOT NULL,
                    error_code TEXT NOT NULL,
                    cause_code TEXT NOT NULL,
                    cause_confidence TEXT NOT NULL,
                    action_code TEXT NOT NULL,
                    target_entity_id TEXT,
                    physical_device_hash TEXT,
                    evidence_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS diagnostic_cursors (
                    name TEXT PRIMARY KEY,
                    initialized_epoch INTEGER NOT NULL
                );
                """
            )
            _migrate_network_identity_schema(self.connection)
            _migrate_operational_schema(self.connection)
            self.connection.execute(
                "INSERT OR IGNORE INTO notification_policies(name,enabled_epoch) "
                "VALUES(?,?)",
                (SENSOR_NOTIFICATION_POLICY, int(time.time())),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO notification_policies(name,enabled_epoch) "
                "VALUES(?,?)",
                (DEVICE_NOTIFICATION_POLICY, int(time.time())),
            )
            columns = {
                str(row[1])
                for row in self.connection.execute("PRAGMA table_info(incidents)")
            }
            if "baseline" not in columns:
                self.connection.execute(
                    "ALTER TABLE incidents ADD COLUMN baseline INTEGER NOT NULL DEFAULT 0"
                )
                baseline_ids: list[int] = []
                for row in self.connection.execute(
                    "SELECT incident_id,evidence_json FROM incident_events WHERE event_type='observed'"
                ):
                    try:
                        evidence = json.loads(str(row["evidence_json"]))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(evidence, dict) and evidence.get("source") == "startup_snapshot":
                        baseline_ids.append(int(row["incident_id"]))
                self.connection.executemany(
                    "UPDATE incidents SET baseline=1 WHERE id=?",
                    ((incident_id,) for incident_id in baseline_ids),
                )
            recovery_columns = {
                str(row[1])
                for row in self.connection.execute(
                    "PRAGMA table_info(recovery_actions)"
                )
            }
            if "verification_checks" not in recovery_columns:
                self.connection.execute(
                    "ALTER TABLE recovery_actions ADD COLUMN "
                    "verification_checks INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(verification_checks BETWEEN 0 AND 3)"
                )
            operational_notification_columns = {
                str(row[1])
                for row in self.connection.execute(
                    "PRAGMA table_info(operational_incident_notifications)"
                )
            }
            if "attempts" not in operational_notification_columns:
                self.connection.execute(
                    "ALTER TABLE operational_incident_notifications ADD COLUMN "
                    "attempts INTEGER NOT NULL DEFAULT 0"
                )
            if "last_attempt_epoch" not in operational_notification_columns:
                self.connection.execute(
                    "ALTER TABLE operational_incident_notifications ADD COLUMN "
                    "last_attempt_epoch INTEGER"
                )
            self.connection.commit()
        except (OSError, sqlite3.Error) as error:
            raise MonitorError("incident database initialization failed") from error

    def close(self) -> None:
        self.connection.close()

    def replace_entity_device_map(
        self, entities: list[dict[str, object]], observed_epoch: int
    ) -> int:
        """Atomically replace the private entity-to-physical-device map."""
        if observed_epoch < 0 or len(entities) > 4_096:
            raise MonitorError("invalid entity device map")
        rows: list[tuple[object, ...]] = []
        for item in entities:
            if not isinstance(item, dict):
                raise MonitorError("invalid entity device map")
            try:
                entity_id = ha_read._validate_entity_id(item.get("entity_id"))
            except ha_read.AdapterError as error:
                raise MonitorError("invalid entity device map") from error
            physical_hash = item.get("physical_device_hash")
            device_id = item.get("device_id")
            platform = item.get("platform")
            entry_ids = item.get("config_entry_ids")
            if physical_hash is None or device_id is None:
                continue
            if (
                not isinstance(physical_hash, str)
                or not re.fullmatch(r"[a-f0-9]{64}", physical_hash)
                or not isinstance(device_id, str)
                or not re.fullmatch(r"[a-f0-9]{32}", device_id)
                or not isinstance(platform, str)
                or not re.fullmatch(r"[a-z0-9_]{1,64}", platform)
                or not isinstance(entry_ids, list)
                or len(entry_ids) > 32
            ):
                raise MonitorError("invalid entity device map")
            normalized_entries: list[str] = []
            for entry_id in entry_ids:
                if not isinstance(entry_id, str) or not re.fullmatch(
                    r"(?:[A-Z0-9]{26}|[a-f0-9]{32})", entry_id
                ):
                    raise MonitorError("invalid entity device map")
                normalized_entries.append(entry_id)
            rows.append((
                entity_id,
                physical_hash,
                device_id,
                platform,
                _json(sorted(set(normalized_entries))),
                observed_epoch,
            ))
        with self.connection:
            self.connection.execute("DELETE FROM entity_device_map")
            self.connection.executemany(
                """
                INSERT INTO entity_device_map(
                    entity_id,physical_device_hash,device_id,platform,
                    config_entry_ids_json,observed_epoch
                ) VALUES(?,?,?,?,?,?)
                """,
                rows,
            )
        return len(rows)

    def _physical_hash_for_entity(self, entity_id: str) -> str:
        row = self.connection.execute(
            "SELECT physical_device_hash FROM entity_device_map WHERE entity_id=?",
            (entity_id,),
        ).fetchone()
        if row is not None:
            value = str(row["physical_device_hash"])
            if re.fullmatch(r"[a-f0-9]{64}", value):
                return value
        return hashlib.sha256(f"entity\0{entity_id}".encode("ascii")).hexdigest()

    def physical_hash_for_entity(self, entity_id: str) -> str:
        normalized = _validate_subject(entity_id, "entity")
        return self._physical_hash_for_entity(normalized)

    def physical_device_members(self, entity_id: str) -> list[str]:
        normalized = _validate_subject(entity_id, "entity")
        physical_hash = self._physical_hash_for_entity(normalized)
        rows = self.connection.execute(
            """
            SELECT entity_id FROM entity_device_map
            WHERE physical_device_hash=? ORDER BY entity_id
            """,
            (physical_hash,),
        ).fetchall()
        members = [str(row["entity_id"]) for row in rows]
        return members or [normalized]

    def device_safety_class_for_entity(self, entity_id: str) -> str:
        return _device_safety_class(self.physical_device_members(entity_id))

    def record_freshness_observation(
        self,
        entity_id: str,
        *,
        source_epoch: int,
        observed_epoch: int,
        minimum_stale_seconds: int,
    ) -> dict[str, object]:
        """Learn report cadence and flag only conservatively overdue telemetry."""
        normalized = _validate_subject(entity_id, "entity")
        if (
            not normalized.startswith("sensor.")
            or not isinstance(source_epoch, int)
            or not isinstance(observed_epoch, int)
            or source_epoch < 0
            or observed_epoch < 0
            or observed_epoch < source_epoch - 30
            or not 1800 <= minimum_stale_seconds <= 604800
        ):
            raise MonitorError("invalid freshness observation")
        physical_hash = self._physical_hash_for_entity(normalized)
        current = self.connection.execute(
            "SELECT * FROM entity_freshness_observations WHERE entity_id=?",
            (normalized,),
        ).fetchone()
        if current is None:
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO entity_freshness_observations(
                        entity_id,physical_device_hash,first_observed_epoch,
                        last_source_epoch,last_poll_epoch,interval_samples,
                        mean_interval_seconds,stale_threshold_seconds,stale_active
                    ) VALUES(?,?,?,?,?,0,0,?,0)
                    """,
                    (
                        normalized, physical_hash, observed_epoch, source_epoch,
                        observed_epoch, minimum_stale_seconds,
                    ),
                )
            return {
                "status": "learning", "stale": False,
                "sample_count": 0, "threshold_seconds": minimum_stale_seconds,
            }

        prior_source = int(current["last_source_epoch"])
        sample_count = int(current["interval_samples"])
        mean_interval = int(current["mean_interval_seconds"])
        if source_epoch < prior_source:
            sample_count = 0
            mean_interval = 0
        elif source_epoch > prior_source:
            interval = source_epoch - prior_source
            if interval > 30 * 86400:
                sample_count = 0
                mean_interval = 0
            else:
                bounded_samples = min(sample_count, 19)
                mean_interval = round(
                    (mean_interval * bounded_samples + interval)
                    / (bounded_samples + 1)
                )
                sample_count = min(1000, sample_count + 1)
        threshold = min(
            604800,
            max(minimum_stale_seconds, mean_interval * 8),
        )
        stale = bool(
            source_epoch == prior_source
            and sample_count >= 3
            and observed_epoch - source_epoch > threshold
        )
        with self.connection:
            self.connection.execute(
                """
                UPDATE entity_freshness_observations
                SET physical_device_hash=?,last_source_epoch=?,last_poll_epoch=?,
                    interval_samples=?,mean_interval_seconds=?,
                    stale_threshold_seconds=?,stale_active=?
                WHERE entity_id=?
                """,
                (
                    physical_hash, source_epoch, observed_epoch, sample_count,
                    mean_interval, threshold, int(stale), normalized,
                ),
            )
        return {
            "status": (
                "stale" if stale else "learning" if sample_count < 3 else "fresh"
            ),
            "stale": stale,
            "sample_count": sample_count,
            "threshold_seconds": threshold,
        }

    def diagnose_device_incident_for_subject(
        self,
        entity_id: str,
        *,
        cause_code: str,
        cause_confidence: str,
        evidence_code: str,
    ) -> bool:
        normalized = _validate_subject(entity_id, "entity")
        if (
            cause_code not in CAUSE_CODES
            or cause_confidence not in CAUSE_CONFIDENCE
            or re.fullmatch(r"[a-z0-9_]{1,64}", evidence_code) is None
        ):
            raise MonitorError("invalid device diagnosis")
        physical_hash = self._physical_hash_for_entity(normalized)
        evidence = _json({"evidence_code": evidence_code})
        with self.connection:
            changed = self.connection.execute(
                """
                UPDATE device_incidents
                SET cause_code=?,cause_confidence=?,evidence_json=?
                WHERE physical_device_hash=?
                  AND status IN ('observed','confirmed','escalated')
                """,
                (cause_code, cause_confidence, evidence, physical_hash),
            ).rowcount
        return bool(changed)

    def record_device_health(
        self,
        *,
        physical_device_hash: str,
        display_name: str,
        health_status: str,
        cause_code: str,
        cause_confidence: str,
        safety_class: str,
        network_status: str,
        entity_count: int,
        available_entity_count: int,
        unavailable_entity_count: int,
        observed_epoch: int,
        baseline: bool,
    ) -> str | None:
        if (
            not re.fullmatch(r"[a-f0-9]{64}", physical_device_hash)
            or health_status not in {
                "healthy", "partial", "degraded", "offline", "unknown"
            }
            or cause_code not in CAUSE_CODES
            or cause_confidence not in CAUSE_CONFIDENCE
            or safety_class not in SAFETY_CLASSES
            or network_status not in {
                "stable", "ip_changed", "not_observed", "unknown"
            }
            or not isinstance(entity_count, int)
            or not isinstance(available_entity_count, int)
            or not isinstance(unavailable_entity_count, int)
            or entity_count < 1
            or not 0 <= available_entity_count <= entity_count
            or not 0 <= unavailable_entity_count <= entity_count
            or available_entity_count + unavailable_entity_count > entity_count
            or observed_epoch < 0
            or not isinstance(baseline, bool)
        ):
            raise MonitorError("invalid device health observation")
        safe_name = ha_read.sanitize_friendly_name(display_name)
        if safe_name is None:
            raise MonitorError("invalid device health observation")
        evidence = _json({
            "available_entity_count": available_entity_count,
            "baseline": baseline,
            "entity_count": entity_count,
            "network_status": network_status,
            "unavailable_entity_count": unavailable_entity_count,
        })
        prior = self.connection.execute(
            "SELECT health_status,cause_code FROM device_health_observations "
            "WHERE physical_device_hash=?",
            (physical_device_hash,),
        ).fetchone()
        event_type: str | None = None
        if prior is None:
            event_type = "observed"
        elif (
            str(prior["health_status"]) != health_status
            or str(prior["cause_code"]) != cause_code
        ):
            event_type = (
                "recovered"
                if health_status == "healthy"
                and str(prior["health_status"]) != "healthy"
                else "changed"
            )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO device_health_observations(
                    physical_device_hash,display_name,health_status,cause_code,
                    cause_confidence,safety_class,network_status,entity_count,
                    available_entity_count,unavailable_entity_count,
                    first_observed_epoch,last_observed_epoch,change_count,
                    evidence_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(physical_device_hash) DO UPDATE SET
                    display_name=excluded.display_name,
                    health_status=excluded.health_status,
                    cause_code=excluded.cause_code,
                    cause_confidence=excluded.cause_confidence,
                    safety_class=excluded.safety_class,
                    network_status=excluded.network_status,
                    entity_count=excluded.entity_count,
                    available_entity_count=excluded.available_entity_count,
                    unavailable_entity_count=excluded.unavailable_entity_count,
                    last_observed_epoch=excluded.last_observed_epoch,
                    change_count=device_health_observations.change_count + ?,
                    evidence_json=excluded.evidence_json
                """,
                (
                    physical_device_hash, safe_name, health_status, cause_code,
                    cause_confidence, safety_class, network_status, entity_count,
                    available_entity_count, unavailable_entity_count,
                    observed_epoch, observed_epoch, int(event_type is not None),
                    evidence, int(event_type is not None),
                ),
            )
            if event_type is not None:
                self.connection.execute(
                    """
                    INSERT INTO device_health_events(
                        physical_device_hash,event_type,observed_epoch,
                        health_status,cause_code,evidence_json
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        physical_device_hash, event_type, observed_epoch,
                        health_status, cause_code, evidence,
                    ),
                )
        return event_type

    def observe_network_device_incident(
        self,
        *,
        physical_device_hash: str,
        representative_subject: str,
        display_name: str,
        network_status: str,
        cause_code: str,
        cause_confidence: str,
        safety_class: str,
        observed_epoch: int,
        baseline: bool,
        confirm_after_seconds: int = 30,
    ) -> str | None:
        """Debounce network-only loss/IP drift into the normal device notifier."""
        if (
            not re.fullmatch(r"[a-f0-9]{64}", physical_device_hash)
            or network_status not in {"stable", "ip_changed", "not_observed", "unknown"}
            or cause_code not in {"unknown", "device_not_observed_on_lan", "confirmed_ip_change"}
            or cause_confidence not in CAUSE_CONFIDENCE
            or safety_class not in SAFETY_CLASSES
            or observed_epoch < 0
            or not isinstance(baseline, bool)
            or not 15 <= confirm_after_seconds <= 180
        ):
            raise MonitorError("invalid network device incident")
        subject = _validate_subject(representative_subject, "entity")
        safe_name = ha_read.sanitize_friendly_name(display_name)
        if safe_name is None:
            raise MonitorError("invalid network device incident")
        current = self.connection.execute(
            "SELECT d.id,d.status,d.first_observed_epoch,"
            "(SELECT COUNT(*) FROM device_incident_members m "
            "WHERE m.device_incident_id=d.id) AS member_count "
            "FROM device_incidents d WHERE d.physical_device_hash=? "
            "AND d.status IN ('observed','confirmed','escalated')",
            (physical_device_hash,),
        ).fetchone()
        network_bad = network_status in {"not_observed", "ip_changed"}
        with self.connection:
            if network_bad:
                if current is None:
                    if baseline:
                        return None
                    self.connection.execute(
                        """
                        INSERT INTO device_incidents(
                            physical_device_hash,representative_subject,display_name,
                            status,first_observed_epoch,last_observed_epoch,
                            confirmed_epoch,resolved_epoch,severity,cause_code,
                            cause_confidence,safety_class,baseline,evidence_json
                        ) VALUES(?,?,?,'observed',?,?,NULL,NULL,'warning',?,?,?,0,?)
                        """,
                        (
                            physical_device_hash, subject, safe_name,
                            observed_epoch, observed_epoch, cause_code,
                            cause_confidence, safety_class,
                            _json({"source": "network_scanner", "status": network_status}),
                        ),
                    )
                    return "observed"
                incident_id = int(current["id"])
                status = str(current["status"])
                confirmed = (
                    status == "observed"
                    and observed_epoch - int(current["first_observed_epoch"])
                    >= confirm_after_seconds
                )
                self.connection.execute(
                    "UPDATE device_incidents SET last_observed_epoch=?,"
                    "display_name=?,cause_code=?,cause_confidence=?,"
                    "status=CASE WHEN ? THEN 'confirmed' ELSE status END,"
                    "confirmed_epoch=CASE WHEN ? THEN ? ELSE confirmed_epoch END,"
                    "evidence_json=? WHERE id=?",
                    (
                        observed_epoch, safe_name, cause_code, cause_confidence,
                        int(confirmed), int(confirmed), observed_epoch,
                        _json({"source": "network_scanner", "status": network_status}),
                        incident_id,
                    ),
                )
                return "confirmed" if confirmed else None
            if current is not None and int(current["member_count"]) == 0:
                self.connection.execute(
                    "UPDATE device_incidents SET status='resolved',"
                    "last_observed_epoch=?,resolved_epoch=?,evidence_json=? WHERE id=?",
                    (
                        observed_epoch, observed_epoch,
                        _json({"source": "network_scanner", "status": network_status}),
                        int(current["id"]),
                    ),
                )
                return "resolved"
        return None

    def record_integration_health(
        self,
        *,
        domain: str,
        entry_count: int,
        loaded_entry_count: int,
        observed_epoch: int,
        baseline: bool,
    ) -> str | None:
        """Record only integration state transitions, never raw config data."""
        if (
            re.fullmatch(r"[a-z0-9_]{1,64}", domain) is None
            or not isinstance(entry_count, int)
            or not isinstance(loaded_entry_count, int)
            or entry_count < 0
            or not 0 <= loaded_entry_count <= entry_count
            or observed_epoch < 0
            or not isinstance(baseline, bool)
        ):
            raise MonitorError("invalid integration health observation")
        health_status = (
            "healthy"
            if entry_count == 0 or loaded_entry_count == entry_count
            else "degraded"
        )
        prior = self.connection.execute(
            "SELECT health_status FROM integration_health_observations "
            "WHERE domain=?",
            (domain,),
        ).fetchone()
        event_type: str | None = None
        if prior is None:
            event_type = "observed"
        elif str(prior["health_status"]) != health_status:
            event_type = "recovered" if health_status == "healthy" else "changed"
        evidence = _json({
            "baseline": baseline,
            "entry_count": entry_count,
            "loaded_entry_count": loaded_entry_count,
        })
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO integration_health_observations(
                    domain,health_status,entry_count,loaded_entry_count,
                    first_observed_epoch,last_observed_epoch,change_count
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(domain) DO UPDATE SET
                    health_status=excluded.health_status,
                    entry_count=excluded.entry_count,
                    loaded_entry_count=excluded.loaded_entry_count,
                    last_observed_epoch=excluded.last_observed_epoch,
                    change_count=integration_health_observations.change_count + ?
                """,
                (
                    domain, health_status, entry_count, loaded_entry_count,
                    observed_epoch, observed_epoch, int(event_type is not None),
                    int(event_type is not None),
                ),
            )
            if event_type is not None:
                self.connection.execute(
                    """
                    INSERT INTO integration_health_events(
                        domain,event_type,observed_epoch,health_status,evidence_json
                    ) VALUES(?,?,?,?,?)
                    """,
                    (
                        domain, event_type, observed_epoch, health_status,
                        evidence,
                    ),
                )
        return event_type

    def confirmed_integration_failure_epoch(
        self,
        domain: str,
        observed_epoch: int,
        *,
        confirm_after_seconds: int = 15,
    ) -> int | None:
        """Return the non-baseline degradation epoch after a stable confirmation."""
        if (
            re.fullmatch(r"[a-z0-9_]{1,64}", domain) is None
            or observed_epoch < 0
            or not 15 <= confirm_after_seconds <= 180
        ):
            raise MonitorError("invalid integration confirmation policy")
        row = self.connection.execute(
            """
            SELECT observed_epoch,health_status,evidence_json
            FROM integration_health_events
            WHERE domain=? ORDER BY id DESC LIMIT 1
            """,
            (domain,),
        ).fetchone()
        if row is None or str(row["health_status"]) != "degraded":
            return None
        try:
            evidence = json.loads(str(row["evidence_json"]))
        except json.JSONDecodeError as error:
            raise MonitorError("invalid integration health evidence") from error
        if not isinstance(evidence, dict) or evidence.get("baseline") is True:
            return None
        failure_epoch = int(row["observed_epoch"])
        if observed_epoch - failure_epoch < confirm_after_seconds:
            return None
        return failure_epoch

    def record_automation_run(
        self,
        *,
        run_hash: str,
        automation_entity_id: str,
        automation_item_hash: str,
        outcome: str,
        started_epoch: int,
        observed_epoch: int,
        error_code: str,
        cause_code: str,
        cause_confidence: str,
        action_code: str,
        target_entity_id: str | None,
        display_name: str,
    ) -> dict[str, object]:
        """Persist one sanitized automation result and aggregate failures."""
        if (
            not re.fullmatch(r"[a-f0-9]{64}", run_hash)
            or not re.fullmatch(r"[a-f0-9]{64}", automation_item_hash)
            or outcome not in {"failed", "succeeded"}
            or started_epoch < 0
            or observed_epoch < started_epoch
            or cause_code not in CAUSE_CODES
            or cause_confidence not in CAUSE_CONFIDENCE
            or not re.fullmatch(r"[a-z0-9_]{1,64}", error_code)
            or not re.fullmatch(r"[a-z0-9_.]{1,64}", action_code)
        ):
            raise MonitorError("invalid automation result")
        automation_entity = _validate_subject(automation_entity_id, "entity")
        if not automation_entity.startswith("automation."):
            raise MonitorError("invalid automation result")
        target = None
        physical_hash = None
        safety_class = "unknown"
        if target_entity_id is not None:
            target = _validate_subject(target_entity_id, "entity")
            physical_hash = self._physical_hash_for_entity(target)
            safety_class = _device_safety_class([target])
        safe_name = ha_read.sanitize_friendly_name(display_name)
        if safe_name is None:
            safe_name = " ".join(
                automation_entity.split(".", 1)[1].replace("_", " ").split()
            )[:100]
        if not safe_name:
            raise MonitorError("invalid automation result")
        evidence = _json({
            "action_code": action_code,
            "error_code": error_code,
            "target_known": target is not None,
        })
        incident_key = hashlib.sha256(
            f"automation\0{automation_entity}\0{physical_hash or 'unknown'}".encode(
                "ascii"
            )
        ).hexdigest()
        with self.connection:
            inserted = self.connection.execute(
                """
                INSERT OR IGNORE INTO automation_runs(
                    run_hash,automation_entity_id,automation_item_hash,outcome,
                    started_epoch,observed_epoch,error_code,cause_code,
                    cause_confidence,action_code,target_entity_id,
                    physical_device_hash,evidence_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_hash, automation_entity, automation_item_hash, outcome,
                    started_epoch, observed_epoch, error_code, cause_code,
                    cause_confidence, action_code, target, physical_hash, evidence,
                ),
            ).rowcount
            if not inserted or outcome != "failed":
                return {"recorded": bool(inserted), "incident_id": None}
            current = self.connection.execute(
                """
                SELECT id FROM operational_incidents
                WHERE incident_key=? AND status IN ('confirmed','escalated')
                """,
                (incident_key,),
            ).fetchone()
            if current is None:
                cursor = self.connection.execute(
                    """
                    INSERT INTO operational_incidents(
                        incident_key,source_type,automation_entity_id,
                        physical_device_hash,representative_subject,display_name,
                        status,first_observed_epoch,last_observed_epoch,
                        resolved_epoch,occurrences,cause_code,cause_confidence,
                        safety_class,action_code,error_code,target_entity_id,
                        evidence_json
                    ) VALUES(?,'automation',?,?,?,?,'confirmed',?,?,NULL,1,?,?,?,?,?,?,?)
                    """,
                    (
                        incident_key, automation_entity, physical_hash,
                        target or automation_entity, safe_name, started_epoch,
                        observed_epoch, cause_code, cause_confidence, safety_class,
                        action_code, error_code, target, evidence,
                    ),
                )
                incident_id = int(cursor.lastrowid)
                event_type = "confirmed"
            else:
                incident_id = int(current["id"])
                self.connection.execute(
                    """
                    UPDATE operational_incidents
                    SET last_observed_epoch=?,occurrences=occurrences+1,
                        cause_code=?,cause_confidence=?,action_code=?,
                        error_code=?,target_entity_id=?,evidence_json=?
                    WHERE id=?
                    """,
                    (
                        observed_epoch, cause_code, cause_confidence,
                        action_code, error_code, target, evidence, incident_id,
                    ),
                )
                event_type = "repeated"
            self.connection.execute(
                """
                INSERT INTO operational_incident_events(
                    operational_incident_id,event_type,observed_epoch,evidence_json
                ) VALUES(?,?,?,?)
                """,
                (incident_id, event_type, observed_epoch, evidence),
            )
        return {"recorded": True, "incident_id": incident_id}

    def record_service_call(
        self,
        *,
        event_hash: str,
        context_hash: str | None,
        domain: str,
        service: str,
        entity_ids: list[str],
        observed_epoch: int,
    ) -> bool:
        """Store only the bounded routing facts from a HA call_service event."""
        if (
            not re.fullmatch(r"[a-f0-9]{64}", event_hash)
            or context_hash is not None
            and not re.fullmatch(r"[a-f0-9]{64}", context_hash)
            or not re.fullmatch(r"[a-z0-9_]{1,64}", domain)
            or not re.fullmatch(r"[a-z0-9_]{1,64}", service)
            or observed_epoch < 0
            or len(entity_ids) > 64
        ):
            raise MonitorError("invalid service call observation")
        normalized = sorted({
            _validate_subject(entity_id, "entity") for entity_id in entity_ids
        })
        with self.connection:
            inserted = self.connection.execute(
                """
                INSERT OR IGNORE INTO service_call_observations(
                    event_hash,context_hash,domain,service,entity_ids_json,
                    observed_epoch
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    event_hash, context_hash, domain, service,
                    _json(normalized), observed_epoch,
                ),
            ).rowcount
            self.connection.execute(
                "DELETE FROM service_call_observations WHERE observed_epoch<?",
                (max(0, observed_epoch - 7 * 86400),),
            )
        return bool(inserted)

    def recent_service_calls(
        self, observed_epoch: int, *, window_seconds: int = 30
    ) -> list[dict[str, object]]:
        if observed_epoch < 0 or not 1 <= window_seconds <= 300:
            raise MonitorError("invalid service call window")
        rows = self.connection.execute(
            """
            SELECT * FROM service_call_observations
            WHERE observed_epoch BETWEEN ? AND ?
            ORDER BY observed_epoch DESC,event_hash
            """,
            (max(0, observed_epoch - window_seconds), observed_epoch + 5),
        ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            try:
                entity_ids = json.loads(str(row["entity_ids_json"]))
            except json.JSONDecodeError as error:
                raise MonitorError("invalid service call ledger") from error
            if not isinstance(entity_ids, list):
                raise MonitorError("invalid service call ledger")
            result.append({
                "event_hash": str(row["event_hash"]),
                "context_hash": (
                    str(row["context_hash"])
                    if row["context_hash"] is not None else None
                ),
                "domain": str(row["domain"]),
                "service": str(row["service"]),
                "entity_ids": [str(value) for value in entity_ids],
                "observed_epoch": int(row["observed_epoch"]),
            })
        return result

    def diagnostic_cursor_exists(self, name: str) -> bool:
        if re.fullmatch(r"[a-z0-9_]{1,64}", name) is None:
            raise MonitorError("invalid diagnostic cursor")
        return self.connection.execute(
            "SELECT 1 FROM diagnostic_cursors WHERE name=?", (name,)
        ).fetchone() is not None

    def mark_diagnostic_cursor(self, name: str, observed_epoch: int) -> None:
        if (
            re.fullmatch(r"[a-z0-9_]{1,64}", name) is None
            or observed_epoch < 0
        ):
            raise MonitorError("invalid diagnostic cursor")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO diagnostic_cursors(name,initialized_epoch)
                VALUES(?,?) ON CONFLICT(name) DO NOTHING
                """,
                (name, observed_epoch),
            )

    def record_operational_failure(
        self,
        *,
        event_hash: str,
        source_type: str,
        source_ref: str,
        observed_epoch: int,
        error_code: str,
        cause_code: str,
        cause_confidence: str,
        action_code: str,
        target_entity_id: str | None,
        display_name: str,
        evidence_code: str,
        baseline: bool = False,
    ) -> dict[str, object]:
        """Aggregate one sanitized non-automation failure into the incident log."""
        if (
            not re.fullmatch(r"[a-f0-9]{64}", event_hash)
            or source_type not in {"service_call", "system_log", "integration"}
            or not re.fullmatch(r"[a-z0-9_.:-]{1,160}", source_ref)
            or observed_epoch < 0
            or not re.fullmatch(r"[a-z0-9_]{1,64}", error_code)
            or cause_code not in CAUSE_CODES
            or cause_confidence not in CAUSE_CONFIDENCE
            or not re.fullmatch(r"[a-z0-9_.]{1,64}", action_code)
            or not re.fullmatch(r"[a-z0-9_]{1,64}", evidence_code)
            or not isinstance(baseline, bool)
        ):
            raise MonitorError("invalid operational failure")
        target = None
        physical_hash = None
        safety_class = "unknown"
        if target_entity_id is not None:
            target = _validate_subject(target_entity_id, "entity")
            physical_hash = self._physical_hash_for_entity(target)
            safety_class = self.device_safety_class_for_entity(target)
        safe_name = ha_read.sanitize_friendly_name(display_name)
        if safe_name is None:
            safe_name = "Home Assistant integration"
        evidence = _json({
            "error_code": error_code,
            "evidence_code": evidence_code,
            "target_known": target is not None,
        })
        incident_key = hashlib.sha256(
            f"{source_type}\0{source_ref}\0{physical_hash or 'unknown'}\0"
            f"{action_code}\0{error_code}".encode("ascii")
        ).hexdigest()
        with self.connection:
            inserted = self.connection.execute(
                """
                INSERT OR IGNORE INTO operational_observations(
                    event_hash,source_type,source_ref,observed_epoch,error_code,
                    cause_code,cause_confidence,action_code,target_entity_id,
                    physical_device_hash,evidence_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_hash, source_type, source_ref, observed_epoch,
                    error_code, cause_code, cause_confidence, action_code,
                    target, physical_hash, evidence,
                ),
            ).rowcount
            if not inserted:
                return {"recorded": False, "incident_id": None}
            if baseline:
                return {"recorded": True, "incident_id": None}
            current = self.connection.execute(
                """
                SELECT id FROM operational_incidents
                WHERE incident_key=? AND status IN ('confirmed','escalated')
                """,
                (incident_key,),
            ).fetchone()
            if current is None:
                cursor = self.connection.execute(
                    """
                    INSERT INTO operational_incidents(
                        incident_key,source_type,automation_entity_id,
                        physical_device_hash,representative_subject,display_name,
                        status,first_observed_epoch,last_observed_epoch,
                        resolved_epoch,occurrences,cause_code,cause_confidence,
                        safety_class,action_code,error_code,target_entity_id,
                        evidence_json
                    ) VALUES(?,?,?,?,?,?,'confirmed',?,?,NULL,1,?,?,?,?,?,?,?)
                    """,
                    (
                        incident_key, source_type, source_ref, physical_hash,
                        target or RESERVED_SUBJECT, safe_name, observed_epoch,
                        observed_epoch, cause_code, cause_confidence,
                        safety_class, action_code, error_code, target, evidence,
                    ),
                )
                incident_id = int(cursor.lastrowid)
                event_type = "confirmed"
            else:
                incident_id = int(current["id"])
                self.connection.execute(
                    """
                    UPDATE operational_incidents
                    SET last_observed_epoch=?,occurrences=occurrences+1,
                        cause_code=?,cause_confidence=?,action_code=?,
                        error_code=?,target_entity_id=?,evidence_json=?
                    WHERE id=?
                    """,
                    (
                        observed_epoch, cause_code, cause_confidence,
                        action_code, error_code, target, evidence, incident_id,
                    ),
                )
                event_type = "repeated"
            self.connection.execute(
                """
                INSERT INTO operational_incident_events(
                    operational_incident_id,event_type,observed_epoch,evidence_json
                ) VALUES(?,?,?,?)
                """,
                (incident_id, event_type, observed_epoch, evidence),
            )
        return {"recorded": True, "incident_id": incident_id}

    def resolve_operational_incident(
        self, incident_id: int, observed_epoch: int, verification_code: str
    ) -> bool:
        """Resolve only after a deterministic post-action verification."""
        if (
            incident_id < 1
            or observed_epoch < 0
            or verification_code not in {
                "target_state_confirmed",
                "automation_trace_succeeded",
                "integration_healthy",
            }
        ):
            raise MonitorError("invalid operational verification")
        evidence = _json({"verification_code": verification_code})
        with self.connection:
            changed = self.connection.execute(
                """
                UPDATE operational_incidents
                SET status='resolved',last_observed_epoch=?,resolved_epoch=?,
                    evidence_json=?
                WHERE id=? AND status IN ('confirmed','escalated')
                """,
                (observed_epoch, observed_epoch, evidence, incident_id),
            ).rowcount
            if changed:
                self.connection.execute(
                    """
                    INSERT INTO operational_incident_events(
                        operational_incident_id,event_type,observed_epoch,evidence_json
                    ) VALUES(?,'resolved',?,?)
                    """,
                    (incident_id, observed_epoch, evidence),
                )
        return bool(changed)

    def escalate_operational_incident(
        self,
        incident_id: int,
        observed_epoch: int,
        *,
        cause_code: str,
        cause_confidence: str,
        evidence_code: str,
    ) -> bool:
        """Record a verified failed recovery without exposing raw evidence."""
        if (
            incident_id < 1
            or observed_epoch < 0
            or cause_code not in CAUSE_CODES
            or cause_confidence not in CAUSE_CONFIDENCE
            or re.fullmatch(r"[a-z0-9_]{1,64}", evidence_code) is None
        ):
            raise MonitorError("invalid operational escalation")
        evidence = _json({"evidence_code": evidence_code})
        with self.connection:
            changed = self.connection.execute(
                """
                UPDATE operational_incidents
                SET status='escalated',last_observed_epoch=?,cause_code=?,
                    cause_confidence=?,evidence_json=?
                WHERE id=? AND status IN ('confirmed','escalated')
                """,
                (
                    observed_epoch, cause_code, cause_confidence, evidence,
                    incident_id,
                ),
            ).rowcount
            if changed:
                self.connection.execute(
                    """
                    INSERT INTO operational_incident_events(
                        operational_incident_id,event_type,observed_epoch,
                        evidence_json
                    ) VALUES(?,'escalated',?,?)
                    """,
                    (incident_id, observed_epoch, evidence),
                )
        return bool(changed)

    def operational_incident_candidates(self) -> list[dict[str, object]]:
        """Return sanitized operational incidents awaiting bounded recovery."""
        rows = self.connection.execute(
            """
            SELECT * FROM operational_incidents
            WHERE status IN ('confirmed','escalated')
            ORDER BY first_observed_epoch,id
            """
        ).fetchall()
        return [
            {
                "incident_id": int(row["id"]),
                "status": str(row["status"]),
                "source_type": str(row["source_type"]),
                "automation_entity_id": str(row["automation_entity_id"]),
                "representative_subject": str(row["representative_subject"]),
                "display_name": str(row["display_name"]),
                "first_observed_epoch": int(row["first_observed_epoch"]),
                "last_observed_epoch": int(row["last_observed_epoch"]),
                "occurrences": int(row["occurrences"]),
                "cause_code": str(row["cause_code"]),
                "cause_confidence": str(row["cause_confidence"]),
                "safety_class": str(row["safety_class"]),
                "action_code": str(row["action_code"]),
                "error_code": str(row["error_code"]),
                "target_entity_id": (
                    str(row["target_entity_id"])
                    if row["target_entity_id"] is not None else None
                ),
            }
            for row in rows
        ]

    def record_recovery_decision(
        self,
        *,
        decision_id: str,
        operational_incident_id: int,
        selected_candidate_id: str,
        decision_source: str,
        fact_ids: list[str],
        decided_epoch: int,
    ) -> bool:
        if (
            not re.fullmatch(r"[a-f0-9]{64}", decision_id)
            or operational_incident_id < 1
            or not re.fullmatch(r"[a-z0-9_]{1,64}", selected_candidate_id)
            or decision_source not in {"model", "verified_fallback"}
            or not fact_ids
            or len(fact_ids) > 32
            or len(set(fact_ids)) != len(fact_ids)
            or any(re.fullmatch(r"[a-z0-9_.:-]{1,96}", item) is None for item in fact_ids)
            or decided_epoch < 0
        ):
            raise MonitorError("invalid recovery decision")
        incident = self.connection.execute(
            "SELECT id FROM operational_incidents WHERE id=?",
            (operational_incident_id,),
        ).fetchone()
        if incident is None:
            raise MonitorError("recovery decision incident is unavailable")
        with self.connection:
            changed = self.connection.execute(
                """
                INSERT OR IGNORE INTO recovery_decisions(
                    decision_id,operational_incident_id,selected_candidate_id,
                    decision_source,fact_ids_json,decided_epoch,status
                ) VALUES(?,?,?,?,?,?,'pending')
                """,
                (
                    decision_id, operational_incident_id,
                    selected_candidate_id, decision_source,
                    _json(sorted(fact_ids)), decided_epoch,
                ),
            ).rowcount
        return bool(changed)

    def operational_attempt_count(
        self, operational_incident_id: int, candidate_id: str | None = None
    ) -> int:
        if operational_incident_id < 1 or (
            candidate_id is not None
            and re.fullmatch(r"[a-z0-9_]{1,64}", candidate_id) is None
        ):
            raise MonitorError("invalid operational attempt query")
        if candidate_id is None:
            row = self.connection.execute(
                """
                SELECT COUNT(*) AS amount FROM operational_recovery_attempts
                WHERE operational_incident_id=?
                  AND status IN ('failed','delivery_unknown','verified')
                """,
                (operational_incident_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                """
                SELECT COUNT(*) AS amount FROM operational_recovery_attempts
                WHERE operational_incident_id=? AND candidate_id=?
                  AND status IN ('failed','delivery_unknown','verified')
                """,
                (operational_incident_id, candidate_id),
            ).fetchone()
        return int(row["amount"])

    def operational_candidate_total(
        self, operational_incident_id: int, candidate_id: str
    ) -> int:
        if (
            operational_incident_id < 1
            or re.fullmatch(r"[a-z0-9_]{1,64}", candidate_id) is None
        ):
            raise MonitorError("invalid operational attempt query")
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS amount FROM operational_recovery_attempts
            WHERE operational_incident_id=? AND candidate_id=?
            """,
            (operational_incident_id, candidate_id),
        ).fetchone()
        return int(row["amount"])

    def operational_next_allowed_epoch(
        self, operational_incident_id: int
    ) -> int | None:
        if operational_incident_id < 1:
            raise MonitorError("invalid operational cooldown query")
        row = self.connection.execute(
            """
            SELECT MAX(next_allowed_epoch) AS value
            FROM operational_recovery_attempts
            WHERE operational_incident_id=?
            """,
            (operational_incident_id,),
        ).fetchone()
        return int(row["value"]) if row["value"] is not None else None

    def last_operational_candidate_epoch(self, candidate_id: str) -> int | None:
        if re.fullmatch(r"[a-z0-9_]{1,64}", candidate_id) is None:
            raise MonitorError("invalid operational cooldown query")
        row = self.connection.execute(
            """
            SELECT MAX(attempted_epoch) AS value
            FROM operational_recovery_attempts
            WHERE candidate_id=? AND status IN (
                'failed','delivery_unknown','verified'
            )
            """,
            (candidate_id,),
        ).fetchone()
        return int(row["value"]) if row["value"] is not None else None

    def record_operational_attempt(
        self,
        *,
        operational_incident_id: int,
        decision_id: str,
        candidate_id: str,
        attempted_epoch: int,
        status: str,
        service_calls: int,
        verification_checks: int,
        before_state: str,
        after_state: str,
        next_allowed_epoch: int,
        evidence_code: str,
    ) -> bool:
        if (
            operational_incident_id < 1
            or re.fullmatch(r"[a-f0-9]{64}", decision_id) is None
            or re.fullmatch(r"[a-z0-9_]{1,64}", candidate_id) is None
            or status not in {
                "no_action", "failed", "delivery_unknown", "verified",
                "rejected", "cooldown",
            }
            or attempted_epoch < 0
            or service_calls not in {0, 1}
            or verification_checks not in {0, 1, 2, 3}
            or re.fullmatch(r"[a-z0-9_]{1,64}", before_state) is None
            or re.fullmatch(r"[a-z0-9_]{1,64}", after_state) is None
            or next_allowed_epoch < attempted_epoch
            or re.fullmatch(r"[a-z0-9_]{1,64}", evidence_code) is None
        ):
            raise MonitorError("invalid operational recovery result")
        decision = self.connection.execute(
            """
            SELECT operational_incident_id,selected_candidate_id,status
            FROM recovery_decisions WHERE decision_id=?
            """,
            (decision_id,),
        ).fetchone()
        if (
            decision is None
            or int(decision["operational_incident_id"]) != operational_incident_id
            or str(decision["selected_candidate_id"]) != candidate_id
            or str(decision["status"]) != "pending"
        ):
            raise MonitorError("operational recovery decision changed")
        evidence = _json({"evidence_code": evidence_code})
        with self.connection:
            changed = self.connection.execute(
                """
                INSERT OR IGNORE INTO operational_recovery_attempts(
                    operational_incident_id,decision_id,candidate_id,
                    attempted_epoch,status,service_calls,verification_checks,
                    before_state,after_state,next_allowed_epoch,evidence_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    operational_incident_id, decision_id, candidate_id,
                    attempted_epoch, status, service_calls, verification_checks,
                    before_state, after_state, next_allowed_epoch, evidence,
                ),
            ).rowcount
            if changed:
                decision_status = "rejected" if status == "rejected" else "executed"
                self.connection.execute(
                    "UPDATE recovery_decisions SET status=? WHERE decision_id=?",
                    (decision_status, decision_id),
                )
        return bool(changed)

    def operational_notification_candidates(
        self,
        observed_epoch: int,
        *,
        retry_seconds: int = 300,
        max_attempts: int = 3,
    ) -> list[dict[str, object]]:
        if observed_epoch < 0 or retry_seconds < 1 or max_attempts < 1:
            raise MonitorError("invalid operational notification policy")
        rows = self.connection.execute(
            """
            SELECT o.*,
                   detected.status AS detected_status,
                   detected.attempts AS detected_attempts,
                   detected.last_attempt_epoch AS detected_last_attempt_epoch,
                   resolved.status AS resolved_status,
                   resolved.attempts AS resolved_attempts,
                   resolved.last_attempt_epoch AS resolved_last_attempt_epoch,
                   EXISTS(
                       SELECT 1 FROM operational_recovery_attempts AS a
                       WHERE a.operational_incident_id=o.id
                         AND a.status='verified'
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
                   ),'service_action') AS action_code
            FROM operational_incidents AS o
            LEFT JOIN operational_incident_notifications AS detected
              ON detected.operational_incident_id=o.id
             AND detected.phase='detected'
            LEFT JOIN operational_incident_notifications AS resolved
              ON resolved.operational_incident_id=o.id
             AND resolved.phase='resolved'
            ORDER BY o.id
            """
        ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            if row["status"] in {"confirmed", "escalated"}:
                phase = "detected"
            elif row["status"] == "resolved":
                phase = (
                    "resolved" if row["detected_status"] == "accepted" else None
                )
            else:
                phase = None
            if phase is None:
                continue
            notification_status = row[f"{phase}_status"]
            if notification_status in {"accepted", "delivery_unknown"}:
                continue
            attempts_value = row[f"{phase}_attempts"]
            attempts = int(attempts_value) if attempts_value is not None else 0
            if attempts >= max_attempts:
                continue
            last_attempt_value = row[f"{phase}_last_attempt_epoch"]
            if (
                notification_status == "claimed"
                and last_attempt_value is not None
                and observed_epoch - int(last_attempt_value) < retry_seconds
            ):
                continue
            result.append({
                "operational_incident_id": int(row["id"]),
                "source_type": str(row["source_type"]),
                "phase": phase,
                "display_name": str(row["display_name"]),
                "cause_code": str(row["cause_code"]),
                "action_code": str(row["action_code"]),
                "first_observed_epoch": int(row["first_observed_epoch"]),
                "resolved_epoch": (
                    int(row["resolved_epoch"])
                    if row["resolved_epoch"] is not None else None
                ),
                "detected_was_announced": row["detected_status"] == "accepted",
                "agent_recovered": bool(row["agent_recovered"]),
                "attempts": attempts,
            })
        return result

    def claim_operational_notification(
        self,
        operational_incident_id: int,
        phase: str,
        claimed_epoch: int,
        *,
        retry_seconds: int = 300,
        max_attempts: int = 3,
    ) -> int:
        if (
            operational_incident_id < 1
            or phase not in {"detected", "resolved"}
            or claimed_epoch < 0
            or retry_seconds < 1
            or max_attempts < 1
        ):
            raise MonitorError("invalid operational notification claim")
        with self.connection:
            changed = self.connection.execute(
                """
                INSERT OR IGNORE INTO operational_incident_notifications(
                    operational_incident_id,phase,status,claimed_epoch,
                    attempts,last_attempt_epoch,completed_epoch,speaker_entity_id
                ) VALUES(?,?,'claimed',?,1,?,NULL,NULL)
                """,
                (
                    operational_incident_id, phase, claimed_epoch, claimed_epoch,
                ),
            ).rowcount
            if not changed:
                changed = self.connection.execute(
                    """
                    UPDATE operational_incident_notifications
                    SET attempts=attempts+1,claimed_epoch=?,last_attempt_epoch=?
                    WHERE operational_incident_id=? AND phase=?
                      AND status='claimed' AND attempts<?
                      AND (last_attempt_epoch IS NULL OR last_attempt_epoch<=?)
                    """,
                    (
                        claimed_epoch,
                        claimed_epoch,
                        operational_incident_id,
                        phase,
                        max_attempts,
                        claimed_epoch - retry_seconds,
                    ),
                ).rowcount
            if not changed:
                return 0
            row = self.connection.execute(
                "SELECT attempts FROM operational_incident_notifications "
                "WHERE operational_incident_id=? AND phase=?",
                (operational_incident_id, phase),
            ).fetchone()
        return int(row["attempts"]) if row is not None else 0

    def finalize_operational_notification(
        self,
        operational_incident_id: int,
        phase: str,
        completed_epoch: int,
        *,
        status: str,
        speaker_entity_id: str | None,
    ) -> bool:
        if (
            operational_incident_id < 1
            or phase not in {"detected", "resolved"}
            or completed_epoch < 0
            or status not in {"accepted", "delivery_unknown"}
        ):
            raise MonitorError("invalid operational notification result")
        if speaker_entity_id is not None:
            speaker = _validate_subject(speaker_entity_id, "entity")
            if not speaker.startswith("media_player."):
                raise MonitorError("invalid operational notification speaker")
        with self.connection:
            changed = self.connection.execute(
                """
                UPDATE operational_incident_notifications
                SET status=?,completed_epoch=?,speaker_entity_id=?
                WHERE operational_incident_id=? AND phase=? AND status='claimed'
                """,
                (
                    status, completed_epoch, speaker_entity_id,
                    operational_incident_id, phase,
                ),
            ).rowcount
        return bool(changed)

    def reconcile_device_incidents(self, observed_epoch: int) -> dict[str, int]:
        """Roll raw entity incidents into durable physical-device incidents."""
        if observed_epoch < 0:
            raise MonitorError("invalid device incident time")
        repaired = self._split_announced_reopenings()
        unlinked = self.connection.execute(
            """
            SELECT i.*
            FROM incidents AS i
            LEFT JOIN device_incident_members AS m ON m.entity_incident_id=i.id
            WHERE i.kind='entity' AND i.baseline=0 AND m.entity_incident_id IS NULL
            ORDER BY i.id
            """
        ).fetchall()
        touched: set[int] = set()
        created = repaired
        with self.connection:
            for row in unlinked:
                subject = str(row["subject"])
                physical_hash = self._physical_hash_for_entity(subject)
                device_row = self.connection.execute(
                    """
                    SELECT id FROM device_incidents
                    WHERE physical_device_hash=?
                      AND (
                        status IN ('observed','confirmed','escalated')
                        OR (
                          first_observed_epoch<=?
                          AND COALESCE(resolved_epoch,last_observed_epoch)>=?
                          AND NOT EXISTS (
                            SELECT 1
                            FROM device_incident_notifications AS notice
                            WHERE notice.device_incident_id=device_incidents.id
                              AND notice.phase='resolved'
                              AND notice.status IN ('accepted','delivery_unknown')
                          )
                        )
                      )
                    ORDER BY id DESC LIMIT 1
                    """,
                    (
                        physical_hash,
                        int(row["resolved_epoch"] or row["last_observed_epoch"]) + 180,
                        int(row["first_observed_epoch"]) - 180,
                    ),
                ).fetchone()
                if device_row is None:
                    cursor = self.connection.execute(
                        """
                        INSERT INTO device_incidents(
                            physical_device_hash,representative_subject,display_name,
                            status,first_observed_epoch,last_observed_epoch,
                            confirmed_epoch,resolved_epoch,severity,cause_code,
                            cause_confidence,safety_class,baseline,evidence_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?)
                        """,
                        (
                            physical_hash,
                            subject,
                            _device_display_name([subject]),
                            str(row["status"]),
                            int(row["first_observed_epoch"]),
                            int(row["last_observed_epoch"]),
                            row["confirmed_epoch"],
                            row["resolved_epoch"],
                            str(row["severity"]),
                            "unknown",
                            "unknown",
                            _device_safety_class([subject]),
                            _json({"active_members": int(row["status"] != "resolved"), "member_count": 1}),
                        ),
                    )
                    device_incident_id = int(cursor.lastrowid)
                    created += 1
                else:
                    device_incident_id = int(device_row["id"])
                self.connection.execute(
                    """
                    INSERT INTO device_incident_members(
                        device_incident_id,entity_incident_id,entity_id
                    ) VALUES(?,?,?)
                    """,
                    (device_incident_id, int(row["id"]), subject),
                )
                touched.add(device_incident_id)

            touched.update(
                int(row["id"])
                for row in self.connection.execute(
                    "SELECT d.id FROM device_incidents d WHERE d.status IN "
                    "('observed','confirmed','escalated') AND EXISTS (SELECT 1 "
                    "FROM device_incident_members m WHERE m.device_incident_id=d.id)"
                )
            )
            resolved = 0
            for device_incident_id in sorted(touched):
                members = self.connection.execute(
                    """
                    SELECT i.subject,i.status,i.first_observed_epoch,
                           i.last_observed_epoch,i.confirmed_epoch,i.resolved_epoch,
                           i.severity
                    FROM device_incident_members AS m
                    JOIN incidents AS i ON i.id=m.entity_incident_id
                    WHERE m.device_incident_id=?
                    ORDER BY i.id
                    """,
                    (device_incident_id,),
                ).fetchall()
                if not members:
                    raise MonitorError("device incident has no members")
                subjects = [str(item["subject"]) for item in members]
                open_members = [item for item in members if item["status"] != "resolved"]
                if open_members:
                    if any(item["status"] == "escalated" for item in open_members):
                        status_value = "escalated"
                    elif any(item["status"] == "confirmed" for item in open_members):
                        status_value = "confirmed"
                    else:
                        status_value = "observed"
                    resolved_epoch: int | None = None
                else:
                    status_value = "resolved"
                    resolved_epoch = max(int(item["resolved_epoch"]) for item in members)
                    resolved += 1
                confirmations = [
                    int(item["confirmed_epoch"])
                    for item in members
                    if item["confirmed_epoch"] is not None
                ]
                priority = {"binary_sensor": 0, "sensor": 1, "light": 2, "switch": 3}
                representative = min(
                    subjects,
                    key=lambda value: (priority.get(value.split(".", 1)[0], 9), len(value), value),
                )
                self.connection.execute(
                    """
                    UPDATE device_incidents
                    SET representative_subject=?,display_name=?,status=?,
                        first_observed_epoch=?,last_observed_epoch=?,
                        confirmed_epoch=?,resolved_epoch=?,severity=?,
                        safety_class=?,evidence_json=?
                    WHERE id=?
                    """,
                    (
                        representative,
                        _device_display_name(subjects),
                        status_value,
                        min(int(item["first_observed_epoch"]) for item in members),
                        max(int(item["last_observed_epoch"]) for item in members),
                        min(confirmations) if confirmations else None,
                        resolved_epoch,
                        "critical" if any(item["severity"] == "critical" for item in members) else "warning",
                        _device_safety_class(subjects),
                        _json({
                            "active_members": len(open_members),
                            "member_count": len(members),
                        }),
                        device_incident_id,
                    ),
                )
        return {"created": created, "updated": len(touched), "resolved": resolved}

    def _split_announced_reopenings(self) -> int:
        """Repair old rollups that reopened after a delivered recovery notice.

        Older runtimes correlated a fresh entity outage back into a recently
        resolved physical-device incident for up to three minutes.  Correlation
        is useful before recovery is announced, but after that announcement the
        next outage is a new owner-visible episode and needs fresh deduplication
        state.  This repair is idempotent and touches only already-correlated
        private incident rows.
        """
        rows = self.connection.execute(
            """
            SELECT d.*,MAX(COALESCE(n.accepted_epoch,n.last_attempt_epoch))
                       AS recovery_notice_epoch
            FROM device_incidents AS d
            JOIN device_incident_notifications AS n
              ON n.device_incident_id=d.id
            WHERE d.status IN ('observed','confirmed','escalated')
              AND n.phase='resolved'
              AND n.status IN ('accepted','delivery_unknown')
            GROUP BY d.id
            ORDER BY d.id
            """
        ).fetchall()
        created = 0
        priority = {"binary_sensor": 0, "sensor": 1, "light": 2, "switch": 3}
        with self.connection:
            for device in rows:
                split_epoch = int(device["recovery_notice_epoch"])
                members = self.connection.execute(
                    """
                    SELECT i.id,i.subject,i.status,i.first_observed_epoch,
                           i.last_observed_epoch,i.confirmed_epoch,i.resolved_epoch,
                           i.severity
                    FROM device_incident_members AS m
                    JOIN incidents AS i ON i.id=m.entity_incident_id
                    WHERE m.device_incident_id=?
                    ORDER BY i.id
                    """,
                    (int(device["id"]),),
                ).fetchall()
                earlier = [
                    item for item in members
                    if int(item["first_observed_epoch"]) <= split_epoch
                ]
                later = [
                    item for item in members
                    if int(item["first_observed_epoch"]) > split_epoch
                ]
                if (
                    not earlier
                    or not later
                    or any(item["status"] != "resolved" for item in earlier)
                    or not any(item["status"] != "resolved" for item in later)
                ):
                    continue

                def aggregate(
                    items: list[sqlite3.Row],
                ) -> tuple[str, str, str, int, int, int | None, int | None, str, str]:
                    subjects = [str(item["subject"]) for item in items]
                    open_items = [item for item in items if item["status"] != "resolved"]
                    if open_items:
                        if any(item["status"] == "escalated" for item in open_items):
                            status_value = "escalated"
                        elif any(item["status"] == "confirmed" for item in open_items):
                            status_value = "confirmed"
                        else:
                            status_value = "observed"
                        resolved_epoch: int | None = None
                    else:
                        status_value = "resolved"
                        resolved_epoch = max(
                            int(item["resolved_epoch"]) for item in items
                        )
                    confirmations = [
                        int(item["confirmed_epoch"])
                        for item in items if item["confirmed_epoch"] is not None
                    ]
                    representative = min(
                        subjects,
                        key=lambda value: (
                            priority.get(value.split(".", 1)[0], 9),
                            len(value),
                            value,
                        ),
                    )
                    return (
                        representative,
                        _device_display_name(subjects),
                        status_value,
                        min(int(item["first_observed_epoch"]) for item in items),
                        max(int(item["last_observed_epoch"]) for item in items),
                        min(confirmations) if confirmations else None,
                        resolved_epoch,
                        "critical" if any(
                            item["severity"] == "critical" for item in items
                        ) else "warning",
                        _device_safety_class(subjects),
                    )

                old_values = aggregate(earlier)
                self.connection.execute(
                    """
                    UPDATE device_incidents
                    SET representative_subject=?,display_name=?,status=?,
                        first_observed_epoch=?,last_observed_epoch=?,
                        confirmed_epoch=?,resolved_epoch=?,severity=?,
                        safety_class=?,evidence_json=?
                    WHERE id=?
                    """,
                    (*old_values, _json({
                        "active_members": 0,
                        "member_count": len(earlier),
                        "repair": "announced_reopening_split",
                    }), int(device["id"])),
                )
                new_values = aggregate(later)
                cursor = self.connection.execute(
                    """
                    INSERT INTO device_incidents(
                        physical_device_hash,representative_subject,display_name,
                        status,first_observed_epoch,last_observed_epoch,
                        confirmed_epoch,resolved_epoch,severity,cause_code,
                        cause_confidence,safety_class,baseline,evidence_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?)
                    """,
                    (
                        str(device["physical_device_hash"]),
                        *new_values[:8],
                        str(device["cause_code"]),
                        str(device["cause_confidence"]),
                        new_values[8],
                        _json({
                            "active_members": sum(
                                item["status"] != "resolved" for item in later
                            ),
                            "member_count": len(later),
                            "repair": "announced_reopening_split",
                        }),
                    ),
                )
                new_id = int(cursor.lastrowid)
                self.connection.executemany(
                    """
                    UPDATE device_incident_members SET device_incident_id=?
                    WHERE device_incident_id=? AND entity_incident_id=?
                    """,
                    (
                        (new_id, int(device["id"]), int(item["id"]))
                        for item in later
                    ),
                )
                created += 1
        return created

    def record_voice_intent(
        self,
        *,
        action_id: str,
        route_id: str,
        action_kind: str,
        speaker_entity_id: str,
        status: str,
        attempted_epoch: int,
        control_service_calls: int,
        tts_service_calls: int,
    ) -> None:
        """Persist only bounded route metadata, never the spoken phrase."""
        if (
            not re.fullmatch(r"[a-f0-9]{32}", action_id)
            or not re.fullmatch(r"[a-z0-9_]{1,64}", route_id)
            or action_kind not in {"status", "incidents", "control"}
            or status not in {"accepted", "completed", "failed"}
            or attempted_epoch < 0
            or control_service_calls not in {0, 1}
            or tts_service_calls not in {0, 1}
        ):
            raise MonitorError("invalid voice intent result")
        speaker = _validate_subject(speaker_entity_id, "entity")
        if not speaker.startswith("media_player."):
            raise MonitorError("invalid voice intent speaker")
        current = self.connection.execute(
            "SELECT route_id,action_kind,speaker_entity_id,attempted_epoch "
            "FROM voice_intent_actions WHERE id=?",
            (action_id,),
        ).fetchone()
        if current is not None and (
            str(current["route_id"]) != route_id
            or str(current["action_kind"]) != action_kind
            or str(current["speaker_entity_id"]) != speaker
            or int(current["attempted_epoch"]) != attempted_epoch
        ):
            raise MonitorError("voice intent identity changed")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO voice_intent_actions(
                    id,route_id,action_kind,speaker_entity_id,status,
                    attempted_epoch,completed_epoch,control_service_calls,tts_service_calls
                ) VALUES(?,?,?,?,?,?,?, ?,?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    completed_epoch=excluded.completed_epoch,
                    control_service_calls=excluded.control_service_calls,
                    tts_service_calls=excluded.tts_service_calls
                """,
                (
                    action_id, route_id, action_kind, speaker, status,
                    attempted_epoch,
                    attempted_epoch if status in {"completed", "failed"} else None,
                    control_service_calls, tts_service_calls,
                ),
            )

    def _open_incident(self, subject: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM incidents WHERE subject=? AND status IN ('observed','confirmed','escalated')",
            (subject,),
        ).fetchone()

    def observe(
        self,
        subject: str,
        kind: str,
        state: str,
        observed_epoch: int,
        *,
        unavailable: bool,
        source: str,
    ) -> dict[str, object] | None:
        subject = _validate_subject(subject, kind)
        if not isinstance(observed_epoch, int) or observed_epoch < 0:
            raise MonitorError("invalid incident time")
        safe_state = state if state in SAFE_INCIDENT_STATES else "available"
        evidence = _json({"source": source, "state": safe_state})
        with self.connection:
            current = self._open_incident(subject)
            if unavailable:
                severity = "critical" if kind == "system" else "warning"
                if current is None:
                    cursor = self.connection.execute(
                        """
                        INSERT INTO incidents(
                            subject,kind,status,first_observed_epoch,last_observed_epoch,
                            confirmed_epoch,resolved_epoch,occurrences,last_state,severity,
                            action_mode,actions_attempted,evidence_json,baseline
                        ) VALUES(?,?,?,?,?,NULL,NULL,?,?,?,?,0,?,?)
                        """,
                        (
                            subject, kind, "observed", observed_epoch, observed_epoch,
                            1, safe_state, severity, "monitor_only", evidence,
                            int(source == "startup_snapshot"),
                        ),
                    )
                    incident_id = int(cursor.lastrowid)
                    self.connection.execute(
                        "INSERT INTO incident_events(incident_id,event_type,observed_epoch,evidence_json) VALUES(?,?,?,?)",
                        (incident_id, "observed", observed_epoch, evidence),
                    )
                    return {"event": "observed", "incident_id": incident_id, "subject": subject}
                self.connection.execute(
                    """
                    UPDATE incidents
                    SET last_observed_epoch=?,occurrences=occurrences+1,last_state=?,evidence_json=?
                    WHERE id=?
                    """,
                    (observed_epoch, safe_state, evidence, current["id"]),
                )
                return None
            if current is None:
                return None
            self.connection.execute(
                """
                UPDATE incidents
                SET status='resolved',last_observed_epoch=?,resolved_epoch=?,last_state=?,evidence_json=?
                WHERE id=?
                """,
                (observed_epoch, observed_epoch, safe_state, evidence, current["id"]),
            )
            self.connection.execute(
                "INSERT INTO incident_events(incident_id,event_type,observed_epoch,evidence_json) VALUES(?,?,?,?)",
                (current["id"], "resolved", observed_epoch, evidence),
            )
            return {"event": "resolved", "incident_id": int(current["id"]), "subject": subject}

    def confirm_due(self, observed_epoch: int, after_seconds: int) -> list[dict[str, object]]:
        if observed_epoch < 0 or not 1 <= after_seconds <= 86_400:
            raise MonitorError("invalid confirmation window")
        threshold = observed_epoch - after_seconds
        due = self.connection.execute(
            "SELECT id,subject FROM incidents WHERE status='observed' AND first_observed_epoch<=?",
            (threshold,),
        ).fetchall()
        confirmed: list[dict[str, object]] = []
        with self.connection:
            for row in due:
                evidence = _json({"source": "debounce", "still_unavailable": True})
                self.connection.execute(
                    "UPDATE incidents SET status='confirmed',confirmed_epoch=?,evidence_json=? WHERE id=?",
                    (observed_epoch, evidence, row["id"]),
                )
                self.connection.execute(
                    "INSERT INTO incident_events(incident_id,event_type,observed_epoch,evidence_json) VALUES(?,?,?,?)",
                    (row["id"], "confirmed", observed_epoch, evidence),
                )
                confirmed.append(
                    {"event": "confirmed", "incident_id": int(row["id"]), "subject": row["subject"]}
                )
        return confirmed

    def summary(self) -> dict[str, object]:
        counts = {"observed": 0, "confirmed": 0, "resolved": 0, "escalated": 0}
        for row in self.connection.execute("SELECT status,COUNT(*) AS amount FROM incidents GROUP BY status"):
            counts[str(row["status"])] = int(row["amount"])
        latest = [
            {
                "id": int(row["id"]),
                "subject": row["subject"],
                "status": row["status"],
                "severity": row["severity"],
                "last_state": row["last_state"],
                "actions_attempted": int(row["actions_attempted"]),
                "baseline": bool(row["baseline"]),
            }
            for row in self.connection.execute(
                "SELECT id,subject,status,severity,last_state,actions_attempted,baseline FROM incidents ORDER BY id DESC LIMIT 20"
            )
        ]
        return {"schema_version": 1, "mode": "monitor_only", "counts": counts, "latest": latest}

    def notification_candidates(
        self,
        observed_epoch: int,
        *,
        retry_seconds: int = 300,
        max_attempts: int = 3,
        sensor_after_seconds: int = SENSOR_NOTIFICATION_AFTER_SECONDS,
        include_sensor: bool = True,
    ) -> list[dict[str, object]]:
        if (
            observed_epoch < 0
            or retry_seconds < 1
            or max_attempts < 1
            or not 120 <= sensor_after_seconds <= 180
        ):
            raise MonitorError("invalid notification policy")
        policy = self.connection.execute(
            "SELECT enabled_epoch FROM notification_policies WHERE name=?",
            (SENSOR_NOTIFICATION_POLICY,),
        ).fetchone()
        if policy is None:
            raise MonitorError("sensor notification policy is unavailable")
        sensor_enabled_epoch = int(policy["enabled_epoch"])
        rows = self.connection.execute(
            """
            SELECT i.id,i.subject,i.kind,i.status,i.first_observed_epoch,
                   i.confirmed_epoch,i.resolved_epoch,i.severity,i.baseline,
                   n.phase,n.status AS notification_status,n.attempts,n.last_attempt_epoch
            FROM incidents AS i
            LEFT JOIN incident_notifications AS n ON n.incident_id=i.id
            WHERE i.confirmed_epoch IS NOT NULL AND (
                i.severity='critical' OR (?=1 AND (
                    i.severity='warning' AND i.kind='entity' AND i.baseline=0
                    AND (i.subject GLOB 'sensor.*' OR i.subject GLOB 'binary_sensor.*')
                    AND i.first_observed_epoch>=?
                ))
            )
            ORDER BY i.id,n.id
            """,
            (int(include_sensor), sensor_enabled_epoch),
        ).fetchall()
        candidates: list[dict[str, object]] = []
        by_incident: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            by_incident.setdefault(int(row["id"]), []).append(row)
        for incident_id, incident_rows in by_incident.items():
            first = incident_rows[0]
            existing = {
                str(row["phase"]): row
                for row in incident_rows
                if row["phase"] is not None
            }
            notification_kind = (
                "sensor" if first["severity"] == "warning" else "critical"
            )
            if notification_kind == "sensor":
                confirmed_notice = existing.get("confirmed")
                if (
                    first["status"] in {"confirmed", "escalated"}
                    and int(first["first_observed_epoch"])
                    <= observed_epoch - sensor_after_seconds
                ):
                    phases = ["confirmed"]
                elif (
                    first["status"] == "resolved"
                    and first["resolved_epoch"] is not None
                    and confirmed_notice is not None
                    and confirmed_notice["notification_status"] == "accepted"
                ):
                    phases = ["resolved"]
                else:
                    phases = []
            else:
                phases = ["confirmed"]
                if first["status"] == "resolved" and first["resolved_epoch"] is not None:
                    phases.append("resolved")
            for phase in phases:
                prior = existing.get(phase)
                if prior is not None:
                    if prior["notification_status"] in {"accepted", "abandoned"}:
                        continue
                    attempts = int(prior["attempts"])
                    if attempts >= max_attempts:
                        continue
                    if observed_epoch - int(prior["last_attempt_epoch"]) < retry_seconds:
                        continue
                candidates.append(
                    {
                        "incident_id": incident_id,
                        "subject": first["subject"],
                        "phase": phase,
                        "notification_kind": notification_kind,
                    }
                )
        return candidates

    def device_notification_candidates(
        self,
        observed_epoch: int,
        *,
        retry_seconds: int = 300,
        max_attempts: int = 3,
        sensor_after_seconds: int = DEVICE_NOTIFICATION_AFTER_SECONDS,
    ) -> list[dict[str, object]]:
        """Return one deduplicated notification for every physical device incident."""
        if (
            observed_epoch < 0
            or retry_seconds < 1
            or max_attempts < 1
            or not 15 <= sensor_after_seconds <= 180
        ):
            raise MonitorError("invalid device notification policy")
        policy = self.connection.execute(
            "SELECT enabled_epoch FROM notification_policies WHERE name=?",
            (DEVICE_NOTIFICATION_POLICY,),
        ).fetchone()
        if policy is None:
            raise MonitorError("device notification policy is unavailable")
        rows = self.connection.execute(
            """
            SELECT d.id,d.representative_subject,d.display_name,d.status,
                   d.first_observed_epoch,d.confirmed_epoch,d.resolved_epoch,
                   d.cause_code,d.cause_confidence,d.safety_class,
                   n.phase,n.status AS notification_status,n.attempts,
                   n.last_attempt_epoch,
                   (SELECT COUNT(*) FROM device_incident_members AS members
                    WHERE members.device_incident_id=d.id) AS member_count
            FROM device_incidents AS d
            LEFT JOIN device_incident_notifications AS n
              ON n.device_incident_id=d.id
            WHERE d.baseline=0 AND d.confirmed_epoch IS NOT NULL
              AND d.first_observed_epoch>=?
            ORDER BY d.id,n.id
            """,
            (int(policy["enabled_epoch"]),),
        ).fetchall()
        by_incident: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            by_incident.setdefault(int(row["id"]), []).append(row)
        candidates: list[dict[str, object]] = []
        for incident_id, incident_rows in by_incident.items():
            first = incident_rows[0]
            existing = {
                str(row["phase"]): row
                for row in incident_rows
                if row["phase"] is not None
            }
            confirmed_notice = existing.get("confirmed")
            if (
                first["status"] in {"confirmed", "escalated"}
                and int(first["first_observed_epoch"])
                <= observed_epoch - sensor_after_seconds
            ):
                phases = ["confirmed"]
            elif (
                first["status"] == "resolved"
                and first["resolved_epoch"] is not None
                and confirmed_notice is not None
                and confirmed_notice["notification_status"] == "accepted"
            ):
                phases = ["resolved"]
            else:
                phases = []
            for phase in phases:
                prior = existing.get(phase)
                if prior is not None:
                    if prior["notification_status"] in {
                        "accepted", "abandoned", "delivery_unknown"
                    }:
                        continue
                    attempts = int(prior["attempts"])
                    if attempts >= max_attempts:
                        continue
                    if observed_epoch - int(prior["last_attempt_epoch"]) < retry_seconds:
                        continue
                candidates.append({
                    "device_incident_id": incident_id,
                    "subject": str(first["representative_subject"]),
                    "display_name": str(first["display_name"]),
                    "phase": phase,
                    "notification_kind": "device",
                    "cause_code": str(first["cause_code"]),
                    "cause_confidence": str(first["cause_confidence"]),
                    "safety_class": str(first["safety_class"]),
                    "member_count": int(first["member_count"]),
                    "first_observed_epoch": int(first["first_observed_epoch"]),
                    "resolved_epoch": (
                        int(first["resolved_epoch"])
                        if first["resolved_epoch"] is not None
                        else None
                    ),
                })
        return candidates

    def record_device_notification(
        self,
        device_incident_id: int,
        phase: str,
        observed_epoch: int,
        *,
        status: str,
        speaker_entity_id: str | None,
        max_attempts: int = 3,
    ) -> None:
        if (
            phase not in {"confirmed", "resolved"}
            or status not in {"failed", "accepted", "delivery_unknown"}
            or observed_epoch < 0
            or max_attempts < 1
        ):
            raise MonitorError("invalid device notification result")
        current = self.connection.execute(
            "SELECT attempts FROM device_incident_notifications "
            "WHERE device_incident_id=? AND phase=?",
            (device_incident_id, phase),
        ).fetchone()
        attempts = (int(current["attempts"]) if current is not None else 0) + 1
        final_status = (
            status
            if status in {"accepted", "delivery_unknown"}
            else "abandoned" if attempts >= max_attempts else "failed"
        )
        if speaker_entity_id is not None:
            _validate_subject(speaker_entity_id, "entity")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO device_incident_notifications(
                    device_incident_id,phase,status,attempts,last_attempt_epoch,
                    accepted_epoch,speaker_entity_id
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(device_incident_id,phase) DO UPDATE SET
                    status=excluded.status,attempts=excluded.attempts,
                    last_attempt_epoch=excluded.last_attempt_epoch,
                    accepted_epoch=excluded.accepted_epoch,
                    speaker_entity_id=excluded.speaker_entity_id
                """,
                (
                    device_incident_id,
                    phase,
                    final_status,
                    attempts,
                    observed_epoch,
                    observed_epoch if status == "accepted" else None,
                    speaker_entity_id,
                ),
            )

    def record_notification(
        self,
        incident_id: int,
        phase: str,
        observed_epoch: int,
        *,
        accepted: bool,
        speaker_entity_id: str | None,
        max_attempts: int = 3,
    ) -> None:
        if phase not in {"confirmed", "resolved"} or observed_epoch < 0:
            raise MonitorError("invalid notification result")
        current = self.connection.execute(
            "SELECT attempts FROM incident_notifications WHERE incident_id=? AND phase=?",
            (incident_id, phase),
        ).fetchone()
        attempts = (int(current["attempts"]) if current is not None else 0) + 1
        status = "accepted" if accepted else "abandoned" if attempts >= max_attempts else "failed"
        if speaker_entity_id is not None:
            _validate_subject(speaker_entity_id, "entity")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO incident_notifications(
                    incident_id,phase,status,attempts,last_attempt_epoch,accepted_epoch,speaker_entity_id
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(incident_id,phase) DO UPDATE SET
                    status=excluded.status,attempts=excluded.attempts,
                    last_attempt_epoch=excluded.last_attempt_epoch,
                    accepted_epoch=excluded.accepted_epoch,
                    speaker_entity_id=excluded.speaker_entity_id
                """,
                (
                    incident_id, phase, status, attempts, observed_epoch,
                    observed_epoch if accepted else None, speaker_entity_id,
                ),
            )

    def recovery_candidates(self) -> list[dict[str, object]]:
        return [
            {
                "incident_id": int(row["id"]),
                "subject": row["subject"],
            }
            for row in self.connection.execute(
                """
                SELECT i.id,i.subject
                FROM incidents AS i
                LEFT JOIN recovery_actions AS r ON r.incident_id=i.id
                WHERE i.status='confirmed' AND i.baseline=0
                  AND i.kind='entity' AND r.id IS NULL
                ORDER BY i.id
                """
            )
        ]

    def last_recovery_epoch(self, integration: str, action: str) -> int | None:
        row = self.connection.execute(
            "SELECT MAX(attempted_epoch) AS latest FROM recovery_actions WHERE integration=? AND action=?",
            (integration, action),
        ).fetchone()
        return int(row["latest"]) if row is not None and row["latest"] is not None else None

    def record_recovery(
        self,
        *,
        incident_id: int,
        action_group_id: str,
        integration: str,
        action: str,
        status: str,
        attempted_epoch: int,
        service_calls: int,
        before_state: str,
        after_state: str,
        verification_checks: int = 0,
    ) -> None:
        if (
            status not in {"failed", "accepted", "delivery_unknown", "verified"}
            or service_calls not in {0, 1}
            or verification_checks not in {0, 1, 2, 3}
            or attempted_epoch < 0
            or not re.fullmatch(r"[a-f0-9]{32}", action_group_id)
            or not re.fullmatch(r"[a-z0-9_]{1,64}", integration)
            or (integration, action) not in {
                ("localtuya", "localtuya.reload"),
                ("tuya_local", "homeassistant.reload_config_entry"),
                ("xiaomi_miot", "homeassistant.reload_config_entry"),
            }
            or before_state not in {"unavailable", "unknown"}
            or after_state not in {"unavailable", "available", "unknown"}
        ):
            raise MonitorError("invalid recovery result")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO recovery_actions(
                    incident_id,action_group_id,integration,action,status,
                    attempted_epoch,service_calls,verification_checks,
                    before_state,after_state
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    incident_id, action_group_id, integration, action, status,
                    attempted_epoch, service_calls, verification_checks,
                    before_state, after_state,
                ),
            )

    def core_recovery_candidate(
        self,
        observed_epoch: int,
        *,
        min_confirmed_seconds: int,
    ) -> dict[str, object] | None:
        if observed_epoch < 0 or min_confirmed_seconds < 1:
            raise MonitorError("invalid core recovery window")
        row = self.connection.execute(
            """
            SELECT i.id,i.subject,i.confirmed_epoch
            FROM incidents AS i
            LEFT JOIN core_recovery_actions AS r ON r.incident_id=i.id
            WHERE i.subject=? AND i.kind='system' AND i.status='confirmed'
              AND i.baseline=0 AND i.confirmed_epoch IS NOT NULL
              AND i.confirmed_epoch<=? AND r.id IS NULL
            ORDER BY i.id
            LIMIT 1
            """,
            (RESERVED_SUBJECT, observed_epoch - min_confirmed_seconds),
        ).fetchone()
        if row is None:
            return None
        return {
            "incident_id": int(row["id"]),
            "subject": str(row["subject"]),
            "confirmed_epoch": int(row["confirmed_epoch"]),
        }

    def last_core_restart_epoch(self) -> int | None:
        row = self.connection.execute(
            "SELECT MAX(attempted_epoch) AS latest FROM core_recovery_actions WHERE restart_calls=1"
        ).fetchone()
        return int(row["latest"]) if row is not None and row["latest"] is not None else None

    def record_core_recovery(
        self,
        *,
        incident_id: int,
        action_group_id: str,
        status: str,
        attempted_epoch: int,
        check_calls: int,
        restart_calls: int,
        after_state: str,
    ) -> None:
        if (
            status not in {
                "check_failed", "check_unknown", "failed", "accepted",
                "delivery_unknown", "verified",
            }
            or attempted_epoch < 0
            or check_calls not in {0, 1}
            or restart_calls not in {0, 1}
            or not re.fullmatch(r"[a-f0-9]{32}", action_group_id)
            or after_state not in {"reachable", "unknown"}
        ):
            raise MonitorError("invalid core recovery result")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO core_recovery_actions(
                    incident_id,action_group_id,status,attempted_epoch,
                    check_calls,restart_calls,after_state
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    incident_id, action_group_id, status, attempted_epoch,
                    check_calls, restart_calls, after_state,
                ),
            )

    def out_of_band_recovery_candidate(
        self,
        observed_epoch: int,
        *,
        min_confirmed_seconds: int,
        retry_seconds: int,
    ) -> dict[str, object] | None:
        if observed_epoch < 0 or min_confirmed_seconds < 1 or retry_seconds < 1:
            raise MonitorError("invalid out-of-band recovery window")
        row = self.connection.execute(
            """
            SELECT i.id,i.subject,i.confirmed_epoch,
                   o.status AS action_status,o.attempts,o.attempted_epoch
            FROM incidents AS i
            LEFT JOIN out_of_band_recovery_actions AS o ON o.incident_id=i.id
            WHERE i.subject=? AND i.kind='system'
              AND i.status IN ('confirmed','escalated')
              AND i.baseline=0 AND i.confirmed_epoch IS NOT NULL
              AND i.confirmed_epoch<=?
              AND (
                    o.id IS NULL
                    OR (o.status='failed' AND o.attempts<3 AND o.attempted_epoch<=?)
                  )
              AND NOT EXISTS (
                    SELECT 1 FROM core_recovery_actions AS c
                    WHERE c.incident_id=i.id AND c.attempted_epoch>?
                  )
            ORDER BY i.id
            LIMIT 1
            """,
            (
                RESERVED_SUBJECT,
                observed_epoch - min_confirmed_seconds,
                observed_epoch - retry_seconds,
                observed_epoch - retry_seconds,
            ),
        ).fetchone()
        if row is None:
            return None
        return {
            "incident_id": int(row["id"]),
            "subject": str(row["subject"]),
            "confirmed_epoch": int(row["confirmed_epoch"]),
            "prior_attempts": int(row["attempts"] or 0),
        }

    def record_out_of_band_recovery(
        self,
        *,
        incident_id: int,
        action_group_id: str,
        status: str,
        attempted_epoch: int,
        ssh_calls: int,
        restart_calls: int,
        after_state: str,
    ) -> None:
        if (
            status not in {"failed", "cooldown", "healthy", "verified"}
            or attempted_epoch < 0
            or ssh_calls not in {0, 1}
            or restart_calls not in {0, 1}
            or not re.fullmatch(r"[a-f0-9]{32}", action_group_id)
            or after_state not in {"reachable", "unknown"}
        ):
            raise MonitorError("invalid out-of-band recovery result")
        current = self.connection.execute(
            "SELECT attempts FROM out_of_band_recovery_actions WHERE incident_id=?",
            (incident_id,),
        ).fetchone()
        attempts = (int(current["attempts"]) if current is not None else 0) + 1
        if attempts > 3:
            raise MonitorError("out-of-band recovery attempt budget exhausted")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO out_of_band_recovery_actions(
                    incident_id,action_group_id,status,attempted_epoch,attempts,
                    ssh_calls,restart_calls,after_state
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(incident_id) DO UPDATE SET
                    action_group_id=excluded.action_group_id,
                    status=excluded.status,
                    attempted_epoch=excluded.attempted_epoch,
                    attempts=excluded.attempts,
                    ssh_calls=excluded.ssh_calls,
                    restart_calls=excluded.restart_calls,
                    after_state=excluded.after_state
                """,
                (
                    incident_id, action_group_id, status, attempted_epoch, attempts,
                    ssh_calls, restart_calls, after_state,
                ),
            )

    def record_network_bindings(
        self,
        bindings: list[dict[str, object]],
        observed_epoch: int,
    ) -> dict[str, int]:
        if observed_epoch < 0 or len(bindings) > 1_024:
            raise MonitorError("invalid network identity observations")
        counts = {"observed": 0, "events": 0, "ip_changed": 0, "converged": 0}
        with self.connection:
            for item in bindings:
                if not isinstance(item, dict):
                    raise MonitorError("invalid network identity observation")
                identity_hash = item.get("identity_hash")
                platform = item.get("platform")
                device_id = item.get("device_id")
                entry_id = item.get("config_entry_id")
                configured_ip = item.get("configured_ip")
                observed_ip = item.get("observed_ip")
                mac = item.get("mac")
                status_value = item.get("status")
                if (
                    not isinstance(identity_hash, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", identity_hash)
                    or platform not in {"localtuya", "tuya_local", "xiaomi_miot"}
                    or not isinstance(device_id, str)
                    or not re.fullmatch(r"[a-f0-9]{32}", device_id)
                    or not isinstance(entry_id, str)
                    or not re.fullmatch(r"(?:[A-Z0-9]{26}|[a-f0-9]{32})", entry_id)
                    or status_value not in {"stable", "ip_changed", "not_observed"}
                    or mac is not None
                    and (not isinstance(mac, str) or not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", mac))
                ):
                    raise MonitorError("invalid network identity observation")
                try:
                    configured_address = ipaddress.ip_address(configured_ip)
                    observed_address = (
                        ipaddress.ip_address(observed_ip) if observed_ip is not None else None
                    )
                except ValueError as error:
                    raise MonitorError("invalid network identity observation") from error
                local_network = ipaddress.ip_network("192.168.1.0/24")
                if (
                    configured_address not in local_network
                    or observed_address is not None and observed_address not in local_network
                    or status_value == "stable" and observed_ip != configured_ip
                    or status_value == "ip_changed" and (
                        observed_ip is None or observed_ip == configured_ip or mac is None
                    )
                ):
                    raise MonitorError("invalid network identity observation")
                prior = self.connection.execute(
                    "SELECT status,observed_ip,mac FROM network_identity_observations WHERE identity_hash=?",
                    (identity_hash,),
                ).fetchone()
                event_type: str | None = None
                if prior is None:
                    event_type = "bound" if status_value == "stable" else str(status_value)
                elif status_value == "ip_changed" and (
                    prior["status"] != "ip_changed" or prior["observed_ip"] != observed_ip
                ):
                    event_type = "ip_changed"
                elif status_value == "stable" and prior["status"] == "ip_changed":
                    event_type = "converged"
                elif status_value == "not_observed" and prior["status"] != "not_observed":
                    event_type = "not_observed"
                first_observed = observed_epoch if prior is None else None
                self.connection.execute(
                    """
                    INSERT INTO network_identity_observations(
                        identity_hash,platform,device_id,config_entry_id,configured_ip,
                        observed_ip,mac,status,first_observed_epoch,last_observed_epoch,change_count
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(identity_hash) DO UPDATE SET
                        platform=excluded.platform,device_id=excluded.device_id,
                        config_entry_id=excluded.config_entry_id,
                        configured_ip=excluded.configured_ip,observed_ip=excluded.observed_ip,
                        mac=COALESCE(excluded.mac,network_identity_observations.mac),
                        status=excluded.status,last_observed_epoch=excluded.last_observed_epoch,
                        change_count=network_identity_observations.change_count + ?
                    """,
                    (
                        identity_hash, platform, device_id, entry_id,
                        str(configured_address), str(observed_address) if observed_address else None,
                        mac, status_value,
                        first_observed if first_observed is not None else observed_epoch,
                        observed_epoch, int(event_type in {"ip_changed", "converged"}),
                        int(event_type in {"ip_changed", "converged"}),
                    ),
                )
                if event_type is not None:
                    self.connection.execute(
                        """
                        INSERT INTO network_identity_events(
                            identity_hash,event_type,observed_epoch,configured_ip,observed_ip,mac
                        ) VALUES(?,?,?,?,?,?)
                        """,
                        (
                            identity_hash, event_type, observed_epoch,
                            str(configured_address),
                            str(observed_address) if observed_address else None,
                            mac,
                        ),
                    )
                    counts["events"] += 1
                    counts["ip_changed"] += int(event_type == "ip_changed")
                    counts["converged"] += int(event_type == "converged")
                counts["observed"] += 1
        return counts

    def record_device_network_bindings(
        self,
        bindings: list[dict[str, object]],
        observed_epoch: int,
    ) -> dict[str, int]:
        """Persist generic HA-device/MAC health without exposing it to the model."""
        if observed_epoch < 0 or len(bindings) > 4_096:
            raise MonitorError("invalid device network observations")
        counts = {
            "observed": 0, "events": 0, "ip_changed": 0,
            "returned": 0, "not_observed": 0,
        }
        local_network = ipaddress.ip_network("192.168.1.0/24")
        with self.connection:
            for item in bindings:
                if not isinstance(item, dict):
                    raise MonitorError("invalid device network observation")
                physical_hash = item.get("physical_device_hash")
                device_ids = item.get("device_ids")
                entry_ids = item.get("config_entry_ids")
                mac = item.get("mac")
                observed_ip = item.get("observed_ip")
                previous_ip = item.get("previous_ip")
                status_value = item.get("status")
                if (
                    not isinstance(physical_hash, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", physical_hash)
                    or not isinstance(device_ids, list)
                    or not 1 <= len(device_ids) <= 32
                    or any(
                        not isinstance(value, str)
                        or not re.fullmatch(r"[a-f0-9]{32}", value)
                        for value in device_ids
                    )
                    or not isinstance(entry_ids, list)
                    or len(entry_ids) > 32
                    or any(
                        not isinstance(value, str)
                        or not re.fullmatch(
                            r"(?:[A-Z0-9]{26}|[a-f0-9]{32})", value
                        )
                        for value in entry_ids
                    )
                    or not isinstance(mac, str)
                    or not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", mac)
                    or status_value not in {
                        "stable", "ip_changed", "not_observed"
                    }
                ):
                    raise MonitorError("invalid device network observation")
                try:
                    observed_address = (
                        ipaddress.ip_address(observed_ip)
                        if observed_ip is not None else None
                    )
                    previous_address = (
                        ipaddress.ip_address(previous_ip)
                        if previous_ip is not None else None
                    )
                except ValueError as error:
                    raise MonitorError(
                        "invalid device network observation"
                    ) from error
                if (
                    observed_address is not None
                    and observed_address not in local_network
                    or previous_address is not None
                    and previous_address not in local_network
                    or status_value == "not_observed"
                    and observed_address is not None
                    or status_value in {"stable", "ip_changed"}
                    and observed_address is None
                    or status_value == "ip_changed"
                    and previous_address == observed_address
                ):
                    raise MonitorError("invalid device network observation")
                prior = self.connection.execute(
                    "SELECT status,observed_ip,last_known_ip FROM "
                    "device_network_observations WHERE physical_device_hash=?",
                    (physical_hash,),
                ).fetchone()
                event_type: str | None = None
                if prior is None:
                    event_type = (
                        "bound" if status_value == "stable" else str(status_value)
                    )
                elif status_value == "ip_changed" and (
                    prior["status"] != "ip_changed"
                    or prior["observed_ip"] != observed_ip
                ):
                    event_type = "ip_changed"
                elif status_value == "not_observed" and prior["status"] != "not_observed":
                    event_type = "not_observed"
                elif status_value == "stable" and prior["status"] == "not_observed":
                    event_type = "returned"
                last_known_ip = (
                    str(observed_address)
                    if observed_address is not None
                    else str(previous_address)
                    if previous_address is not None
                    else str(prior["last_known_ip"])
                    if prior is not None and prior["last_known_ip"] is not None
                    else None
                )
                self.connection.execute(
                    """
                    INSERT INTO device_network_observations(
                        physical_device_hash,device_ids_json,
                        config_entry_ids_json,mac,observed_ip,last_known_ip,
                        status,first_observed_epoch,last_observed_epoch,change_count
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(physical_device_hash) DO UPDATE SET
                        device_ids_json=excluded.device_ids_json,
                        config_entry_ids_json=excluded.config_entry_ids_json,
                        mac=excluded.mac,observed_ip=excluded.observed_ip,
                        last_known_ip=COALESCE(
                            excluded.last_known_ip,
                            device_network_observations.last_known_ip
                        ),status=excluded.status,
                        last_observed_epoch=excluded.last_observed_epoch,
                        change_count=device_network_observations.change_count + ?
                    """,
                    (
                        physical_hash, _json(sorted(set(device_ids))),
                        _json(sorted(set(entry_ids))), mac,
                        str(observed_address) if observed_address else None,
                        last_known_ip, status_value, observed_epoch,
                        observed_epoch, int(event_type in {
                            "ip_changed", "returned", "not_observed"
                        }),
                        int(event_type in {
                            "ip_changed", "returned", "not_observed"
                        }),
                    ),
                )
                if event_type is not None:
                    evidence = _json({
                        "network_status": status_value,
                        "ip_changed": event_type == "ip_changed",
                    })
                    self.connection.execute(
                        """
                        INSERT INTO device_network_events(
                            physical_device_hash,event_type,observed_epoch,
                            status,evidence_json
                        ) VALUES(?,?,?,?,?)
                        """,
                        (
                            physical_hash, event_type, observed_epoch,
                            status_value, evidence,
                        ),
                    )
                    counts["events"] += 1
                    if event_type in counts:
                        counts[event_type] += 1
                counts["observed"] += 1
        return counts


def _message(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise MonitorError("invalid websocket message")
    encoded = raw.encode("utf-8", errors="strict")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise MonitorError("websocket message too large")
    parsed = ha_read.strict_json_loads(encoded)
    if not isinstance(parsed, dict):
        raise MonitorError("invalid websocket message")
    return parsed


def authenticate(socket: Any, token: str) -> None:
    required = _message(socket.recv())
    if required.get("type") != "auth_required":
        raise MonitorError("websocket authentication protocol failed")
    socket.send(_json({"type": "auth", "access_token": token}))
    authenticated = _message(socket.recv())
    if authenticated.get("type") != "auth_ok":
        raise MonitorError("websocket authentication failed")


def authenticate_and_subscribe(socket: Any, token: str) -> None:
    authenticate(socket, token)
    socket.send(_json({"id": 1, "type": "subscribe_events", "event_type": "state_changed"}))
    subscribed = _message(socket.recv())
    if subscribed.get("type") != "result" or subscribed.get("id") != 1 or subscribed.get("success") is not True:
        raise MonitorError("websocket subscription failed")
    socket.send(_json({"id": 2, "type": "subscribe_events", "event_type": "call_service"}))
    subscribed = _message(socket.recv())
    if subscribed.get("type") != "result" or subscribed.get("id") != 2 or subscribed.get("success") is not True:
        raise MonitorError("websocket subscription failed")


def process_state_message(
    document: dict[str, Any], store: IncidentStore, observed_epoch: int
) -> dict[str, object] | None:
    if document.get("type") != "event":
        return None
    event = document.get("event")
    if not isinstance(event, dict) or event.get("event_type") != "state_changed":
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        raise MonitorError("invalid state event")
    entity_id = data.get("entity_id")
    new_state = data.get("new_state")
    if not isinstance(entity_id, str):
        raise MonitorError("invalid state event")
    if new_state is None:
        return None
    if not isinstance(new_state, dict) or not isinstance(new_state.get("state"), str):
        raise MonitorError("invalid state event")
    state = new_state["state"]
    return store.observe(
        entity_id, "entity", state, observed_epoch,
        unavailable=state in BAD_ENTITY_STATES, source="websocket",
    )


def process_service_call_message(
    document: dict[str, Any], store: IncidentStore, observed_epoch: int
) -> dict[str, object] | None:
    """Reduce a call_service event to routing facts; never treat it as success."""
    if document.get("type") != "event":
        return None
    event = document.get("event")
    if not isinstance(event, dict) or event.get("event_type") != "call_service":
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        raise MonitorError("invalid service call event")
    domain = data.get("domain")
    service = data.get("service")
    if (
        not isinstance(domain, str)
        or not re.fullmatch(r"[a-z0-9_]{1,64}", domain)
        or not isinstance(service, str)
        or not re.fullmatch(r"[a-z0-9_]{1,64}", service)
    ):
        raise MonitorError("invalid service call event")
    service_data = data.get("service_data")
    raw_entities = (
        service_data.get("entity_id") if isinstance(service_data, dict) else None
    )
    if isinstance(raw_entities, str):
        candidates = [raw_entities]
    elif isinstance(raw_entities, list) and len(raw_entities) <= 64:
        candidates = raw_entities
    else:
        candidates = []
    entity_ids: list[str] = []
    for candidate in candidates:
        try:
            entity_ids.append(ha_read._validate_entity_id(candidate))
        except ha_read.AdapterError:
            continue
    context = event.get("context")
    context_id = context.get("id") if isinstance(context, dict) else None
    context_hash = None
    if isinstance(context_id, str) and len(context_id) <= 256:
        context_hash = hashlib.sha256(context_id.encode("utf-8")).hexdigest()
    seed = _json({
        "context_hash": context_hash,
        "domain": domain,
        "entity_ids": sorted(set(entity_ids)),
        "observed_epoch": observed_epoch,
        "service": service,
    })
    event_hash = hashlib.sha256(seed.encode("ascii")).hexdigest()
    recorded = store.record_service_call(
        event_hash=event_hash,
        context_hash=context_hash,
        domain=domain,
        service=service,
        entity_ids=entity_ids,
        observed_epoch=observed_epoch,
    )
    return {
        "recorded": recorded,
        "domain": domain,
        "service": service,
        "entity_count": len(set(entity_ids)),
    }


def sync_snapshot(
    store: IncidentStore,
    observed_epoch: int,
    *,
    snapshot_reader: Callable[[str], tuple[dict[str, Any], int]] = ha_read.execute_safely,
) -> int:
    snapshot, exit_code = snapshot_reader("snapshot")
    entities = snapshot.get("entities") if isinstance(snapshot, dict) else None
    if exit_code != 0 or not isinstance(entities, list):
        raise MonitorError("Home Assistant snapshot failed")
    unavailable_count = 0
    for entity in entities:
        if not isinstance(entity, dict) or not isinstance(entity.get("entity_id"), str):
            raise MonitorError("Home Assistant snapshot failed")
        unavailable = entity.get("state_kind") == "unavailable"
        unavailable_count += int(unavailable)
        result = store.observe(
            entity["entity_id"], "entity", "unavailable" if unavailable else "available",
            observed_epoch, unavailable=unavailable, source="startup_snapshot",
        )
        _emit_store_result(result)
    return unavailable_count


def _connect(config: ha_read.AdapterConfig) -> Any:
    if websocket is None:
        raise MonitorError("websocket client is unavailable")
    scheme = "wss" if config.scheme == "https" else "ws"
    url = f"{scheme}://{config.host}:{config.port}/api/websocket"
    try:
        return websocket.create_connection(
            url,
            timeout=10,
            suppress_origin=True,
            http_proxy_host=None,
            http_proxy_port=None,
            http_no_proxy=[config.host],
        )
    except Exception as error:
        raise MonitorError("Home Assistant websocket is unreachable") from error


def _emit_store_result(result: dict[str, object] | None) -> None:
    if result is not None:
        emit(str(result["event"]), incident_id=result["incident_id"], subject=result["subject"])


def run_session(
    socket: Any,
    config: ha_read.AdapterConfig,
    store: IncidentStore,
    *,
    now: Callable[[], float] = time.time,
    confirm_after_seconds: int = CONFIRM_AFTER_SECONDS,
) -> None:
    authenticate_and_subscribe(socket, config.token)
    current = int(now())
    _emit_store_result(
        store.observe(RESERVED_SUBJECT, "system", "reachable", current, unavailable=False, source="websocket")
    )
    unavailable = sync_snapshot(store, current)
    emit("websocket_subscribed", unavailable_entities=unavailable)
    socket.settimeout(SOCKET_TIMEOUT_SECONDS)
    while not STOP_EVENT.is_set():
        try:
            document = _message(socket.recv())
            current = int(now())
            state_result = process_state_message(document, store, current)
            _emit_store_result(state_result)
            if state_result is None:
                service_result = process_service_call_message(
                    document, store, current
                )
                if service_result is not None and service_result["recorded"]:
                    emit(
                        "service_call_observed",
                        domain=service_result["domain"],
                        service=service_result["service"],
                        entity_count=service_result["entity_count"],
                    )
        except Exception as error:
            if websocket is not None and isinstance(error, websocket.WebSocketTimeoutException):
                pass
            else:
                raise MonitorError("Home Assistant websocket disconnected") from error
        for result in store.confirm_due(int(now()), confirm_after_seconds):
            _emit_store_result(result)


def run_forever(store: IncidentStore) -> None:
    config = ha_read.load_config()
    delay = 1
    while not STOP_EVENT.is_set():
        socket = None
        try:
            socket = _connect(config)
            run_session(socket, config, store)
            delay = 1
        except (MonitorError, ha_read.AdapterError):
            current = int(time.time())
            _emit_store_result(
                store.observe(
                    RESERVED_SUBJECT, "system", "unreachable", current,
                    unavailable=True, source="websocket_watchdog",
                )
            )
            for result in store.confirm_due(current, CONFIRM_AFTER_SECONDS):
                _emit_store_result(result)
            emit("websocket_reconnect_wait", seconds=delay)
            STOP_EVENT.wait(delay)
            delay = min(MAX_RECONNECT_SECONDS, delay * 2)
        finally:
            if socket is not None:
                try:
                    socket.close()
                except Exception:
                    pass


def _signal_handler(_signum: int, _frame: object) -> None:
    STOP_EVENT.set()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true")
    arguments = parser.parse_args(argv)
    state_dir = _state_dir()
    try:
        _validate_directory(state_dir)
        store = IncidentStore(state_dir / DATABASE_NAME)
        try:
            if arguments.status:
                print(_json(store.summary()))
                return 0
            signal.signal(signal.SIGTERM, _signal_handler)
            signal.signal(signal.SIGINT, _signal_handler)
            run_forever(store)
            return 0
        finally:
            store.close()
    except (MonitorError, ha_read.AdapterError, OSError, sqlite3.Error):
        print("INCIDENT_MONITOR_FAILED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
