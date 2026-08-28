#!/usr/bin/env python3
"""Minimal fail-closed GET-only Home Assistant adapter."""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import re
import socket
import ssl
import stat
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote, urlsplit

import turn_observability


sys.dont_write_bytecode = True

PROJECT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_DIR / "config" / "home-assistant.env"
EXPECTED_TOKEN_PATH = PROJECT_DIR / "secrets" / "home-assistant.token"
RUNTIME_PROJECT_DIR = Path("/opt/home-butler")
RUNTIME_CONFIG_PATH = RUNTIME_PROJECT_DIR / "config" / "home-assistant.env"
CREDENTIAL_REFERENCE = "systemd-credential:home-assistant.token"
ALLOWED_CREDENTIAL_DIRECTORIES = {
    Path("/run/credentials/home-butler.service"),
    Path("/run/credentials/home-butler-heartbeat.service"),
    Path("/run/credentials/home-butler-ha-proof.service"),
    Path("/run/credentials/home-butler-startup-ha-check.service"),
    Path("/run/credentials/home-butler-startup-self-check.service"),
    Path("/run/credentials/home-butler-startup-voice-status.service"),
    Path("/run/credentials/home-butler-incident-monitor.service"),
    Path("/run/credentials/home-butler-incident-notifier.service"),
    Path("/run/credentials/home-butler-inventory.service"),
    Path("/run/credentials/home-butler-recovery.service"),
    Path("/run/credentials/home-butler-core-recovery.service"),
    Path("/run/credentials/home-butler-voice-intent.service"),
    Path("/run/credentials/home-butler-alice-skill.service"),
    Path("/run/credentials/home-butler-local-chat.service"),
    Path("/run/credentials/home-butler-daily-report.service"),
    Path("/run/credentials/home-butler-operations-supervisor.service"),
    Path("/run/credentials/home-butler-automation-diagnostics.service"),
    Path("/run/credentials/home-butler-automation-recovery.service"),
    Path("/run/credentials/home-butler-entity-freshness.service"),
    Path("/run/credentials/home-butler-system-log-diagnostics.service"),
    Path("/run/credentials/home-butler-device-health.service"),
    Path("/run/credentials/home-butler-integration-recovery.service"),
    Path("/run/credentials/home-butler-model-study.service"),
    Path("/run/credentials/home-butler-full-entity-report.service"),
    Path("/run/credentials/home-butler-diagnostic-monitor.service"),
}
DEVICE_LEARNING_CREDENTIAL_DIRECTORY_RE = re.compile(
    r"/run/credentials/home-butler-device-learning@[a-f0-9]{64}\.service\Z"
)


def _credential_directory_allowed(path: Path) -> bool:
    return (
        path in ALLOWED_CREDENTIAL_DIRECTORIES
        or DEVICE_LEARNING_CREDENTIAL_DIRECTORY_RE.fullmatch(str(path)) is not None
    )
