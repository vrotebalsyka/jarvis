#!/usr/bin/env python3
"""Prove and narrowly recover each component of the private Alice path."""

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


SCHEMA_VERSION = 3
STATE_PATH = Path("/run/home-butler-alice-health/status.json")
MAX_STATE_BYTES = 32 * 1024
MAX_RESPONSE_BYTES = 8 * 1024
PROBE_TIMEOUT_SECONDS = 3.2
STATUS_MAX_AGE_SECONDS = 35
RECOVERY_THRESHOLD = 2
RECOVERY_WAIT_SECONDS = 20
RECOVERY_BASE_BACKOFF_SECONDS = 30
RECOVERY_MAX_BACKOFF_SECONDS = 15 * 60
RECOVERY_CIRCUIT_THRESHOLD = 5
RECOVERY_CIRCUIT_SECONDS = 15 * 60
MODEL_TURN_PROBE_INTERVAL_SECONDS = 60
HA_READ_PROBE_INTERVAL_SECONDS = 30
# The slowest public-route ladder after confirmation is: one bounded public
# probe, a 10 s Tailscale wait, an 8 s exact Funnel reassert and a 20 s public
# readback.  systemd restarts are submitted non-blocking, so they do not expand
# this deadline.
PUBLIC_RECOVERY_BUDGET_SECONDS = (
    PROBE_TIMEOUT_SECONDS + 10 + 4 + 4 + RECOVERY_WAIT_SECONDS
)
SKILL_UNIT = "home-butler-alice-skill.service"
TUNNEL_UNIT = "home-butler-alice-tunnel.service"
TAILSCALE_UNIT = "tailscaled.service"
ALLOWED_UNITS = frozenset((SKILL_UNIT, TUNNEL_UNIT, TAILSCALE_UNIT))
SYSTEMCTL = "/usr/bin/systemctl"
PING_TEXT = "Дворецкий на связи."
COMPONENT_FIELDS = (
    "gateway_ready",
    "public_route_ready",
    "tailscale_ready",
    "model_endpoint_ready",
    "model_loaded",
    "model_turn_ready",
    "ha_read_ready",
)
RECOVERY_DOMAINS = ("gateway", "public_route", "model")
VALID_ACTIONS = frozenset(
    (
        "none",
        "restart_skill",
        "reassert_funnel",
        "reassert_funnel_restart_tunnel",
        "restart_tailscale_reassert_funnel",
        "restart_tailscale_reassert_funnel_restart_tunnel",
        "warm_model",
        "await_model_supervisor",
        # Accepted only to migrate schema 1/2 state safely.
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
        "tailscale",
        "tailscale_policy",
        "funnel_config",
        "model_endpoint",
        "model_loaded",
        "model_turn",
        "ha_read",
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


def health_request(
    config: alice_skill_gateway.GatewayConfig,
    *,
    session_id: str,
    command: str,
) -> bytes:
    if config.pending or not config.owner_ids:
        raise HealthError("configuration")
    document = {
        "version": "1.0",
        "request": {
            "type": "SimpleUtterance",
            "original_utterance": command,
            "command": command,
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


def ping_request(
    config: alice_skill_gateway.GatewayConfig,
    *,
    session_id: str,
) -> bytes:
    """Compatibility wrapper for the transport-only owner probe."""

    return health_request(
        config,
        session_id=session_id,
        command="ping",
    )


def validate_probe_response(
    status: int,
    raw: bytes,
    error_code: str,
    expected_text: str,
) -> None:
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
        or response.get("text") != expected_text
        or response.get("tts") != expected_text
        or response.get("end_session") is not False
    ):
        raise HealthError(error_code)


def validate_ping_response(status: int, raw: bytes, error_code: str) -> None:
    validate_probe_response(status, raw, error_code, PING_TEXT)


def _probe(
    config: alice_skill_gateway.GatewayConfig,
    *,
    host: str,
    public: bool,
    command: str = "ping",
    expected_text: str = PING_TEXT,
    error_code: str | None = None,
) -> None:
    code = error_code or ("public_probe" if public else "local_probe")
    label = "public" if public else "local"
    session_id = f"alice-health-{label}-{time.time_ns()}"
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
            body=health_request(
                config,
                session_id=session_id,
                command=command,
            ),
            headers={
                "Accept": "application/json",
                "Connection": "close",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, TimeoutError, http.client.HTTPException) as error:
        raise HealthError(code) from error
    finally:
        connection.close()
    validate_probe_response(response.status, raw, code, expected_text)


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


def probe_tailscale() -> None:
    try:
        alice_tailscale_funnel.current_origin()
    except alice_tailscale_funnel.FunnelError as error:
        code = (
            "tailscale_policy"
            if error.code == "invalid_packet_filter"
            else "tailscale"
        )
        raise HealthError(code) from error


def probe_funnel_config() -> None:
    try:
        alice_tailscale_funnel.inspect_funnel()
    except alice_tailscale_funnel.FunnelError as error:
        code = (
            "tailscale_policy"
            if error.code == "invalid_packet_filter"
            else "funnel_config"
        )
        raise HealthError(code) from error


def reassert_funnel() -> None:
    try:
        alice_tailscale_funnel.ensure_funnel(
            wait_seconds=4,
            public_wait_seconds=4,
        )
    except alice_tailscale_funnel.FunnelError as error:
        code = (
            "tailscale_policy"
            if error.code == "invalid_packet_filter"
            else "funnel_config"
        )
        raise HealthError(code) from error


def probe_model_endpoint() -> None:
    try:
        endpoint = alice_skill_gateway.owner_chat.load_runtime_ollama_endpoint()
        document = alice_skill_gateway.model_ha_proof.get_ollama(
            endpoint, "/api/version"
        )
    except (
        alice_skill_gateway.EndpointConfigError,
        alice_skill_gateway.model_ha_proof.ProofError,
    ) as error:
        raise HealthError("model_endpoint") from error
    if (
        set(document) != {"version"}
        or not isinstance(document.get("version"), str)
        or not document["version"]
    ):
        raise HealthError("model_endpoint")


def probe_model_loaded() -> None:
    try:
        endpoint = alice_skill_gateway.owner_chat.load_runtime_ollama_endpoint()
        alice_skill_gateway.model_ha_proof.gpu_evidence(
            alice_skill_gateway.model_ha_proof.get_ollama(endpoint, "/api/ps"),
            expected_model=alice_skill_gateway.VOICE_POLICY.model,
        )
    except (
        alice_skill_gateway.EndpointConfigError,
        alice_skill_gateway.model_ha_proof.ProofError,
    ) as error:
        raise HealthError("model_loaded") from error


def probe_model_turn(config: alice_skill_gateway.GatewayConfig) -> None:
    _probe(
        config,
        host="127.0.0.1",
        public=False,
        command=alice_skill_gateway.HEALTH_MODEL_COMMAND,
        expected_text=alice_skill_gateway.HEALTH_MODEL_TEXT,
        error_code="model_turn",
    )


def probe_ha_read(config: alice_skill_gateway.GatewayConfig) -> None:
    _probe(
        config,
        host="127.0.0.1",
        public=False,
        command=alice_skill_gateway.HEALTH_HA_READ_COMMAND,
        expected_text=alice_skill_gateway.HEALTH_HA_READ_TEXT,
        error_code="ha_read",
    )


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


def _zero_map(keys: tuple[str, ...]) -> dict[str, int]:
    return {key: 0 for key in keys}


def _default_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_epoch": 0,
        "healthy": False,
        "overall_voice_ready": False,
        "owner_config_ready": False,
        **{field: False for field in COMPONENT_FIELDS},
        "consecutive_failures": 0,
        "failure_streaks": _zero_map(COMPONENT_FIELDS),
        "last_action": "none",
        "last_error_code": "none",
        "last_recovery_epoch": 0,
        "recovery_failures": _zero_map(RECOVERY_DOMAINS),
        "next_recovery_epoch": _zero_map(RECOVERY_DOMAINS),
        "circuit_open_until_epoch": _zero_map(RECOVERY_DOMAINS),
        "last_model_turn_probe_epoch": 0,
        "last_ha_read_probe_epoch": 0,
    }


def _migrate_state(document: dict[str, Any]) -> dict[str, Any]:
    schema = document.get("schema_version")
    if schema == SCHEMA_VERSION:
        return {**_default_state(), **document, "schema_version": SCHEMA_VERSION}
    if schema not in {1, 2}:
        raise HealthError("state_file")
    migrated = _default_state()
    local_ready = document.get("local_ready") is True
    public_ready = document.get("public_ready") is True
    failures = document.get("consecutive_failures", 0)
    if isinstance(failures, bool) or not isinstance(failures, int):
        failures = 0
    migrated.update(
        {
            "observed_epoch": document.get("observed_epoch", 0),
            "healthy": False,
            "overall_voice_ready": False,
            "owner_config_ready": True,
            "gateway_ready": local_ready,
            "public_route_ready": public_ready,
            "tailscale_ready": public_ready,
            "consecutive_failures": max(0, min(1000, failures)),
            "last_action": document.get("last_action", "none"),
            "last_error_code": document.get("last_error_code", "none"),
            "last_recovery_epoch": document.get("last_recovery_epoch", 0),
        }
    )
    migrated["failure_streaks"]["gateway_ready"] = (
        migrated["consecutive_failures"] if not local_ready else 0
    )
    migrated["failure_streaks"]["public_route_ready"] = (
        migrated["consecutive_failures"] if not public_ready else 0
    )
    return migrated


def _validate_int_map(value: Any, keys: tuple[str, ...], maximum: int) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == set(keys)
        and all(
            isinstance(item, int)
            and not isinstance(item, bool)
            and 0 <= item <= maximum
            for item in value.values()
        )
    )


def _validate_state(document: dict[str, Any]) -> None:
    integer_fields = (
        "observed_epoch",
        "consecutive_failures",
        "last_recovery_epoch",
        "last_model_turn_probe_epoch",
        "last_ha_read_probe_epoch",
    )
    if (
        document.get("schema_version") != SCHEMA_VERSION
        or any(
            not isinstance(document.get(field), bool)
            for field in (
                "healthy",
                "overall_voice_ready",
                "owner_config_ready",
                *COMPONENT_FIELDS,
            )
        )
        or any(
            isinstance(document.get(field), bool)
            or not isinstance(document.get(field), int)
            or not 0 <= document[field] <= 4_000_000_000
            for field in integer_fields
        )
        or document["consecutive_failures"] > 1000
        or document.get("last_action") not in VALID_ACTIONS
        or document.get("last_error_code") not in VALID_ERROR_CODES
        or not _validate_int_map(
            document.get("failure_streaks"), COMPONENT_FIELDS, 1000
        )
        or not _validate_int_map(
            document.get("recovery_failures"), RECOVERY_DOMAINS, 1000
        )
        or not _validate_int_map(
            document.get("next_recovery_epoch"), RECOVERY_DOMAINS, 4_000_000_000
        )
        or not _validate_int_map(
            document.get("circuit_open_until_epoch"),
            RECOVERY_DOMAINS,
            4_000_000_000,
        )
        or document["healthy"] != document["overall_voice_ready"]
    ):
        raise HealthError("state_file")


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
        document = json.loads(path.read_bytes().decode("ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HealthError("state_file") from error
    if not isinstance(document, dict):
        raise HealthError("state_file")
    migrated = _migrate_state(document)
    _validate_state(migrated)
    return migrated


def write_state(path: Path, document: dict[str, Any]) -> None:
    _require_private_directory(path.parent)
    _validate_state(document)
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


def record_configuration_failure(
    path: Path = STATE_PATH,
    *,
    wall_clock: Callable[[], float] = time.time,
) -> None:
    """Persist an owner/config fault without attempting any recovery action."""

    try:
        previous = load_state(path)
    except HealthError:
        previous = _default_state()
    observed_epoch = int(wall_clock())
    failure_streaks = {
        field: min(1000, previous["failure_streaks"][field] + 1)
        for field in COMPONENT_FIELDS
    }
    document = {
        **_default_state(),
        "observed_epoch": observed_epoch,
        "consecutive_failures": max(failure_streaks.values()),
        "failure_streaks": failure_streaks,
        "last_error_code": "configuration",
        "last_recovery_epoch": previous["last_recovery_epoch"],
        "recovery_failures": dict(previous["recovery_failures"]),
        "next_recovery_epoch": dict(previous["next_recovery_epoch"]),
        "circuit_open_until_epoch": dict(previous["circuit_open_until_epoch"]),
        "last_model_turn_probe_epoch": previous["last_model_turn_probe_epoch"],
        "last_ha_read_probe_epoch": previous["last_ha_read_probe_epoch"],
    }
    write_state(path, document)


def restart_unit(unit: str) -> None:
    if unit not in ALLOWED_UNITS:
        raise HealthError("configuration")
    try:
        for command in (
            [SYSTEMCTL, "reset-failed", "--", unit],
            [SYSTEMCTL, "restart", "--no-block", "--", unit],
        ):
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
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
    wait_seconds: float = RECOVERY_WAIT_SECONDS,
) -> tuple[bool, str]:
    deadline = clock() + wait_seconds
    while True:
        ready, error_code = _attempt(probe)
        remaining = deadline - clock()
        if ready or remaining <= 0:
            return ready, error_code
        sleeper(min(2.0, remaining))


def _collect_readiness(
    previous: dict[str, Any],
    *,
    observed_epoch: int,
    gateway_probe: Callable[[], None],
    public_probe_runner: Callable[[], None],
    tailscale_probe_runner: Callable[[], None],
    model_endpoint_probe_runner: Callable[[], None],
    model_loaded_probe_runner: Callable[[], None],
    model_turn_probe_runner: Callable[[], None],
    ha_read_probe_runner: Callable[[], None],
) -> tuple[dict[str, bool], dict[str, str], int, int]:
    readiness: dict[str, bool] = {}
    errors = {field: "none" for field in COMPONENT_FIELDS}
    for field, probe in (
        ("gateway_ready", gateway_probe),
        ("public_route_ready", public_probe_runner),
        ("tailscale_ready", tailscale_probe_runner),
        ("model_endpoint_ready", model_endpoint_probe_runner),
    ):
        readiness[field], errors[field] = _attempt(probe)

    last_model_turn = int(previous["last_model_turn_probe_epoch"])
    last_ha_read = int(previous["last_ha_read_probe_epoch"])
    if readiness["model_endpoint_ready"]:
        readiness["model_loaded"], errors["model_loaded"] = _attempt(
            model_loaded_probe_runner
        )
    else:
        readiness["model_loaded"] = False
        errors["model_loaded"] = "model_loaded"

    model_due = (
        readiness["gateway_ready"]
        and readiness["model_endpoint_ready"]
        and (
            last_model_turn == 0
            or observed_epoch - last_model_turn >= MODEL_TURN_PROBE_INTERVAL_SECONDS
            or previous.get("model_turn_ready") is not True
            or not readiness["model_loaded"]
        )
    )
    if model_due:
        readiness["model_turn_ready"], errors["model_turn_ready"] = _attempt(
            model_turn_probe_runner
        )
        last_model_turn = observed_epoch
        if readiness["model_turn_ready"] and not readiness["model_loaded"]:
            readiness["model_loaded"], errors["model_loaded"] = _attempt(
                model_loaded_probe_runner
            )
    elif readiness["gateway_ready"] and readiness["model_endpoint_ready"]:
        readiness["model_turn_ready"] = previous.get("model_turn_ready") is True
        if not readiness["model_turn_ready"]:
            errors["model_turn_ready"] = "model_turn"
    else:
        readiness["model_turn_ready"] = False
        errors["model_turn_ready"] = "model_turn"

    ha_due = (
        readiness["gateway_ready"]
        and (
            last_ha_read == 0
            or observed_epoch - last_ha_read >= HA_READ_PROBE_INTERVAL_SECONDS
            or previous.get("ha_read_ready") is not True
        )
    )
    if ha_due:
        readiness["ha_read_ready"], errors["ha_read_ready"] = _attempt(
            ha_read_probe_runner
        )
        last_ha_read = observed_epoch
    elif readiness["gateway_ready"]:
        readiness["ha_read_ready"] = previous.get("ha_read_ready") is True
        if not readiness["ha_read_ready"]:
            errors["ha_read_ready"] = "ha_read"
    else:
        readiness["ha_read_ready"] = False
        errors["ha_read_ready"] = "ha_read"
    return readiness, errors, last_model_turn, last_ha_read


def _primary_error(readiness: dict[str, bool], errors: dict[str, str]) -> str:
    for field in COMPONENT_FIELDS:
        if not readiness[field]:
            code = errors.get(field, "configuration")
            return code if code in VALID_ERROR_CODES else "configuration"
    return "none"


def _recovery_due(previous: dict[str, Any], domain: str, now: int) -> bool:
    return now >= max(
        previous["next_recovery_epoch"][domain],
        previous["circuit_open_until_epoch"][domain],
    )


def _record_recovery(
    recovery_failures: dict[str, int],
    next_recovery_epoch: dict[str, int],
    circuit_open_until_epoch: dict[str, int],
    *,
    domain: str,
    success: bool,
    now: int,
) -> None:
    if success:
        recovery_failures[domain] = 0
        next_recovery_epoch[domain] = 0
        circuit_open_until_epoch[domain] = 0
        return
    failures = min(1000, recovery_failures[domain] + 1)
    recovery_failures[domain] = failures
    exponent = min(10, max(0, failures - 1))
    delay = min(
        RECOVERY_MAX_BACKOFF_SECONDS,
        RECOVERY_BASE_BACKOFF_SECONDS * (2**exponent),
    )
    next_recovery_epoch[domain] = now + delay
    if failures >= RECOVERY_CIRCUIT_THRESHOLD:
        circuit_open_until_epoch[domain] = now + RECOVERY_CIRCUIT_SECONDS
        next_recovery_epoch[domain] = max(
            next_recovery_epoch[domain], circuit_open_until_epoch[domain]
        )


def run_once(
    config: alice_skill_gateway.GatewayConfig,
    *,
    state_path: Path = STATE_PATH,
    local_probe: Callable[[], None] | None = None,
    public_probe_runner: Callable[[], None] | None = None,
    tailscale_probe_runner: Callable[[], None] = probe_tailscale,
    model_endpoint_probe_runner: Callable[[], None] = probe_model_endpoint,
    model_loaded_probe_runner: Callable[[], None] = probe_model_loaded,
    model_turn_probe_runner: Callable[[], None] | None = None,
    ha_read_probe_runner: Callable[[], None] | None = None,
    funnel_config_checker: Callable[[], None] = probe_funnel_config,
    funnel_reasserter: Callable[[], None] = reassert_funnel,
    restarter: Callable[[str], None] = restart_unit,
    wall_clock: Callable[[], float] = time.time,
    monotonic_clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    recovery_threshold: int = RECOVERY_THRESHOLD,
    allow_recovery: bool = True,
) -> bool:
    if recovery_threshold < 2 or recovery_threshold > 10:
        raise HealthError("configuration")
    previous = load_state(state_path)
    observed_epoch = int(wall_clock())
    gateway_check = local_probe or (lambda: probe_local(config))
    public_check = public_probe_runner or (lambda: probe_public(config))
    model_turn_check = model_turn_probe_runner or (lambda: probe_model_turn(config))
    ha_read_check = ha_read_probe_runner or (lambda: probe_ha_read(config))

    def collect(seed: dict[str, Any]) -> tuple[dict[str, bool], dict[str, str], int, int]:
        return _collect_readiness(
            seed,
            observed_epoch=observed_epoch,
            gateway_probe=gateway_check,
            public_probe_runner=public_check,
            tailscale_probe_runner=tailscale_probe_runner,
            model_endpoint_probe_runner=model_endpoint_probe_runner,
            model_loaded_probe_runner=model_loaded_probe_runner,
            model_turn_probe_runner=model_turn_check,
            ha_read_probe_runner=ha_read_check,
        )

    readiness, errors, last_model_turn, last_ha_read = collect(previous)
    initial_streaks = {
        field: (
            0
            if readiness[field]
            else min(1000, previous["failure_streaks"][field] + 1)
        )
        for field in COMPONENT_FIELDS
    }
    action = "none"
    recovery_domain: str | None = None
    last_recovery_epoch = int(previous["last_recovery_epoch"])
    recovery_failures = dict(previous["recovery_failures"])
    next_recovery_epoch = dict(previous["next_recovery_epoch"])
    circuit_open_until_epoch = dict(previous["circuit_open_until_epoch"])

    try:
        if allow_recovery and (
            initial_streaks["gateway_ready"] >= recovery_threshold
            and _recovery_due(previous, "gateway", observed_epoch)
        ):
            recovery_domain = "gateway"
            action = "restart_skill"
            restarter(SKILL_UNIT)
            _wait_for(
                gateway_check,
                sleeper=sleeper,
                clock=monotonic_clock,
            )
        elif allow_recovery and (
            max(
                initial_streaks["public_route_ready"],
                initial_streaks["tailscale_ready"],
            )
            >= recovery_threshold
            and _recovery_due(previous, "public_route", observed_epoch)
        ):
            public_now, _public_error = _attempt(public_check)
            tailscale_now, _tailscale_error = _attempt(tailscale_probe_runner)
            if not public_now:
                recovery_domain = "public_route"
                if not tailscale_now:
                    action = "restart_tailscale_reassert_funnel"
                    restarter(TAILSCALE_UNIT)
                    tailscale_now, _tailscale_error = _wait_for(
                        tailscale_probe_runner,
                        sleeper=sleeper,
                        clock=monotonic_clock,
                        wait_seconds=10,
                    )
                else:
                    action = "reassert_funnel"
                if tailscale_now:
                    _attempt(funnel_config_checker)
                    _attempt(funnel_reasserter)
                    public_now, _public_error = _attempt(public_check)
                    if not public_now:
                        action = (
                            "restart_tailscale_reassert_funnel_restart_tunnel"
                            if action.startswith("restart_tailscale")
                            else "reassert_funnel_restart_tunnel"
                        )
                        restarter(TUNNEL_UNIT)
                        _wait_for(
                            public_check,
                            sleeper=sleeper,
                            clock=monotonic_clock,
                        )
        elif allow_recovery and (
            readiness["model_endpoint_ready"]
            and max(
                initial_streaks["model_loaded"],
                initial_streaks["model_turn_ready"],
            )
            >= recovery_threshold
            and _recovery_due(previous, "model", observed_epoch)
        ):
            recovery_domain = "model"
            action = "warm_model"
            _attempt(model_turn_check)
            _attempt(model_loaded_probe_runner)
        elif allow_recovery and (
            not readiness["model_endpoint_ready"]
            and initial_streaks["model_endpoint_ready"] >= recovery_threshold
        ):
            # The pinned Windows supervisor owns endpoint process recovery.  The
            # Alice guardian observes it but never restarts Tunnel for this fault.
            action = "await_model_supervisor"
    except HealthError as error:
        errors[_error_component(error.code)] = error.code

    if recovery_domain is not None:
        last_recovery_epoch = observed_epoch
        seed = {
            **previous,
            **readiness,
            "last_model_turn_probe_epoch": last_model_turn,
            "last_ha_read_probe_epoch": last_ha_read,
        }
        readiness, errors, last_model_turn, last_ha_read = collect(seed)
        target_success = {
            "gateway": readiness["gateway_ready"],
            "public_route": (
                readiness["gateway_ready"]
                and readiness["tailscale_ready"]
                and readiness["public_route_ready"]
            ),
            "model": (
                readiness["model_endpoint_ready"]
                and readiness["model_loaded"]
                and readiness["model_turn_ready"]
            ),
        }[recovery_domain]
        _record_recovery(
            recovery_failures,
            next_recovery_epoch,
            circuit_open_until_epoch,
            domain=recovery_domain,
            success=target_success,
            now=observed_epoch,
        )

    failure_streaks = {
        field: (
            0
            if readiness[field]
            else min(1000, previous["failure_streaks"][field] + 1)
        )
        for field in COMPONENT_FIELDS
    }
    overall_voice_ready = all(readiness.values())
    document = {
        **_default_state(),
        "observed_epoch": observed_epoch,
        "healthy": overall_voice_ready,
        "overall_voice_ready": overall_voice_ready,
        "owner_config_ready": True,
        **readiness,
        "consecutive_failures": 0 if overall_voice_ready else max(failure_streaks.values()),
        "failure_streaks": failure_streaks,
        "last_action": action,
        "last_error_code": (
            "none" if overall_voice_ready else _primary_error(readiness, errors)
        ),
        "last_recovery_epoch": last_recovery_epoch,
        "recovery_failures": recovery_failures,
        "next_recovery_epoch": next_recovery_epoch,
        "circuit_open_until_epoch": circuit_open_until_epoch,
        "last_model_turn_probe_epoch": last_model_turn,
        "last_ha_read_probe_epoch": last_ha_read,
    }
    write_state(state_path, document)
    return overall_voice_ready


def _error_component(code: str) -> str:
    mapping = {
        "local_probe": "gateway_ready",
        "public_probe": "public_route_ready",
        "tailscale": "tailscale_ready",
        "tailscale_policy": "tailscale_ready",
        "funnel_config": "public_route_ready",
        "model_endpoint": "model_endpoint_ready",
        "model_loaded": "model_loaded",
        "model_turn": "model_turn_ready",
        "ha_read": "ha_read_ready",
    }
    return mapping.get(code, "gateway_ready")


def check_status(
    path: Path = STATE_PATH,
    *,
    clock: Callable[[], float] = time.time,
    max_age_seconds: int = STATUS_MAX_AGE_SECONDS,
) -> None:
    document = load_state(path)
    age = clock() - document["observed_epoch"]
    if (
        document["schema_version"] != SCHEMA_VERSION
        or document["overall_voice_ready"] is not True
        or age < 0
        or age > max_age_seconds
    ):
        raise HealthError("stale_status")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--recover", action="store_true")
    action.add_argument("--probe-only", action="store_true")
    action.add_argument("--check-status", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        if arguments.check_status:
            check_status()
            print("alice_skill_health=ready")
            return 0
        if os.geteuid() != 0:
            raise HealthError("configuration")
        try:
            config = alice_skill_gateway.GatewayConfig.load()
        except alice_skill_gateway.GatewayError as error:
            record_configuration_failure()
            raise HealthError("configuration") from error
        if config.pending:
            record_configuration_failure()
            print("alice_skill_health=pending_provisioning")
            return 0
        if not run_once(config, allow_recovery=not arguments.probe_only):
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
