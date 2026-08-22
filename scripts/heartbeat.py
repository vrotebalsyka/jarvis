#!/usr/bin/env python3
"""Run the read-only health pipeline with persistent duplicate suppression."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

sys.dont_write_bytecode = True

from health_report_core import (  # noqa: E402
    HealthReportError,
    analyze_snapshot,
    ensure_snapshot_fresh,
    parse_snapshot_bytes,
    strict_json_loads,
)


PROJECT_DIR = Path(__file__).resolve().parents[1]
COLLECTOR = PROJECT_DIR / "scripts" / "local-health-check.sh"
REPORTER = PROJECT_DIR / "scripts" / "health_report.py"
DEFAULT_STATE_DIR = Path("/home/homebutler/.local/state/home-butler")
MAX_COMMAND_OUTPUT_BYTES = 1_048_576
COLLECT_TIMEOUT_SECONDS = 30
REPORT_TIMEOUT_SECONDS = 240
DUPLICATE_COOLDOWN_SECONDS = 3600


class HeartbeatError(RuntimeError):
    """A safe heartbeat failure that must not expose command output."""


def _state_dir_from_environment() -> Path:
    raw = os.environ.get("HOME_BUTLER_HEARTBEAT_STATE_DIR", "")
    return Path(raw) if raw else DEFAULT_STATE_DIR


def _validate_state_dir(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise HeartbeatError("heartbeat state directory is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise HeartbeatError("heartbeat state directory is unsafe")


def _open_lock(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise HeartbeatError("heartbeat lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except (OSError, BlockingIOError) as error:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise HeartbeatError("heartbeat is already running") from error


def _run_fixed(command: list[str], *, input_bytes: bytes | None, timeout: int) -> bytes:
    try:
        completed = subprocess.run(
            command,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HeartbeatError("heartbeat command failed") from error
    if completed.returncode != 0 or not completed.stdout:
        raise HeartbeatError("heartbeat command failed")
    if len(completed.stdout) > MAX_COMMAND_OUTPUT_BYTES:
        raise HeartbeatError("heartbeat command output is too large")
    return completed.stdout


def collect_snapshot() -> bytes:
    return _run_fixed([str(COLLECTOR)], input_bytes=None, timeout=COLLECT_TIMEOUT_SECONDS)


def render_report(snapshot: bytes) -> bytes:
    return _run_fixed(
        [sys.executable, str(REPORTER)],
        input_bytes=snapshot,
        timeout=REPORT_TIMEOUT_SECONDS,
    )


def _fingerprint(snapshot: dict[str, object]) -> tuple[str, str]:
    analysis = analyze_snapshot(snapshot)
    problems = [
        {
            "code": item.code,
            "value": item.model_value,
            "details": dict(item.details),
        }
        for item in analysis.problems
    ]
    missing = [
        {
            "code": item.code,
            "value": item.model_value,
            "details": dict(item.details),
        }
        for item in analysis.missing
    ]
    disks = snapshot["disks"]  # type: ignore[assignment]
    temperatures = snapshot["temperatures"]  # type: ignore[assignment]
    ollama = snapshot["ollama"]  # type: ignore[assignment]
    stable = {
        "status": analysis.status,
        "problems": problems,
        "missing": missing,
        "failed_systemd_units": sorted(snapshot["failed_systemd_units"]),  # type: ignore[arg-type]
        "problem_disks": [
            {
                "filesystem": disk["filesystem"],
                "used_percent": disk["used_percent"],
                "available_bytes": disk["available_bytes"],
            }
            for disk in disks  # type: ignore[union-attr]
            if disk["used_percent"] >= 90
        ],
        "problem_temperatures": [
            {
                "chip": item["chip"],
                "sensor": item["sensor"],
                "celsius": item["celsius"],
            }
            for item in temperatures  # type: ignore[union-attr]
            if item["celsius"] >= 90 or item["celsius"] <= -100
        ],
        "probes": snapshot["probes"],
        "ollama": {
            "reachable": ollama["reachable"],  # type: ignore[index]
            "version": ollama["version"],  # type: ignore[index]
            "models": [
                {
                    "name": model["name"],
                    "size_vram_bytes": model["size_vram_bytes"],
                    "context_length": model["context_length"],
                }
                for model in ollama["loaded_models"]  # type: ignore[index]
            ],
        },
        "hermes": snapshot["hermes"],
        "home_assistant": snapshot["home_assistant"],
    }
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest(), analysis.status


def _load_state(path: Path) -> dict[str, object] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise HeartbeatError("heartbeat state is invalid") from error
    try:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_size > 65_536
        ):
            raise HeartbeatError("heartbeat state is unsafe")
        document = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, HealthReportError) as error:
        raise HeartbeatError("heartbeat state is invalid") from error
    if not isinstance(document, dict) or set(document) != {
        "schema_version", "fingerprint", "last_emitted_at", "last_observed_at",
        "status", "snapshot_sha256", "report_sha256"
    }:
        raise HeartbeatError("heartbeat state is invalid")
    if (
        document["schema_version"] != 2
        or not isinstance(document["fingerprint"], str)
        or len(document["fingerprint"]) != 64
        or not isinstance(document["last_emitted_at"], int)
        or document["last_emitted_at"] < 0
        or not isinstance(document["last_observed_at"], str)
        or not isinstance(document["snapshot_sha256"], str)
        or len(document["snapshot_sha256"]) != 64
        or not isinstance(document["report_sha256"], str)
        or len(document["report_sha256"]) != 64
        or document["status"] not in {"ok", "attention"}
    ):
        raise HeartbeatError("heartbeat state is invalid")
    return document


def _atomic_write(path: Path, payload: bytes) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        metadata = None
    except OSError as error:
        raise HeartbeatError("heartbeat output target is unsafe") from error
    if metadata is not None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise HeartbeatError("heartbeat output target is unsafe")
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    except OSError as error:
        raise HeartbeatError("heartbeat output write failed") from error
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        directory_descriptor = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as error:
        raise HeartbeatError("heartbeat output write failed") from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def run_once(
    state_dir: Path,
    *,
    now: Callable[[], float] = time.time,
    collector: Callable[[], bytes] | None = None,
    reporter: Callable[[bytes], bytes] | None = None,
) -> bool:
    collector = collector or collect_snapshot
    reporter = reporter or render_report
    _validate_state_dir(state_dir)
    lock_descriptor = _open_lock(state_dir / "heartbeat.lock")
    try:
        snapshot_raw = collector()
        snapshot = parse_snapshot_bytes(snapshot_raw)
        ensure_snapshot_fresh(snapshot)
        fingerprint, status = _fingerprint(snapshot)
        report = reporter(snapshot_raw)
        try:
            report_text = report.decode("utf-8")
        except UnicodeDecodeError as error:
            raise HeartbeatError("heartbeat report is not UTF-8") from error

        current = int(now())
        if current < 0:
            raise HeartbeatError("heartbeat clock is invalid")
        previous = _load_state(state_dir / "heartbeat-state.json")
        same = previous is not None and previous["fingerprint"] == fingerprint
        elapsed = current - int(previous["last_emitted_at"]) if same else 0
        within_cooldown = (
            same
            and 0 <= elapsed < DUPLICATE_COOLDOWN_SECONDS
        )
        emit = not within_cooldown
        last_emitted_at = current if emit else int(previous["last_emitted_at"])
        state = {
            "schema_version": 2,
            "fingerprint": fingerprint,
            "last_emitted_at": last_emitted_at,
            "last_observed_at": snapshot["observed_at"],
            "status": status,
            "snapshot_sha256": hashlib.sha256(snapshot_raw.rstrip() + b"\n").hexdigest(),
            "report_sha256": hashlib.sha256(report.rstrip() + b"\n").hexdigest(),
        }
        _atomic_write(state_dir / "latest-snapshot.json", snapshot_raw.rstrip() + b"\n")
        _atomic_write(state_dir / "latest-report.txt", report.rstrip() + b"\n")
        _atomic_write(
            state_dir / "heartbeat-state.json",
            json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n",
        )
        if emit:
            print(report_text.rstrip())
        else:
            remaining = DUPLICATE_COOLDOWN_SECONDS - (current - last_emitted_at)
            print(f"HEARTBEAT_SUPPRESSED status={status} cooldown_remaining_seconds={remaining}")
        return emit
    finally:
        os.close(lock_descriptor)


def main() -> int:
    try:
        run_once(_state_dir_from_environment())
    except (HeartbeatError, HealthReportError, OSError):
        print("HEARTBEAT_FAILED", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