MAX_CONFIG_BYTES = 16_384
MAX_TOKEN_BYTES = 4_096
MAX_RESPONSE_BYTES = 4 * 1_048_576
MAX_JSON_DEPTH = 20
MAX_JSON_NODES = 100_000
MAX_ALLOWED_ENTITIES = 128
MAX_LISTED_ENTITIES = 4_096
MAX_FRIENDLY_NAME_CHARS = 120
REQUEST_TIMEOUT_SECONDS = 5.0
REQUEST_DEADLINE_SECONDS = 10.0
MAX_FUTURE_SKEW_SECONDS = 30
EXPECTED_SCHEME = "http"
EXPECTED_HOST = "192.168.1.127"
EXPECTED_PORT = 8123
ALL_ENTITIES_SENTINEL = "*"
ENTITY_ID_RE = re.compile(r"^[a-z0-9_]{1,64}\.[a-z0-9_]{1,200}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{20,4096}$")
NUMBER_STATE_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
SENSITIVE_STATE_RE = re.compile(
    r"(?:https?://|\bbearer\b|\btoken\b|\bsecret\b|"
    r"ignore[\s_]+(?:all[\s_]+)?previous|system[\s_]+prompt)",
    re.IGNORECASE,
)
SENSITIVE_FRIENDLY_NAME_RE = re.compile(
    r"(?:https?://|\bbearer\b|\btoken\b|\bsecret\b|\bpassword\b|"
    r"ignore\s+(?:all\s+)?previous|system\s+prompt|"
    r"игнорир\S*\s+инструкц\S*|системн\S*\s+промпт\S*|"
    r"\bтокен\S*\b|\bсекрет\S*\b|\bпарол\S*\b)",
    re.IGNORECASE,
)
COMMON_ENUM_STATES = {
    "off", "on", "open", "closed", "opening", "closing", "locked", "unlocked",
    "home", "not_home", "idle", "playing", "paused", "standby", "docked",
    "cleaning", "returning", "error", "ok", "problem", "active", "inactive",
}
ENUM_STATES = {
    "alarm_control_panel": {
        "disarmed", "armed_home", "armed_away", "armed_night", "armed_vacation",
        "armed_custom_bypass", "pending", "arming", "disarming", "triggered",
    },
    "binary_sensor": {"off", "on"},
    "climate": {"off", "heat", "cool", "heat_cool", "auto", "dry", "fan_only"},
}
REQUIRED_CONFIG_KEYS = {
    "HOME_ASSISTANT_URL",
    "HOME_ASSISTANT_TOKEN_FILE",
    "HOME_ASSISTANT_ALLOWED_ENTITIES",
    "HOME_ASSISTANT_MODE",
}
STATUSES = {
    "not_configured",
    "dns_failure",
    "host_unreachable",
    "port_closed",
    "unauthorized",
    "api_unavailable",
    "stale_data",
    "healthy",
}
CONTROL_DOMAINS = {
    "switch", "light", "button", "fan", "humidifier", "siren", "vacuum",
    "number", "select",
}


class AdapterError(RuntimeError):
    """A classified error safe to expose without its underlying details."""

    def __init__(self, status: str, *, configured: bool = True) -> None:
        if status not in STATUSES:
            status = "api_unavailable"
        super().__init__(status)
        self.status = status
        self.configured = configured


@dataclass(frozen=True)
class AdapterConfig:
    scheme: str
    host: str
    port: int
    token: str
    allowed_entities: tuple[str, ...]
    read_all_entities: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_open_file(
    metadata: os.stat_result,
    *,
    expected_owners: set[int],
    expected_modes: set[int],
) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid not in expected_owners
        or stat.S_IMODE(metadata.st_mode) not in expected_modes
    ):
        raise AdapterError("api_unavailable")


def _read_bounded_file(
    path: Path,
    limit: int,
    *,
    expected_owners: set[int] | None = None,
    expected_modes: set[int] | None = None,
) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as error:
        raise AdapterError("not_configured", configured=False) from error
    except OSError as error:
        raise AdapterError("api_unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        _validate_open_file(
            metadata,
            expected_owners=expected_owners or {os.geteuid()},
            expected_modes=expected_modes or {0o600},
        )
        if metadata.st_size <= 0 or metadata.st_size > limit:
            raise AdapterError("api_unavailable")
        raw = os.read(descriptor, limit + 1)
        if len(raw) > limit:
            raise AdapterError("api_unavailable")
        return raw
    finally:
        os.close(descriptor)


def _read_bounded_file_at(
    directory_fd: int,
    name: str,
    limit: int,
    *,
    expected_owners: set[int] | None = None,
    expected_modes: set[int] | None = None,
) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError as error:
        raise AdapterError("not_configured", configured=False) from error
    except OSError as error:
        raise AdapterError("api_unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        _validate_open_file(
            metadata,
            expected_owners=expected_owners or {os.geteuid()},
            expected_modes=expected_modes or {0o600},
        )
        if metadata.st_size <= 0 or metadata.st_size > limit:
            raise AdapterError("api_unavailable")
        raw = os.read(descriptor, limit + 1)
        if len(raw) > limit:
            raise AdapterError("api_unavailable")
        return raw
    finally:
        os.close(descriptor)


