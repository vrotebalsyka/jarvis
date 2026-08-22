#!/usr/bin/env python3
"""Check and narrowly recover the private Alice webhook end to end."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import alice_skill_gateway  # noqa: E402
import alice_tailscale_funnel  # noqa: E402


SCHEMA_VERSION = 2
STATE_PATH = Path("/run/home-butler-alice-health/status.json")
MAX_STATE_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 8 * 1024
PROBE_TIMEOUT_SECONDS = 3.2
STATUS_MAX_AGE_SECONDS = 90
RECOVERY_THRESHOLD = 3
RECOVERY_COOLDOWN_SECONDS = 300
RESTART_WAIT_SECONDS = 90
SKILL_UNIT = "home-butler-alice-skill.service"
TUNNEL_UNIT = "home-butler-alice-tunnel.service"
TAILSCALE_UNIT = "tailscaled.service"
ALLOWED_UNITS = frozenset((SKILL_UNIT, TUNNEL_UNIT, TAILSCALE_UNIT))
SYSTEMCTL = "/usr/bin/systemctl"
PING_TEXT = "Дворецкий на связи."
VALID_ACTIONS = frozenset(
    (
        "none",
        "restart_skill",
        "restart_tunnel",
        "restart_skill_and_tunnel",
        "restart_tailscale_and_tunnel",
    )
)
VALID_ERROR_CODES = frozenset(
    (
        "none",
        "configuration",
        "local_probe",
        "public_probe",
        "tailscale_policy",
        "restart_failed",
        "state_directory",
        "state_file",
        "stale_status",
    )
)


class HealthError(RuntimeError):
    """A fixed, secret-free health failure."""

    def __init__(self, code: str) -> None:
        if code not in VALID_ERROR_CODES:
            code = "configuration"
        super().__init__(code)
        self.code = code


def ping_request(
    config: alice_skill_gateway.GatewayConfig,
    *,
    session_id: str,
) -> bytes:
    if config.pending or not config.owner_ids:
        raise HealthError("configuration")
    document = {
        "version": "1.0",
        "request": {
            "type": "SimpleUtterance",
            "original_utterance": "ping",
            "command": "ping",
        },
        "session": {
            "session_id": session_id,
            "message_id": 0,
            "new": True,
            "skill_id": config.skill_id,
            "user": {"user_id": config.owner_ids[0]},
        },
    }
    return json.dumps(
        document, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def validate_ping_response(status: int, raw: bytes, error_code: str) -> None:
    if status != 200 or not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise HealthError(error_code)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise HealthError(error_code) from error
    response = document.get("response") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("version") != "1.0"
        or not isinstance(response, dict)
        or response.get("text") != PING_TEXT
        or response.get("tts") != PING_TEXT
        or response.get("end_session") is not False
    ):
        raise HealthError(error_code)


def _probe(
    config: alice_skill_gateway.GatewayConfig,
    *,
    host: str,
    public: bool,
) -> None:
    error_code = "public_probe" if public else "local_probe"
    session_id = f"alice-health-{'public' if public else 'local'}-{time.time_ns()}"
    if alice_skill_gateway.ID_RE.fullmatch(session_id) is None:
        raise HealthError("configuration")
    connection: http.client.HTTPConnection
    if public:
        connection = http.client.HTTPSConnection(
            host, 443, timeout=PROBE_TIMEOUT_SECONDS
        )
    else:
        connection = http.client.HTTPConnection(
            host, config.port, timeout=PROBE_TIMEOUT_SECONDS
        )
    try:
        connection.request(
            "POST",
            config.webhook_path,
            body=ping_request(config, session_id=session_id),
            headers={
                "Accept": "application/json",
                "Connection": "close",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, TimeoutError, http.client.HTTPException) as error:
        raise HealthError(error_code) from error
    finally:
        connection.close()
    validate_ping_response(response.status, raw, error_code)


def probe_local(config: alice_skill_gateway.GatewayConfig) -> None:
    _probe(config, host="127.0.0.1", public=False)


def probe_public(config: alice_skill_gateway.GatewayConfig) -> None:
    try:
        origin = alice_tailscale_funnel.current_origin()
    except alice_tailscale_funnel.FunnelError as error:
        code = (
            "tailscale_policy"
            if error.code == "invalid_packet_filter"
            else "public_probe"
        )
        raise HealthError(code) from error
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or parsed.port not in {None, 443}
        or not isinstance(parsed.hostname, str)
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise HealthError("public_probe")
    _probe(config, host=parsed.hostname, public=True)


def _require_private_directory(path: Path) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise HealthError("state_directory") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise HealthError("state_directory")


def _default_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_epoch": 0,
        "healthy": False,
        "consecutive_failures": 0,
        "local_ready": False,
        "public_ready": False,
        "last_action": "none",
        "last_error_code": "none",
        "last_recovery_epoch": 0,
    }


def load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    _require_private_directory(path.parent)
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return _default_state()
    except OSError as error:
        raise HealthError("state_file") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= MAX_STATE_BYTES
    ):
        raise HealthError("state_file")
    try:
        raw = path.read_bytes()
        document = json.loads(raw.decode("ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HealthError("state_file") from error
    if (
        not isinstance(document, dict)
        or document.get("schema_version") not in {1, SCHEMA_VERSION}
        or not isinstance(document.get("observed_epoch"), int)
        or isinstance(document.get("observed_epoch"), bool)
        or not isinstance(document.get("healthy"), bool)
        or not isinstance(document.get("local_ready"), bool)
        or not isinstance(document.get("public_ready"), bool)
        or not isinstance(document.get("consecutive_failures"), int)
        or isinstance(document.get("consecutive_failures"), bool)
        or not 0 <= document["consecutive_failures"] <= 1000
        or document.get("last_action") not in VALID_ACTIONS
        or document.get("last_error_code") not in VALID_ERROR_CODES
    ):
        raise HealthError("state_file")
    migrated = {**_default_state(), **document, "schema_version": SCHEMA_VERSION}
    last_recovery_epoch = migrated.get("last_recovery_epoch")
    if (
        not isinstance(last_recovery_epoch, int)
        or isinstance(last_recovery_epoch, bool)
        or last_recovery_epoch < 0
    ):
        raise HealthError("state_file")
    return migrated


def write_state(path: Path, document: dict[str, Any]) -> None:
    _require_private_directory(path.parent)
    if path.exists() or path.is_symlink():
        metadata = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise HealthError("state_file")
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("ascii")
    descriptor, name = tempfile.mkstemp(prefix=".alice-health.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, os.geteuid(), os.getegid())
        os.write(descriptor, raw)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def restart_unit(unit: str) -> None:
    if unit not in ALLOWED_UNITS:
        raise HealthError("configuration")
    try:
        for command in (
            [SYSTEMCTL, "reset-failed", "--", unit],
            [SYSTEMCTL, "restart", "--", unit],
        ):
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=180,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
            if completed.returncode != 0:
                raise HealthError("restart_failed")
    except (OSError, subprocess.SubprocessError) as error:
        raise HealthError("restart_failed") from error


def _attempt(probe: Callable[[], None]) -> tuple[bool, str]:
    try:
        probe()
        return True, "none"
    except HealthError as error:
        return False, error.code


def _wait_for(
    probe: Callable[[], None],
    *,
    sleeper: Callable[[float], None],
    clock: Callable[[], float],
    wait_seconds: float = RESTART_WAIT_SECONDS,
) -> tuple[bool, str]:
    deadline = clock() + wait_seconds
    while True:
        ready, error_code = _attempt(probe)
        if ready or clock() >= deadline:
            return ready, error_code
        sleeper(2.0)


def run_once(
    config: alice_skill_gateway.GatewayConfig,
    *,
    state_path: Path = STATE_PATH,
    local_probe: Callable[[], None] | None = None,
    public_probe_runner: Callable[[], None] | None = None,
    restarter: Callable[[str], None] = restart_unit,
    wall_clock: Callable[[], float] = time.time,
    monotonic_clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    recovery_threshold: int = RECOVERY_THRESHOLD,
    recovery_cooldown_seconds: int = RECOVERY_COOLDOWN_SECONDS,
) -> bool:
    previous = load_state(state_path)
    local_check = local_probe or (lambda: probe_local(config))
    public_check = public_probe_runner or (lambda: probe_public(config))
    local_ready, local_error = _attempt(local_check)
    public_ready, public_error = _attempt(public_check)
    observed_epoch = int(wall_clock())
    if local_ready and public_ready:
        write_state(
            state_path,
            {
                **_default_state(),
                "observed_epoch": observed_epoch,
                "healthy": True,
                "local_ready": True,
                "public_ready": True,
                "last_recovery_epoch": previous["last_recovery_epoch"],
            },
        )
        return True

    failures = min(1000, previous["consecutive_failures"] + 1)
    error_code = local_error if not local_ready else public_error
    action = "none"
    last_recovery_epoch = previous["last_recovery_epoch"]
    recovery_due = (
        last_recovery_epoch == 0
        or observed_epoch - last_recovery_epoch >= recovery_cooldown_seconds
    )
    if failures >= recovery_threshold and recovery_due:
        try:
            if not local_ready:
                action = "restart_skill"
                restarter(SKILL_UNIT)
                local_ready, local_error = _wait_for(
                    local_check,
                    sleeper=sleeper,
                    clock=monotonic_clock,
                )
                if local_ready:
                    public_ready, public_error = _attempt(public_check)
            if local_ready and not public_ready:
                if public_error == "tailscale_policy":
                    action = "restart_tailscale_and_tunnel"
                    restarter(TAILSCALE_UNIT)
                else:
                    action = (
                        "restart_skill_and_tunnel"
                        if action == "restart_skill"
                        else "restart_tunnel"
                    )
                restarter(TUNNEL_UNIT)
                public_ready, public_error = _wait_for(
                    public_check,
                    sleeper=sleeper,
                    clock=monotonic_clock,
                )
        except HealthError as error:
            error_code = error.code
        else:
            error_code = local_error if not local_ready else public_error
        last_recovery_epoch = observed_epoch

    healthy = local_ready and public_ready
    write_state(
        state_path,
        {
            **_default_state(),
            "observed_epoch": observed_epoch,
            "healthy": healthy,
            "consecutive_failures": 0 if healthy else failures,
            "local_ready": local_ready,
            "public_ready": public_ready,
            "last_action": action,
            "last_error_code": "none" if healthy else error_code,
            "last_recovery_epoch": last_recovery_epoch,
        },
    )
    return healthy


def check_status(
    path: Path = STATE_PATH,
    *,
    clock: Callable[[], float] = time.time,
    max_age_seconds: int = STATUS_MAX_AGE_SECONDS,
) -> None:
    document = load_state(path)
    age = clock() - document["observed_epoch"]
    if document["healthy"] is not True or age < 0 or age > max_age_seconds:
        raise HealthError("stale_status")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--recover", action="store_true")
    action.add_argument("--check-status", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        if arguments.check_status:
            check_status()
            print("alice_skill_health=ready")
            return 0
        if os.geteuid() != 0:
            raise HealthError("configuration")
        config = alice_skill_gateway.GatewayConfig.load()
        if config.pending:
            print("alice_skill_health=pending_provisioning")
            return 0
        if not run_once(config):
            raise HealthError("public_probe")
        print("alice_skill_health=ready")
        return 0
    except (
        HealthError,
        alice_skill_gateway.GatewayError,
        alice_tailscale_funnel.FunnelError,
        OSError,
        subprocess.SubprocessError,
    ):
        print("Alice skill health check failed safely.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
