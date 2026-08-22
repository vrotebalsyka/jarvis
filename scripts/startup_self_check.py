#!/usr/bin/env python3
"""Persist a secret-free proof that Home Butler really started and can work."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import model_ha_proof  # noqa: E402
from ollama_endpoint import EndpointConfigError  # noqa: E402


SCHEMA_VERSION = 1
STATE_DIR = Path("/home/homebutler/.local/state/home-butler")
STATUS_NAME = "startup-self-check.json"
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
MAX_STATUS_BYTES = 64 * 1024
BOOT_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
REQUIRED_UNITS = (
    "home-butler.service",
    "home-butler-incident-monitor.service",
    "home-butler-incident-notifier.timer",
    "home-butler-inventory.timer",
    "home-butler-dialogue-qualification.timer",
    "home-butler-local-chat.service",
    "home-butler-alice-skill.service",
    "home-butler-alice-tunnel.service",
)


class SelfCheckError(RuntimeError):
    """A fixed, secret-free startup self-check failure."""


def read_boot_id(path: Path = BOOT_ID_PATH) -> str:
    try:
        value = path.read_text(encoding="ascii").strip().lower()
    except (OSError, UnicodeError) as error:
        raise SelfCheckError("boot identity is unavailable") from error
    if BOOT_ID_RE.fullmatch(value) is None:
        raise SelfCheckError("boot identity is invalid")
    return value


def unit_is_active(unit: str) -> bool:
    if unit not in REQUIRED_UNITS:
        raise SelfCheckError("unit is outside the self-check allowlist")
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", "--quiet", "--", unit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SelfCheckError("unit state is unavailable") from error
    return result.returncode == 0


def evaluate(
    proof: dict[str, Any] | None,
    unit_states: dict[str, bool],
    *,
    boot_id: str,
    observed_epoch: int,
) -> dict[str, Any]:
    if BOOT_ID_RE.fullmatch(boot_id) is None or observed_epoch < 0:
        raise SelfCheckError("self-check input is invalid")
    if set(unit_states) != set(REQUIRED_UNITS) or not all(
        isinstance(value, bool) for value in unit_states.values()
    ):
        raise SelfCheckError("unit evidence is incomplete")

    home_assistant = proof.get("home_assistant") if isinstance(proof, dict) else None
    accelerator = proof.get("accelerator") if isinstance(proof, dict) else None
    verified = isinstance(proof, dict) and proof.get("verified") is True
    tool_call = proof.get("tool_call") if isinstance(proof, dict) else None
    tool_verified = (
        verified
        and isinstance(tool_call, dict)
        and tool_call == {"name": model_ha_proof.TOOL_NAME, "arguments": {}}
    )
    ha_ready = (
        tool_verified
        and isinstance(home_assistant, dict)
        and home_assistant.get("status") in {"healthy", "stale_data"}
        and home_assistant.get("http_method") == "GET"
        and home_assistant.get("service_calls") == 0
    )
    endpoint = proof.get("ollama_endpoint") if isinstance(proof, dict) else None
    fully_on_gpu = (
        accelerator.get("fully_on_gpu")
        if isinstance(accelerator, dict)
        else None
    )
    if endpoint == "http://127.0.0.1:11434" and fully_on_gpu is False:
        accelerator_mode = "cpu_fallback"
    elif (
        isinstance(endpoint, str)
        and endpoint != "http://127.0.0.1:11434"
        and fully_on_gpu is True
    ):
        accelerator_mode = "gpu"
    else:
        accelerator_mode = "unverified"
    model_ready = tool_verified and accelerator_mode in {"gpu", "cpu_fallback"}
    observer_ready = unit_states["home-butler-incident-monitor.service"]
    notification_ready = unit_states["home-butler-incident-notifier.timer"]
    alice_ready = (
        unit_states["home-butler-alice-skill.service"]
        and unit_states["home-butler-alice-tunnel.service"]
    )
    local_chat_ready = unit_states["home-butler-local-chat.service"]
    units_ready = all(unit_states.values())
    ready = all((ha_ready, model_ready, observer_ready, notification_ready,
                 alice_ready, local_chat_ready, units_ready))

    return {
        "schema_version": SCHEMA_VERSION,
        "observed_epoch": observed_epoch,
        "boot_id": boot_id,
        "ready": ready,
        "home_assistant_ready": ha_ready,
        "observer_ready": observer_ready,
        "model_ready": model_ready,
        "accelerator": accelerator_mode,
        "notifications_ready": notification_ready,
        "local_chat_ready": local_chat_ready,
        "alice_local_ready": alice_ready,
        "tool_call_verified": tool_verified,
        "entity_count": (
            int(home_assistant["entity_count"])
            if ha_ready
            and isinstance(home_assistant.get("entity_count"), int)
            and 0 <= int(home_assistant["entity_count"]) <= 100_000
            else None
        ),
        "inactive_units": sorted(
            unit for unit, active in unit_states.items() if not active
        ),
    }


def _status_path(state_dir: Path | None = None) -> Path:
    return (STATE_DIR if state_dir is None else state_dir) / STATUS_NAME


def write_status(document: dict[str, Any], state_dir: Path | None = None) -> Path:
    directory = STATE_DIR if state_dir is None else state_dir
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = directory.lstat()
    except OSError as error:
        raise SelfCheckError("self-check directory is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise SelfCheckError("self-check directory is unsafe")
    raw = (json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    if len(raw) > MAX_STATUS_BYTES:
        raise SelfCheckError("self-check result is too large")
    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{STATUS_NAME}.", dir=directory
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, _status_path(directory))
        temporary_path = None
    except OSError as error:
        raise SelfCheckError("self-check result cannot be stored") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
    return _status_path(directory)


def read_status(
    state_dir: Path | None = None,
    *,
    current_boot_id: str | None = None,
) -> dict[str, Any]:
    path = _status_path(state_dir)
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= MAX_STATUS_BYTES
        ):
            raise SelfCheckError("self-check result is unsafe")
        document = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SelfCheckError("self-check result is unavailable") from error
    expected_keys = {
        "schema_version", "observed_epoch", "boot_id", "ready",
        "home_assistant_ready", "observer_ready", "model_ready", "accelerator",
        "notifications_ready", "local_chat_ready", "alice_local_ready",
        "tool_call_verified", "entity_count", "inactive_units",
    }
    if not isinstance(document, dict) or set(document) != expected_keys:
        raise SelfCheckError("self-check result is invalid")
    boot_id = document.get("boot_id")
    if (
        document.get("schema_version") != SCHEMA_VERSION
        or document.get("ready") is not True
        or not isinstance(boot_id, str)
        or BOOT_ID_RE.fullmatch(boot_id) is None
        or (current_boot_id is not None and boot_id != current_boot_id)
        or document.get("accelerator") not in {"gpu", "cpu_fallback"}
        or not all(
            document.get(key) is True
            for key in (
                "home_assistant_ready", "observer_ready", "model_ready",
                "notifications_ready", "local_chat_ready", "alice_local_ready",
                "tool_call_verified",
            )
        )
        or document.get("inactive_units") != []
    ):
        raise SelfCheckError("self-check is not ready for this boot")
    return document


def run_once(
    *,
    proof_runner: Callable[[], dict[str, Any]] = model_ha_proof.run_proof,
    unit_checker: Callable[[str], bool] = unit_is_active,
    boot_id_reader: Callable[[], str] = read_boot_id,
    clock: Callable[[], float] = time.time,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    boot_id = boot_id_reader()
    unit_states = {unit: bool(unit_checker(unit)) for unit in REQUIRED_UNITS}
    try:
        proof = proof_runner()
    except (model_ha_proof.ProofError, EndpointConfigError, OSError):
        proof = None
    document = evaluate(
        proof, unit_states, boot_id=boot_id, observed_epoch=int(clock())
    )
    write_status(document, state_dir)
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-status", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        if arguments.check_status:
            document = read_status(current_boot_id=read_boot_id())
        else:
            document = run_once()
    except SelfCheckError:
        print("startup_self_check=unavailable", file=sys.stderr)
        return 2
    if arguments.check_status:
        print(f"startup_self_check=ready accelerator={document['accelerator']}")
        return 0
    print(json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0 if document["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