def _parse_env(raw: bytes) -> dict[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AdapterError("api_unavailable") from error
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise AdapterError("api_unavailable")
        key, value = line.split("=", 1)
        if key not in REQUIRED_CONFIG_KEYS or key in values:
            raise AdapterError("api_unavailable")
        if any(unicodedata.category(character).startswith("C") for character in value):
            raise AdapterError("api_unavailable")
        values[key] = value
    if set(values) != REQUIRED_CONFIG_KEYS:
        raise AdapterError("api_unavailable")
    return values


def _parse_url(value: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise AdapterError("api_unavailable") from error
    if (
        parsed.scheme != EXPECTED_SCHEME
        or parsed.hostname != EXPECTED_HOST
        or port != EXPECTED_PORT
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise AdapterError("api_unavailable")
    return parsed.scheme, parsed.hostname, port


def _validate_entity_id(entity_id: str) -> str:
    if not isinstance(entity_id, str) or not ENTITY_ID_RE.fullmatch(entity_id):
        raise AdapterError("api_unavailable")
    return entity_id


def load_config(path: Path | None = None) -> AdapterConfig:
    path = path or CONFIG_PATH
    runtime_config = path == RUNTIME_CONFIG_PATH and os.geteuid() != 0
    raw_config = _read_bounded_file(
        path,
        MAX_CONFIG_BYTES,
        expected_owners={0} if runtime_config else {os.geteuid()},
        expected_modes={0o644} if runtime_config else {0o600},
    )
    values = _parse_env(raw_config)
    if values["HOME_ASSISTANT_MODE"] != "read-only":
        raise AdapterError("api_unavailable")
    if runtime_config:
        if values["HOME_ASSISTANT_TOKEN_FILE"] != CREDENTIAL_REFERENCE:
            raise AdapterError("api_unavailable")
        credential_directory = Path(os.environ.get("CREDENTIALS_DIRECTORY", ""))
        if not _credential_directory_allowed(credential_directory):
            raise AdapterError("api_unavailable")
        token_path = credential_directory / "home-assistant.token"
    else:
        expected_token = path.resolve().parents[1] / "secrets" / "home-assistant.token"
        token_path = Path(values["HOME_ASSISTANT_TOKEN_FILE"])
        if token_path != expected_token or token_path != EXPECTED_TOKEN_PATH and path == CONFIG_PATH:
            raise AdapterError("api_unavailable")
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(token_path.parent, directory_flags)
    except FileNotFoundError as error:
        raise AdapterError("not_configured", configured=False) from error
    except OSError as error:
        raise AdapterError("api_unavailable") from error
    try:
        secret_directory = os.fstat(directory_fd)
        safe_directory = (
            stat.S_ISDIR(secret_directory.st_mode)
            and (
                runtime_config
                or (
                    secret_directory.st_uid == os.geteuid()
                    and stat.S_IMODE(secret_directory.st_mode) == 0o700
                )
            )
        )
        if not safe_directory:
            raise AdapterError("api_unavailable")
        raw_token = _read_bounded_file_at(
            directory_fd,
            token_path.name,
            MAX_TOKEN_BYTES,
            expected_owners={0, os.geteuid()} if runtime_config else {os.geteuid()},
            expected_modes={0o400, 0o600} if runtime_config else {0o600},
        )
    finally:
        os.close(directory_fd)
    try:
        token = raw_token.decode("ascii")
    except UnicodeDecodeError as error:
        raise AdapterError("api_unavailable") from error
    if not TOKEN_RE.fullmatch(token):
        raise AdapterError("api_unavailable")
    entity_text = values["HOME_ASSISTANT_ALLOWED_ENTITIES"]
    read_all_entities = entity_text == ALL_ENTITIES_SENTINEL
    entities = () if read_all_entities else tuple(entity_text.split(",")) if entity_text else ()
    if len(entities) > MAX_ALLOWED_ENTITIES or len(set(entities)) != len(entities):
        raise AdapterError("api_unavailable")
    for entity_id in entities:
        _validate_entity_id(entity_id)
    scheme, host, port = _parse_url(values["HOME_ASSISTANT_URL"])
    return AdapterConfig(scheme, host, port, token, entities, read_all_entities)


def _reject_constant(_value: str) -> None:
    raise AdapterError("api_unavailable")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AdapterError("api_unavailable")
        result[key] = value
    return result


def _check_json_bounds(value: Any, depth: int = 0) -> int:
    if depth > MAX_JSON_DEPTH:
        raise AdapterError("api_unavailable")
    nodes = 1
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 512:
                raise AdapterError("api_unavailable")
            nodes += _check_json_bounds(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            nodes += _check_json_bounds(item, depth + 1)
    elif isinstance(value, str) and len(value) > MAX_RESPONSE_BYTES:
        raise AdapterError("api_unavailable")
    if nodes > MAX_JSON_NODES:
        raise AdapterError("api_unavailable")
    return nodes


def strict_json_loads(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
        _check_json_bounds(value)
        return value
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise AdapterError("api_unavailable") from error


ConnectionFactory = Callable[..., http.client.HTTPConnection]


def _close_connection(
    connection: http.client.HTTPConnection,
    connection_socket: socket.socket | None = None,
) -> None:
    active_socket = connection_socket or getattr(connection, "sock", None)
    if active_socket is not None:
        try:
            active_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    try:
        connection.close()
    except (OSError, http.client.HTTPException):
        pass


def _abort_request(
    connection: http.client.HTTPConnection,
    socket_holder: list[socket.socket | None],
) -> None:
    _close_connection(connection, socket_holder[0])


def _default_connection(config: AdapterConfig) -> http.client.HTTPConnection:
    if config.scheme == "https":
        return http.client.HTTPSConnection(
            config.host,
            config.port,
            timeout=REQUEST_TIMEOUT_SECONDS,
            context=ssl.create_default_context(),
        )
    return http.client.HTTPConnection(
        config.host,
        config.port,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def request_json(
    config: AdapterConfig,
    path: str,
    *,
    connection_factory: Callable[[AdapterConfig], http.client.HTTPConnection] = _default_connection,
    deadline_seconds: float = REQUEST_DEADLINE_SECONDS,
) -> Any:
    if path not in {"/api/", "/api/states"}:
        raise AdapterError("api_unavailable")
    return _request_json_get(
        config,
        path,
        connection_factory=connection_factory,
        deadline_seconds=deadline_seconds,
    )


def _request_json_get(
    config: AdapterConfig,
    path: str,
    *,
    connection_factory: Callable[[AdapterConfig], http.client.HTTPConnection],
    deadline_seconds: float,
) -> Any:
    """Perform one bounded GET after the caller has validated the exact path."""

    try:
        connection = connection_factory(config)
    except (OSError, http.client.HTTPException) as error:
        raise AdapterError("host_unreachable") from error
    socket_holder: list[socket.socket | None] = [None]
    timer = threading.Timer(deadline_seconds, _abort_request, args=(connection, socket_holder))
    timer.daemon = True
    timer.start()
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {config.token}",
                "Connection": "close",
            },
        )
        socket_holder[0] = getattr(connection, "sock", None)
        response = connection.getresponse()
        content_length = response.getheader("Content-Length")
        if content_length is not None:
            try:
                parsed_length = int(content_length)
                if parsed_length < 0 or parsed_length > MAX_RESPONSE_BYTES:
                    raise AdapterError("api_unavailable")
            except ValueError as error:
                raise AdapterError("api_unavailable") from error
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise AdapterError("api_unavailable")
        if response.status in {401, 403}:
            raise AdapterError("unauthorized")
        if response.status != 200:
            raise AdapterError("api_unavailable")
        return strict_json_loads(raw)
    except AdapterError:
        raise
    except socket.gaierror as error:
        raise AdapterError("dns_failure") from error
    except ConnectionRefusedError as error:
        raise AdapterError("port_closed") from error
    except (TimeoutError, socket.timeout) as error:
        raise AdapterError("host_unreachable") from error
    except (OSError, http.client.HTTPException) as error:
        if isinstance(error, OSError) and error.errno == 111:
            raise AdapterError("port_closed") from error
        raise AdapterError("api_unavailable") from error
    finally:
        timer.cancel()
        _close_connection(connection, socket_holder[0])


def sanitize_history_response(
    raw: Any,
    entity_id: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Reduce HA history to bounded typed state evidence without attributes."""

    _validate_entity_id(entity_id)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 64:
        raise AdapterError("api_unavailable")
    if not isinstance(raw, list) or len(raw) > 1:
        raise AdapterError("api_unavailable")
    if not raw:
        return []
    series = raw[0]
    if not isinstance(series, list) or len(series) > MAX_JSON_NODES:
        raise AdapterError("api_unavailable")
    observations: list[dict[str, Any]] = []
    for item in series:
        if not isinstance(item, dict):
            raise AdapterError("api_unavailable")
        reported_entity = item.get("entity_id")
        if reported_entity is not None and reported_entity != entity_id:
            raise AdapterError("api_unavailable")
        timestamp = _parse_timestamp(item.get("last_changed") or item.get("last_updated"))
        state_kind, state_value = _normalize_state(entity_id, item.get("state"))
        observations.append({
            "state_kind": state_kind,
            "state_value": state_value,
            "source_last_updated_at": timestamp.isoformat(timespec="seconds"),
        })
    return observations[-limit:]


def request_recent_history(
    config: AdapterConfig,
    entity_id: str,
    *,
    hours: int = 6,
    limit: int = 32,
    connection_factory: Callable[[AdapterConfig], http.client.HTTPConnection] = _default_connection,
    deadline_seconds: float = REQUEST_DEADLINE_SECONDS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read one entity's recent history through a closed, GET-only contract."""

    normalized_entity = _validate_entity_id(entity_id)
    if not config.read_all_entities and normalized_entity not in config.allowed_entities:
        raise AdapterError("api_unavailable")
    if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= 24:
        raise AdapterError("api_unavailable")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 64:
        raise AdapterError("api_unavailable")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = current - timedelta(hours=hours)
    encoded_start = quote(start.isoformat(timespec="seconds"), safe="")
    encoded_entity = quote(normalized_entity, safe="")
    path = (
        f"/api/history/period/{encoded_start}"
        f"?filter_entity_id={encoded_entity}"
        "&minimal_response&no_attributes&significant_changes_only"
    )
    raw = _request_json_get(
        config,
        path,
        connection_factory=connection_factory,
        deadline_seconds=deadline_seconds,
    )
    return sanitize_history_response(raw, normalized_entity, limit=limit)


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise AdapterError("api_unavailable")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AdapterError("api_unavailable") from error
    if parsed.tzinfo is None:
        raise AdapterError("api_unavailable")
    return parsed.astimezone(timezone.utc)


def _normalize_state(entity_id: str, value: Any) -> tuple[str, str | float | None]:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return "redacted", None
    if any(unicodedata.category(character).startswith("C") for character in value):
        return "redacted", None
    if value in {"unknown", "unavailable"}:
        return "unavailable", None
    if NUMBER_STATE_RE.fullmatch(value):
        number = float(value)
        if math.isfinite(number) and abs(number) <= 1_000_000_000_000:
            return "number", number
        return "redacted", None
    domain = entity_id.split(".", 1)[0]
    if value in COMMON_ENUM_STATES or value in ENUM_STATES.get(domain, set()):
        return "enum", value
    if SENSITIVE_STATE_RE.search(value):
        return "redacted", None
    return "text", value


def sanitize_entity(
    raw: Any,
    expected_entity_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("entity_id") != expected_entity_id:
        raise AdapterError("api_unavailable")
    observed = raw.get("last_reported") or raw.get("last_updated")
    timestamp = _parse_timestamp(observed)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = (current - timestamp).total_seconds()
    if age < -MAX_FUTURE_SKEW_SECONDS:
        raise AdapterError("api_unavailable")
    state_kind, state_value = _normalize_state(expected_entity_id, raw.get("state"))
    return {
        "entity_id": expected_entity_id,
        "state_kind": state_kind,
        "state_value": state_value,
        "observed_at": current.isoformat(timespec="seconds"),
        "source_last_updated_at": timestamp.isoformat(timespec="seconds"),
    }


def sanitize_friendly_name(value: Any) -> str | None:
    """Return a bounded display name for deterministic owner-side matching.

    Home Assistant attributes remain untrusted.  The selected name is never
    added to the model-facing snapshot and only a conservative character set
    is allowed through this separate control catalogue.
    """

    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not 1 <= len(normalized) <= MAX_FRIENDLY_NAME_CHARS:
        return None
    if SENSITIVE_FRIENDLY_NAME_RE.search(normalized):
        return None
    for character in normalized:
        category = unicodedata.category(character)
        if category.startswith(("L", "N")) or character in " _-.()":
            continue
        return None
    return " ".join(normalized.split())


def _read_control_catalog(config: AdapterConfig) -> list[dict[str, Any]]:
    raw_states = request_json(config, "/api/states")
    if not isinstance(raw_states, list) or len(raw_states) > MAX_LISTED_ENTITIES:
        raise AdapterError("api_unavailable")
    catalogue: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_states:
        if not isinstance(raw, dict):
            raise AdapterError("api_unavailable")
        candidate = raw.get("entity_id")
        try:
            entity_id = _validate_entity_id(candidate)
        except AdapterError:
            continue
        domain = entity_id.split(".", 1)[0]
        if domain not in CONTROL_DOMAINS:
            continue
        if entity_id in seen:
            raise AdapterError("api_unavailable")
        seen.add(entity_id)
        attributes = raw.get("attributes")
        friendly_name = sanitize_friendly_name(
            attributes.get("friendly_name") if isinstance(attributes, dict) else None
        )
        entry: dict[str, Any] = {
            "entity_id": entity_id,
            "friendly_name": friendly_name,
            "available": raw.get("state") not in {"unknown", "unavailable"},
        }
        if domain == "select":
            raw_options = attributes.get("options") if isinstance(attributes, dict) else None
            safe_options: list[str] = []
            if isinstance(raw_options, list) and len(raw_options) <= 128:
                for option in raw_options:
                    safe = sanitize_friendly_name(option)
                    if safe is not None and safe not in safe_options:
                        safe_options.append(safe)
            entry["options"] = safe_options
        elif domain == "number":
            for key in ("min", "max", "step"):
                value = attributes.get(key) if isinstance(attributes, dict) else None
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
                    entry[key] = float(value)
        catalogue.append(entry)
    return sorted(catalogue, key=lambda item: str(item["entity_id"]))


def _probe(config: AdapterConfig) -> None:
    response = request_json(config, "/api/")
    if not isinstance(response, dict) or not isinstance(response.get("message"), str):
        raise AdapterError("api_unavailable")


def _read_entities(
    config: AdapterConfig,
    entity_ids: Sequence[str],
) -> tuple[str, list[dict[str, Any]]]:
    if not entity_ids:
        return "stale_data", []
    raw_states = request_json(config, "/api/states")
    if not isinstance(raw_states, list) or len(raw_states) > MAX_LISTED_ENTITIES:
        raise AdapterError("api_unavailable")
    selected: dict[str, Any] = {}
    wanted = set(entity_ids)
    for raw in raw_states:
        if not isinstance(raw, dict):
            raise AdapterError("api_unavailable")
        candidate = raw.get("entity_id")
        if candidate in wanted:
            if candidate in selected:
                raise AdapterError("api_unavailable")
            selected[candidate] = raw
    entities: list[dict[str, Any]] = []
    redacted_state = False
    missing_or_unavailable = False
    now = datetime.now(timezone.utc)
    for entity_id in entity_ids:
        raw = selected.get(entity_id)
        if raw is None:
            entities.append({
                "entity_id": entity_id,
                "state_kind": "unavailable",
                "state_value": None,
                "observed_at": now.isoformat(timespec="seconds"),
                "source_last_updated_at": None,
            })
            missing_or_unavailable = True
            continue
        entity = sanitize_entity(raw, entity_id, now=now)
        entities.append(entity)
        redacted_state = redacted_state or entity["state_kind"] == "redacted"
        missing_or_unavailable = missing_or_unavailable or entity["state_kind"] == "unavailable"
    status = (
        "stale_data" if redacted_state or missing_or_unavailable
        else "healthy"
    )
    return status, entities


def _read_all_entities(config: AdapterConfig) -> tuple[str, list[dict[str, Any]]]:
    raw_states = request_json(config, "/api/states")
    if not isinstance(raw_states, list) or len(raw_states) > MAX_LISTED_ENTITIES:
        raise AdapterError("api_unavailable")
    selected: dict[str, Any] = {}
    for raw in raw_states:
        if not isinstance(raw, dict):
            raise AdapterError("api_unavailable")
        candidate = raw.get("entity_id")
        try:
            entity_id = _validate_entity_id(candidate)
        except AdapterError:
            continue
        if entity_id in selected:
            raise AdapterError("api_unavailable")
        selected[entity_id] = raw
    now = datetime.now(timezone.utc)
    entities = [
        sanitize_entity(selected[entity_id], entity_id, now=now)
        for entity_id in sorted(selected)
    ]
    if not entities:
        return "stale_data", []
    incomplete = any(
        entity["state_kind"] in {"unavailable", "redacted"} for entity in entities
    )
    return "stale_data" if incomplete else "healthy", entities


def _base_result(configured: bool, status: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "observed_at": _now_iso(),
        "configured": configured,
        "status": status,
    }


def execute(command: str, entity_id: str | None = None) -> dict[str, Any]:
    config = load_config()
    read_scope = "all_entities" if config.read_all_entities else "configured_entities"
    if command == "probe":
        _probe(config)
        result = _base_result(True, "healthy")
        result["read_scope"] = read_scope
        return result
    if command == "list":
        if config.read_all_entities:
            raw = request_json(config, "/api/states")
            if not isinstance(raw, list) or len(raw) > MAX_LISTED_ENTITIES:
                raise AdapterError("api_unavailable")
            entity_ids = sorted({
                candidate for item in raw if isinstance(item, dict)
                if isinstance((candidate := item.get("entity_id")), str)
                and ENTITY_ID_RE.fullmatch(candidate)
            })
        else:
            entity_ids = list(config.allowed_entities)
        result = _base_result(True, "healthy" if entity_ids else "stale_data")
        result["read_scope"] = read_scope
        result["entity_count"] = len(entity_ids)
        result["entity_ids"] = entity_ids
        return result
    if command == "owner-list":
        raw = request_json(config, "/api/states")
        if not isinstance(raw, list) or len(raw) > MAX_LISTED_ENTITIES:
            raise AdapterError("api_unavailable")
        candidates: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise AdapterError("api_unavailable")
            candidate = item.get("entity_id")
            if isinstance(candidate, str) and ENTITY_ID_RE.fullmatch(candidate):
                candidates.add(candidate)
        result = _base_result(True, "healthy")
        result["read_scope"] = "all_entities"
        result["entity_count"] = len(candidates)
        result["entity_ids"] = sorted(candidates)
        return result
    if command == "control-catalog":
        catalogue = _read_control_catalog(config)
        result = _base_result(True, "healthy" if catalogue else "stale_data")
        result["read_scope"] = "control_entities"
        result["control_entity_count"] = len(catalogue)
        result["named_control_entity_count"] = sum(
            isinstance(item.get("friendly_name"), str) for item in catalogue
        )
        result["control_entities"] = catalogue
        return result
    if command == "get":
        _validate_entity_id(entity_id)
        if not config.read_all_entities and entity_id not in config.allowed_entities:
            raise AdapterError("api_unavailable")
        status, entities = _read_entities(config, [entity_id])
        result = _base_result(True, status)
        result["read_scope"] = read_scope
        result["entity"] = entities[0]
        return result
    if command in {"snapshot", "health"}:
        if config.read_all_entities:
            status, entities = _read_all_entities(config)
        else:
            status, entities = _read_entities(config, config.allowed_entities)
        result = _base_result(True, status)
        result.update(
            {
                "read_scope": read_scope,
                "entity_count": len(entities),
                "available_entity_count": sum(
                    entity["state_kind"] not in {"unavailable", "redacted"}
                    for entity in entities
                ),
                "unavailable_entity_count": sum(
                    entity["state_kind"] == "unavailable" for entity in entities
                ),
                "redacted_entity_count": sum(
                    entity["state_kind"] == "redacted" for entity in entities
                ),
            }
        )
        if command == "snapshot":
            result["entities"] = entities
        return result
    raise AdapterError("api_unavailable")


def execute_safely(command: str, entity_id: str | None = None) -> tuple[dict[str, Any], int]:
    started = time.monotonic()
    try:
        result, exit_code = execute(command, entity_id), 0
    except AdapterError as error:
        result = _base_result(error.configured, error.status)
        exit_code = 0 if command in {"probe", "snapshot", "health"} else 3
    except Exception:
        result, exit_code = _base_result(True, "api_unavailable"), 3
    turn_observability.record_tool_call(
        f"ha_read.{command}",
        latency_ms=round((time.monotonic() - started) * 1000),
        policy_result="allowed" if exit_code == 0 else "rejected",
        result_status=result.get("status", "unknown"),
    )
    return result, exit_code


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Home Assistant adapter")
    parser.add_argument(
        "command", choices=(
            "probe", "list", "owner-list", "control-catalog", "get", "snapshot", "health"
        )
    )
    parser.add_argument("entity_id", nargs="?")
    arguments = parser.parse_args(argv)
    if (arguments.command == "get") != (arguments.entity_id is not None):
        parser.error("entity_id is required only for get")
    result, exit_code = execute_safely(arguments.command, arguments.entity_id)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run())
