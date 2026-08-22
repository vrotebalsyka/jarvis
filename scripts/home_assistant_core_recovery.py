#!/usr/bin/env python3
"""Perform one bounded Home Assistant Core restart after a confirmed partial outage."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_read as ha_read  # noqa: E402
import incident_monitor  # noqa: E402


CHECK_PATH = "/api/services/homeassistant/check_config"
RESTART_PATH = "/api/services/homeassistant/restart"
MIN_CONFIRMED_SECONDS = 300
COOLDOWN_SECONDS = 6 * 3600
REQUEST_TIMEOUT_SECONDS = 25
VERIFY_TIMEOUT_SECONDS = 180
VERIFY_INTERVAL_SECONDS = 3


class CoreRecoveryError(RuntimeError):
    """A fixed, secret-free Core recovery failure."""


class CoreDeliveryUnknown(CoreRecoveryError):
    """A request was sent but no response was received."""


def _connection(config: ha_read.AdapterConfig) -> http.client.HTTPConnection:
    return http.client.HTTPConnection(config.host, config.port, timeout=REQUEST_TIMEOUT_SECONDS)


def _request_json(
    config: ha_read.AdapterConfig,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    connection_factory: Callable[[ha_read.AdapterConfig], http.client.HTTPConnection] = _connection,
) -> Any:
    request_sent = False
    connection: http.client.HTTPConnection | None = None
    try:
        connection = connection_factory(config)
        connection.request(
            method,
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
            raise CoreRecoveryError("Home Assistant rejected the bounded Core request")
        return ha_read.strict_json_loads(raw)
    except CoreRecoveryError:
        raise
    except (OSError, socket.timeout, TimeoutError, http.client.HTTPException) as error:
        if request_sent and method == "POST":
            raise CoreDeliveryUnknown("Core request delivery is unknown") from error
        raise CoreRecoveryError("Home Assistant Core is unreachable") from error
    finally:
        if connection is not None:
            try:
                connection.close()
            except (OSError, http.client.HTTPException):
                pass


def probe_core(config: ha_read.AdapterConfig) -> None:
    root = _request_json(config, "GET", "/api/")
    states = _request_json(config, "GET", "/api/states")
    if not isinstance(root, dict) or not isinstance(root.get("message"), str):
        raise CoreRecoveryError("Home Assistant root response is invalid")
    if not isinstance(states, list):
        raise CoreRecoveryError("Home Assistant states response is invalid")


def post_check_config(config: ha_read.AdapterConfig) -> None:
    document = _request_json(config, "POST", CHECK_PATH, body=b"{}")
    if not isinstance(document, (list, dict)):
        raise CoreRecoveryError("Home Assistant returned an invalid config-check response")


def post_restart(config: ha_read.AdapterConfig) -> None:
    document = _request_json(config, "POST", RESTART_PATH, body=b"{}")
    if not isinstance(document, (list, dict)):
        raise CoreRecoveryError("Home Assistant returned an invalid restart response")


def verify_restart(
    config: ha_read.AdapterConfig,
    *,
    probe: Callable[[ha_read.AdapterConfig], None] = probe_core,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    deadline = monotonic() + VERIFY_TIMEOUT_SECONDS
    observed_down = False
    while monotonic() < deadline:
        try:
            probe(config)
            if observed_down:
                return "verified"
        except CoreRecoveryError:
            observed_down = True
        sleeper(VERIFY_INTERVAL_SECONDS)
    try:
        probe(config)
        return "verified" if observed_down else "accepted"
    except CoreRecoveryError:
        return "delivery_unknown"


def run_once(
    store: incident_monitor.IncidentStore,
    *,
    now: int | None = None,
    live: bool,
    config_loader: Callable[[], ha_read.AdapterConfig] = ha_read.load_config,
    probe: Callable[[ha_read.AdapterConfig], None] = probe_core,
    check_caller: Callable[[ha_read.AdapterConfig], None] = post_check_config,
    restart_caller: Callable[[ha_read.AdapterConfig], None] = post_restart,
    verifier: Callable[[ha_read.AdapterConfig], str] = verify_restart,
) -> dict[str, object]:
    attempted_epoch = int(time.time()) if now is None else now
    candidate = store.core_recovery_candidate(
        attempted_epoch, min_confirmed_seconds=MIN_CONFIRMED_SECONDS
    )
    last_restart = store.last_core_restart_epoch()
    if (
        candidate is None
        or last_restart is not None
        and attempted_epoch - last_restart < COOLDOWN_SECONDS
    ):
        return {
            "schema_version": 1, "mode": "live" if live else "dry_run",
            "candidates": 0, "check_calls": 0, "restart_calls": 0,
            "verified": 0,
        }

    config = config_loader()
    try:
        probe(config)
    except CoreRecoveryError:
        return {
            "schema_version": 1, "mode": "live" if live else "dry_run",
            "candidates": 1, "check_calls": 0, "restart_calls": 0,
            "verified": 0, "outcome": "out_of_band_required",
        }
    if not live:
        return {
            "schema_version": 1, "mode": "dry_run", "candidates": 1,
            "check_calls": 0, "restart_calls": 0, "verified": 0,
        }

    incident_id = int(candidate["incident_id"])
    action_group_id = hashlib.sha256(
        f"core:{incident_id}:{attempted_epoch}".encode("ascii")
    ).hexdigest()[:32]
    check_calls = 1
    restart_calls = 0
    try:
        check_caller(config)
    except CoreDeliveryUnknown:
        status = "check_unknown"
    except CoreRecoveryError:
        status = "check_failed"
    else:
        restart_calls = 1
        try:
            restart_caller(config)
            status = verifier(config)
        except CoreDeliveryUnknown:
            status = verifier(config)
            if status == "accepted":
                status = "delivery_unknown"
        except CoreRecoveryError:
            status = "failed"

    after_state = "reachable" if status in {"accepted", "verified"} else "unknown"
    store.record_core_recovery(
        incident_id=incident_id,
        action_group_id=action_group_id,
        status=status,
        attempted_epoch=attempted_epoch,
        check_calls=check_calls,
        restart_calls=restart_calls,
        after_state=after_state,
    )
    return {
        "schema_version": 1, "mode": "live", "candidates": 1,
        "check_calls": check_calls, "restart_calls": restart_calls,
        "verified": int(status == "verified"), "outcome": status,
    }


def main() -> int:
    live = os.environ.get("HOME_BUTLER_CORE_RECOVERY_MODE", "dry-run") == "live"
    state_dir = incident_monitor._state_dir()
    try:
        incident_monitor._validate_directory(state_dir)
        store = incident_monitor.IncidentStore(state_dir / incident_monitor.DATABASE_NAME)
        try:
            result = run_once(store, live=live)
        finally:
            store.close()
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 0
    except (
        CoreRecoveryError,
        incident_monitor.MonitorError,
        ha_read.AdapterError,
        OSError,
        sqlite3.Error,
    ):
        print("HOME_ASSISTANT_CORE_RECOVERY_FAILED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
