#!/usr/bin/env python3
"""Continuously prove that Home Butler's read-only operational duties are alive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import heartbeat  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402
import incident_status  # noqa: E402
import model_ha_proof  # noqa: E402
import persistent_scheduler  # noqa: E402
from ollama_endpoint import (  # noqa: E402
    EndpointConfigError,
    load_runtime_ollama_endpoint,
)


STATE_DIR = Path("/home/homebutler/.local/state/home-butler")
INCIDENT_DB = STATE_DIR / "incidents" / "incidents.sqlite3"
HEARTBEAT_STATE = STATE_DIR / "heartbeat-state.json"
OPERATIONS_STATE = STATE_DIR / "operations-status.json"
OPERATIONS_JOURNAL = STATE_DIR / "operations-events.jsonl"
DEVICE_HEALTH_MAX_AGE_SECONDS = 45
HEARTBEAT_MAX_AGE_SECONDS = 15 * 60
OPERATIONS_MAX_AGE_SECONDS = 90
MAX_PRIVATE_JSON_BYTES = 65_536
MAX_OPERATIONS_JOURNAL_BYTES = 4 * 1_048_576
REQUIRED_UNITS = (
    "home-butler.service",
    "home-butler-heartbeat.timer",
    "home-butler-incident-monitor.service",
    "home-butler-incident-notifier.timer",
    "home-butler-inventory.timer",
    "home-butler-daily-report.timer",
    "home-butler-device-health.timer",
    "home-butler-model-study.timer",
    "home-butler-diagnostic-monitor.timer",
    "home-butler-startup-voice-status.timer",
    "home-butler-local-chat.service",
)


class SupervisorError(RuntimeError):
    """A bounded, secret-free supervisor failure."""


def _load_private_json(path: Path) -> tuple[dict[str, Any], os.stat_result]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise SupervisorError("private state is unavailable") from error
    expected_owners = {os.geteuid()}
    if path in {HEARTBEAT_STATE, OPERATIONS_STATE}:
        try:
            expected_owners.add(pwd.getpwnam("homebutler").pw_uid)
        except KeyError:
            pass
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in expected_owners
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= MAX_PRIVATE_JSON_BYTES
    ):
        raise SupervisorError("private state is unsafe")
    try:
        document = ha_read.strict_json_loads(path.read_bytes())
    except (OSError, ha_read.AdapterError) as error:
        raise SupervisorError("private state is invalid") from error
    if not isinstance(document, dict):
        raise SupervisorError("private state is invalid")
    return document, metadata


def read_home_assistant() -> dict[str, Any]:
    snapshot, exit_code = ha_read.execute_safely("snapshot")
    status = snapshot.get("status") if isinstance(snapshot, dict) else None
    connected = exit_code == 0 and status in {"healthy", "stale_data"}
    return {
        "connected": connected,
        "status": status if connected else "unavailable",
        "entity_count": (
            snapshot.get("entity_count", 0) if connected else 0
        ),
        "available_entity_count": (
            snapshot.get("available_entity_count", 0) if connected else 0
        ),
        "unavailable_entity_count": (
            snapshot.get("unavailable_entity_count", 0) if connected else 0
        ),
    }


def read_device_health(*, now: int | None = None) -> dict[str, Any]:
    observed_now = int(time.time()) if now is None else now
    incident_status._validate_path(INCIDENT_DB, os.geteuid())
    try:
        connection = sqlite3.connect(
            f"file:{INCIDENT_DB}?mode=ro", uri=True, timeout=3
        )
        try:
            rows = connection.execute(
                "SELECT health_status,COUNT(*) FROM device_health_observations "
                "GROUP BY health_status"
            ).fetchall()
            newest = connection.execute(
                "SELECT MAX(last_observed_epoch) FROM device_health_observations"
            ).fetchone()[0]
            integration_rows = connection.execute(
                "SELECT health_status,COUNT(*) FROM integration_health_observations "
                "GROUP BY health_status"
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as error:
        raise SupervisorError("device health journal is unavailable") from error
    counts = {str(status): int(count) for status, count in rows}
    integration_counts = {
        str(status): int(count) for status, count in integration_rows
    }
    if not isinstance(newest, int) or newest < 0 or newest > observed_now + 5:
        raise SupervisorError("device health journal is invalid")
    age = observed_now - newest
    total = sum(counts.values())
    if total <= 0 or any(value < 0 for value in counts.values()):
        raise SupervisorError("device health journal is invalid")
    return {
        "fresh": age <= DEVICE_HEALTH_MAX_AGE_SECONDS,
        "age_seconds": age,
        "device_count": total,
        "healthy": counts.get("healthy", 0),
        "partial": counts.get("partial", 0),
        "degraded": counts.get("degraded", 0),
        "offline": counts.get("offline", 0),
        "unknown": counts.get("unknown", 0),
        "integration_degraded": integration_counts.get("degraded", 0),
    }


def read_heartbeat(*, now: int | None = None) -> dict[str, Any]:
    observed_now = int(time.time()) if now is None else now
    document, metadata = _load_private_json(HEARTBEAT_STATE)
    if document.get("schema_version") != 2 or document.get("status") not in {
        "ok",
        "attention",
    }:
        raise SupervisorError("heartbeat state is invalid")
    age = observed_now - int(metadata.st_mtime)
    if age < -5:
        raise SupervisorError("heartbeat clock is invalid")
    return {
        "fresh": age <= HEARTBEAT_MAX_AGE_SECONDS,
        "age_seconds": max(0, age),
        "status": document["status"],
    }


def read_daily_report(
    *,
    now: int | None = None,
    localtime: Callable[[float], time.struct_time] = time.localtime,
) -> dict[str, Any]:
    """Read the one persistent scheduler instead of reconstructing a fixed time."""

    observed_now = int(time.time()) if now is None else now
    del localtime  # retained only for public-call compatibility
    try:
        document = persistent_scheduler.read_daily_report_status(now=observed_now)
    except persistent_scheduler.SchedulerError as error:
        raise SupervisorError("scheduler state is unavailable") from error
    state = document.get("state")
    attempts = document.get("attempts")
    next_run = document.get("next_run_epoch")
    last_run = document.get("last_run_epoch")
    verification = document.get("verification")
    valid = (
        state in {"not_due", "running", "retrying", "verified", "missed", "unavailable"}
        and isinstance(attempts, int)
        and not isinstance(attempts, bool)
        and attempts >= 0
        and (
            next_run is None
            or isinstance(next_run, int) and not isinstance(next_run, bool) and next_run >= 0
        )
        and (
            last_run is None
            or isinstance(last_run, int) and not isinstance(last_run, bool) and last_run >= 0
        )
        and isinstance(verification, str)
        and 1 <= len(verification) <= 128
    )
    if not valid:
        raise SupervisorError("scheduler state is invalid")
    return {
        "state": state,
        "verified": state == "verified",
        "attempts": attempts,
        "next_run_epoch": next_run,
        "last_run_epoch": last_run,
        "verification": verification,
    }


def read_model() -> dict[str, Any]:
    endpoint = load_runtime_ollama_endpoint()
    version = model_ha_proof.get_ollama(endpoint, "/api/version")
    reachable = isinstance(version.get("version"), str) and bool(version["version"])
    process = model_ha_proof.get_ollama(endpoint, "/api/ps")
    try:
        evidence = model_ha_proof.gpu_evidence(process)
    except model_ha_proof.ProofError:
        return {"reachable": reachable, "loaded": False, "accelerator": "unknown"}
    return {
        "reachable": reachable,
        "loaded": True,
        "accelerator": "gpu" if evidence["fully_on_gpu"] else "mixed",
    }


def unit_is_active(unit: str) -> bool:
    if unit not in REQUIRED_UNITS:
        raise SupervisorError("unit is outside the fixed allowlist")
    try:
        completed = subprocess.run(
            ["/usr/bin/systemctl", "is-active", "--quiet", "--", unit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SupervisorError("systemd state is unavailable") from error
    return completed.returncode == 0


def _safe_read(reader: Callable[[], dict[str, Any]], fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        return reader()
    except (
        SupervisorError,
        ha_read.AdapterError,
        model_ha_proof.ProofError,
        EndpointConfigError,
        OSError,
        ValueError,
    ):
        return dict(fallback)


def build_status(
    *,
    now: int | None = None,
    ha_reader: Callable[[], dict[str, Any]] = read_home_assistant,
    device_reader: Callable[[], dict[str, Any]] | None = None,
    heartbeat_reader: Callable[[], dict[str, Any]] | None = None,
    daily_reader: Callable[[], dict[str, Any]] | None = None,
    model_reader: Callable[[], dict[str, Any]] = read_model,
    unit_checker: Callable[[str], bool] = unit_is_active,
) -> dict[str, Any]:
    observed_now = int(time.time()) if now is None else now
    devices = _safe_read(
        device_reader or (lambda: read_device_health(now=observed_now)),
        {"fresh": False, "age_seconds": -1, "device_count": 0,
         "healthy": 0, "partial": 0, "degraded": 0, "offline": 0,
         "unknown": 0, "integration_degraded": 0},
    )
    heartbeat_state = _safe_read(
        heartbeat_reader or (lambda: read_heartbeat(now=observed_now)),
        {"fresh": False, "age_seconds": -1, "status": "unavailable"},
    )
    daily = _safe_read(
        daily_reader or (lambda: read_daily_report(now=observed_now)),
        {"state": "unknown", "verified": False, "attempts": 0},
    )
    home_assistant = _safe_read(
        ha_reader,
        {"connected": False, "status": "unavailable", "entity_count": 0,
         "available_entity_count": 0, "unavailable_entity_count": 0},
    )
    model = _safe_read(
        model_reader,
        {"reachable": False, "loaded": False, "accelerator": "unknown"},
    )
    services = {}
    for unit in REQUIRED_UNITS:
        try:
            services[unit] = bool(unit_checker(unit))
        except (SupervisorError, OSError, ValueError):
            services[unit] = False
    attention = (
        not home_assistant.get("connected")
        or not devices.get("fresh")
        or not heartbeat_state.get("fresh")
        or not model.get("reachable")
        or not model.get("loaded")
        or not all(services.values())
        or daily.get("state") in {"missed", "unknown"}
        or devices.get("offline", 0) > 0
        or devices.get("degraded", 0) > 0
        or devices.get("integration_degraded", 0) > 0
    )
    return {
        "schema_version": 1,
        "observed_epoch": observed_now,
        "overall_status": "attention" if attention else "healthy",
        "computer": {"connected": True},
        "services": services,
        "home_assistant": home_assistant,
        "model": model,
        "device_monitor": devices,
        "heartbeat": heartbeat_state,
        "daily_report": daily,
    }


def write_status(document: dict[str, Any], path: Path = OPERATIONS_STATE) -> None:
    heartbeat._validate_state_dir(path.parent)
    heartbeat._atomic_write(
        path,
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("ascii")
        + b"\n",
    )


def _transition_view(document: dict[str, Any]) -> dict[str, Any]:
    """Keep only stable, sanitized operational facts for change detection."""
    devices = document.get("device_monitor", {})
    heartbeat_state = document.get("heartbeat", {})
    return {
        "overall_status": document.get("overall_status"),
        "services": document.get("services", {}),
        "home_assistant": document.get("home_assistant", {}),
        "model": document.get("model", {}),
        "device_monitor": {
            key: devices.get(key)
            for key in (
                "fresh", "device_count", "healthy", "partial", "degraded",
                "offline", "unknown", "integration_degraded",
            )
        },
        "heartbeat": {
            "fresh": heartbeat_state.get("fresh"),
            "status": heartbeat_state.get("status"),
        },
        "daily_report": document.get("daily_report", {}),
    }


def _view_hash(view: dict[str, Any]) -> str:
    raw = json.dumps(view, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def append_transition(
    document: dict[str, Any],
    *,
    previous: dict[str, Any] | None,
    path: Path = OPERATIONS_JOURNAL,
) -> bool:
    current_view = _transition_view(document)
    previous_view = _transition_view(previous) if previous is not None else None
    journal_exists = path.exists() or path.is_symlink()
    if previous_view == current_view and journal_exists:
        return False
    heartbeat._validate_state_dir(path.parent)
    event = {
        "schema_version": 1,
        "observed_epoch": int(document["observed_epoch"]),
        "event": (
            "operational_baseline"
            if previous is None or not journal_exists
            else "operational_transition"
        ),
        "detected_by": "operations_supervisor",
        "action": "observe_only",
        "before_hash": None if previous_view is None else _view_hash(previous_view),
        "after_hash": _view_hash(current_view),
        "state": current_view,
    }
    raw = (
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode("ascii")
        + b"\n"
    )
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size + len(raw) > MAX_OPERATIONS_JOURNAL_BYTES
        ):
            raise SupervisorError("operations journal is unsafe")
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def check_status(
    path: Path = OPERATIONS_STATE,
    *,
    now: int | None = None,
) -> bool:
    observed_now = int(time.time()) if now is None else now
    try:
        document, _metadata = _load_private_json(path)
    except SupervisorError:
        return False
    observed = document.get("observed_epoch")
    return (
        document.get("schema_version") == 1
        and document.get("overall_status") in {"healthy", "attention"}
        and isinstance(observed, int)
        and not isinstance(observed, bool)
        and 0 <= observed <= observed_now + 5
        and observed_now - observed <= OPERATIONS_MAX_AGE_SECONDS
    )


def read_status(
    path: Path = OPERATIONS_STATE,
    *,
    now: int | None = None,
) -> dict[str, Any]:
    """Return only a current, schema-validated operational status document."""

    if not check_status(path, now=now):
        raise SupervisorError("operations status is stale")
    document, _metadata = _load_private_json(path)
    required = {
        "computer",
        "services",
        "home_assistant",
        "model",
        "device_monitor",
        "heartbeat",
        "daily_report",
    }
    if not required.issubset(document) or not all(
        isinstance(document[key], dict) for key in required
    ):
        raise SupervisorError("operations status is invalid")
    return document


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-status", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.check_status:
        result = {"schema_version": 1, "status": "current" if check_status() else "stale"}
        print(json.dumps(result, separators=(",", ":")))
        return 0 if result["status"] == "current" else 3
    document = build_status()
    try:
        try:
            previous, _metadata = _load_private_json(OPERATIONS_STATE)
        except SupervisorError:
            previous = None
        append_transition(document, previous=previous)
        write_status(document)
    except (heartbeat.HeartbeatError, SupervisorError, OSError):
        print('{"schema_version":1,"status":"not_written"}')
        return 3
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
