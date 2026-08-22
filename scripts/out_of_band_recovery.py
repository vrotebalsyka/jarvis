#!/usr/bin/env python3
"""Recover a fully unreachable Home Assistant Container through a forced SSH command."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import incident_monitor  # noqa: E402


HA_HOST = "192.168.1.127"
HA_PORT = 8123
SSH_TARGET = "homebutler-recovery@192.168.1.127"
SSH_HOST_ALIAS = "homebutler-recovery-target"
SSH_BINARY = "/usr/bin/ssh"
KNOWN_HOSTS = Path("/opt/home-butler/config/ha-recovery-known_hosts")
KEY_NAME = "ha-recovery.key"
MIN_CONFIRMED_SECONDS = 300
RETRY_SECONDS = 300
SSH_TIMEOUT_SECONDS = 660
MAX_SSH_OUTPUT_BYTES = 128


class OutOfBandRecoveryError(RuntimeError):
    """Secret-free out-of-band recovery failure."""


class DeliveryUnknown(OutOfBandRecoveryError):
    """The forced command may have started before the SSH session timed out."""


def _validate_file(path: Path, *, expected_uid: int, private: bool) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise OutOfBandRecoveryError("recovery credential is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or metadata.st_mode & (0o077 if private else 0o022)
        or not 32 <= metadata.st_size <= 16_384
    ):
        raise OutOfBandRecoveryError("recovery credential is unsafe")


def load_key_path() -> Path:
    directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if not directory:
        raise OutOfBandRecoveryError("systemd recovery credential is unavailable")
    path = Path(directory) / KEY_NAME
    _validate_file(path, expected_uid=os.geteuid(), private=True)
    _validate_file(KNOWN_HOSTS, expected_uid=0, private=False)
    return path


def local_api_unreachable(
    *,
    attempts: int = 3,
    sleeper: Callable[[float], None] = time.sleep,
    connection_factory: Callable[[], http.client.HTTPConnection] = lambda: http.client.HTTPConnection(
        HA_HOST, HA_PORT, timeout=5
    ),
) -> bool:
    if attempts != 3:
        raise OutOfBandRecoveryError("invalid local probe policy")
    for attempt in range(attempts):
        connection = None
        try:
            connection = connection_factory()
            connection.request(
                "GET", "/", headers={"Connection": "close", "Accept": "text/html"}
            )
            response = connection.getresponse()
            response.read(1)
            if 100 <= response.status <= 599:
                return False
        except (OSError, socket.timeout, TimeoutError, http.client.HTTPException):
            pass
        finally:
            if connection is not None:
                try:
                    connection.close()
                except (OSError, http.client.HTTPException):
                    pass
        if attempt + 1 < attempts:
            sleeper(10)
    return True


def ssh_recover(
    key_path: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> str:
    command = [
        SSH_BINARY,
        "-F", "/dev/null",
        "-T",
        "-i", str(key_path),
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "IdentityAgent=none",
        "-o", "ClearAllForwardings=yes",
        "-o", "RequestTTY=no",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", f"HostKeyAlias={SSH_HOST_ALIAS}",
        "-o", "UpdateHostKeys=no",
        "-o", "VerifyHostKeyDNS=no",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "ConnectTimeout=10",
        "-o", "ConnectionAttempts=1",
        "-o", "LogLevel=ERROR",
        SSH_TARGET,
        "recover",
    ]
    try:
        completed = runner(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SSH_TIMEOUT_SECONDS,
            check=False,
            env={"HOME": "/home/homebutler", "PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired as error:
        raise DeliveryUnknown("SSH recovery delivery is unknown") from error
    except OSError as error:
        raise OutOfBandRecoveryError("SSH recovery could not start") from error
    if len(completed.stdout) > MAX_SSH_OUTPUT_BYTES:
        raise DeliveryUnknown("SSH recovery response is oversized")
    try:
        response = completed.stdout.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as error:
        raise DeliveryUnknown("SSH recovery response is invalid") from error
    allowed = {
        "status=healthy_no_action",
        "status=restarted_verified",
        "status=cooldown",
        "status=maintenance",
        "status=restart_failed",
        "status=identity_invalid",
        "status=docker_unavailable",
    }
    if response not in allowed:
        if completed.returncode == 255:
            raise OutOfBandRecoveryError("SSH recovery authentication failed")
        raise DeliveryUnknown("SSH recovery response is unknown")
    return response.removeprefix("status=")


def run_once(
    store: incident_monitor.IncidentStore,
    *,
    now: int | None = None,
    live: bool,
    api_unreachable: Callable[[], bool] = local_api_unreachable,
    key_loader: Callable[[], Path] = load_key_path,
    remote_recover: Callable[[Path], str] = ssh_recover,
) -> dict[str, object]:
    current = int(time.time()) if now is None else now
    candidate = store.out_of_band_recovery_candidate(
        current,
        min_confirmed_seconds=MIN_CONFIRMED_SECONDS,
        retry_seconds=RETRY_SECONDS,
    )
    if candidate is None:
        return {"schema_version": 1, "mode": "live" if live else "dry_run", "candidates": 0}
    if not live:
        return {
            "schema_version": 1,
            "mode": "dry_run",
            "candidates": 1,
            "outcome": "planned",
            "ssh_calls": 0,
            "restart_calls": 0,
        }
    incident_id = int(candidate["incident_id"])
    action_group_id = uuid.uuid4().hex
    if not api_unreachable():
        store.record_out_of_band_recovery(
            incident_id=incident_id,
            action_group_id=action_group_id,
            status="healthy",
            attempted_epoch=current,
            ssh_calls=0,
            restart_calls=0,
            after_state="reachable",
        )
        return {
            "schema_version": 1,
            "mode": "live",
            "candidates": 1,
            "outcome": "local_api_recovered",
            "ssh_calls": 0,
            "restart_calls": 0,
        }

    ssh_calls = 0
    restart_calls = 0
    try:
        key_path = key_loader()
        ssh_calls = 1
        outcome = remote_recover(key_path)
        if outcome == "healthy_no_action":
            status_value, after_state = "healthy", "reachable"
        elif outcome == "restarted_verified":
            status_value, after_state, restart_calls = "verified", "reachable", 1
        elif outcome in {"cooldown", "maintenance"}:
            status_value, after_state = "cooldown", "unknown"
        elif outcome == "restart_failed":
            status_value, after_state, restart_calls = "failed", "unknown", 1
        else:
            status_value, after_state = "failed", "unknown"
    except DeliveryUnknown:
        ssh_calls, restart_calls = 1, 1
        outcome, status_value, after_state = "delivery_unknown", "failed", "unknown"
    except OutOfBandRecoveryError:
        outcome, status_value, after_state = "channel_failed", "failed", "unknown"

    store.record_out_of_band_recovery(
        incident_id=incident_id,
        action_group_id=action_group_id,
        status=status_value,
        attempted_epoch=current,
        ssh_calls=ssh_calls,
        restart_calls=restart_calls,
        after_state=after_state,
    )
    return {
        "schema_version": 1,
        "mode": "live",
        "candidates": 1,
        "outcome": outcome,
        "ssh_calls": ssh_calls,
        "restart_calls": restart_calls,
        "verified": int(status_value == "verified"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    mode = os.environ.get("HOME_BUTLER_OUT_OF_BAND_MODE", "dry_run")
    if mode not in {"dry_run", "live"}:
        print("OUT_OF_BAND_RECOVERY_FAILED", file=sys.stderr)
        return 2
    live = mode == "live"
    state_dir = incident_monitor._state_dir()
    try:
        incident_monitor._validate_directory(state_dir)
        store = incident_monitor.IncidentStore(state_dir / incident_monitor.DATABASE_NAME)
        try:
            result = run_once(store, live=live)
        finally:
            store.close()
    except (OutOfBandRecoveryError, incident_monitor.MonitorError, OSError):
        print("OUT_OF_BAND_RECOVERY_FAILED", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    raise SystemExit(main())
