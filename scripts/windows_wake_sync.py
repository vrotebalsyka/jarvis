#!/usr/bin/env python3
"""Synchronize one fixed Windows wake task from the safe scheduler export."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


sys.dont_write_bytecode = True

SCHEMA_VERSION = 1
WAKE_TASK_NAME = "Home Butler Scheduler Wake"
CMD_EXE = Path("/mnt/c/Windows/System32/cmd.exe")
DEFAULT_STATE_PATH = Path(
    os.environ.get(
        "HOME_BUTLER_WINDOWS_WAKE_STATE",
        "/home/homebutler/.local/state/home-butler/scheduler/windows-wake-sync.json",
    )
)
VERIFY_INTERVAL_SECONDS = 10 * 60
MINIMUM_LEAD_SECONDS = 30
MAXIMUM_LEAD_SECONDS = 366 * 24 * 60 * 60


class WakeSyncError(RuntimeError):
    """A bounded failure that never contains Windows output or private paths."""


def _safe_epoch(value: object, *, now: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WakeSyncError("wake epoch is invalid")
    if value <= now + MINIMUM_LEAD_SECONDS:
        raise WakeSyncError("wake epoch is not in the future")
    if value > now + MAXIMUM_LEAD_SECONDS:
        raise WakeSyncError("wake epoch is outside the bounded horizon")
    return value


def _read_state(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            return {}
        if path.stat().st_size > 4096:
            return {}
        document = json.loads(path.read_text(encoding="ascii"))
    except (OSError, ValueError, TypeError):
        return {}
    return document if isinstance(document, dict) else {}


def _atomic_write(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise WakeSyncError("wake state directory is unsafe")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(
        dict(document), sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode("ascii") + b"\n"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _invoke_helper(
    wake_epoch: int,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    helper_resolver: Callable[..., Path] | None = None,
) -> dict[str, Any]:
    if not CMD_EXE.is_file():
        raise WakeSyncError("Windows interop is unavailable")
    resolver = _resolve_helper if helper_resolver is None else helper_resolver
    helper = resolver(runner=runner)
    if not helper.is_file():
        raise WakeSyncError("Windows wake helper is unavailable")
    # The executable location is resolved from Windows' own LocalAppData and
    # the only variable argument is a validated integer epoch. No shell is
    # involved in the helper execution.
    try:
        completed = runner(
            [str(helper), str(wake_epoch)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise WakeSyncError("Windows wake helper failed") from error
    if completed.returncode != 0:
        raise WakeSyncError("Windows wake helper rejected the request")
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    try:
        document = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise WakeSyncError("Windows wake helper returned invalid evidence") from error
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("status") != "synced"
        or document.get("wake_epoch") != wake_epoch
        or document.get("task") != WAKE_TASK_NAME
    ):
        raise WakeSyncError("Windows wake helper verification failed")
    return document


def _resolve_helper(
    *, runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run
) -> Path:
    try:
        completed = runner(
            [str(CMD_EXE), "/u", "/d", "/c", "echo %LOCALAPPDATA%"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise WakeSyncError("Windows profile path is unavailable") from error
    if completed.returncode != 0:
        raise WakeSyncError("Windows profile path is unavailable")
    output = completed.stdout
    try:
        text = output.decode("utf-16-le") if isinstance(output, bytes) else str(output)
    except UnicodeError as error:
        raise WakeSyncError("Windows profile path is invalid") from error
    value = text.strip().splitlines()[-1] if text.strip() else ""
    if (
        len(value) > 512
        or not re.fullmatch(r"[A-Za-z]:\\[^\r\n\x00]+", value)
        or ".." in value.replace("\\", "/").split("/")
    ):
        raise WakeSyncError("Windows profile path is invalid")
    drive = value[0].lower()
    relative = value[3:].replace("\\", "/")
    return Path(f"/mnt/{drive}/{relative}/HomeButler/HomeButlerWakeSync.exe")


def sync_status(
    status: Mapping[str, Any],
    *,
    now: int | None = None,
    state_path: Path = DEFAULT_STATE_PATH,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    helper_resolver: Callable[..., Path] | None = None,
) -> dict[str, Any]:
    observed = int(time.time()) if now is None else int(now)
    try:
        desired = _safe_epoch(status.get("wake_epoch"), now=observed)
    except WakeSyncError:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "not_scheduled",
            "task": WAKE_TASK_NAME,
        }
    previous = _read_state(state_path)
    if (
        previous.get("wake_epoch") == desired
        and isinstance(previous.get("verified_epoch"), int)
        and observed - int(previous["verified_epoch"]) < VERIFY_INTERVAL_SECONDS
    ):
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "cached",
            "wake_epoch": desired,
            "task": WAKE_TASK_NAME,
        }
    try:
        _invoke_helper(desired, runner=runner, helper_resolver=helper_resolver)
    except WakeSyncError:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "unavailable",
            "wake_epoch": desired,
            "task": WAKE_TASK_NAME,
        }
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "wake_epoch": desired,
        "verified_epoch": observed,
        "task": WAKE_TASK_NAME,
    }
    try:
        _atomic_write(state_path, evidence)
    except (OSError, WakeSyncError):
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "unavailable",
            "wake_epoch": desired,
            "task": WAKE_TASK_NAME,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "synced",
        "wake_epoch": desired,
        "task": WAKE_TASK_NAME,
    }


def run(argv: Sequence[str] | None = None) -> int:
    del argv
    try:
        from persistent_scheduler import SchedulerStore, export_status

        status = export_status(SchedulerStore())
        result = sync_status(status)
    except (OSError, WakeSyncError):
        result = {"schema_version": SCHEMA_VERSION, "status": "unavailable"}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("status") in {"synced", "cached", "not_scheduled"} else 3


if __name__ == "__main__":
    raise SystemExit(run())
