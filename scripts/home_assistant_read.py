#!/usr/bin/env python3
"""Fail-closed Home Assistant adapter with an intentionally GET-only surface."""

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
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit


sys.dont_write_bytecode = True
PROJECT_DIR = Path(__file__).resolve().parents[1]
CONFIG_CANDIDATES = (
    Path("/opt/home-butler/config/home-assistant.env"),
    PROJECT_DIR / "config" / "home-assistant.env",
)
CREDENTIAL_NAME = "home-assistant.token"
MAX_CONFIG_BYTES = 16_384
MAX_TOKEN_BYTES = 4_096
MAX_RESPONSE_BYTES = 4 * 1_048_576
MAX_JSON_DEPTH = 20
MAX_JSON_NODES = 100_000
MAX_ALLOWED_ENTITIES = 256
MAX_LISTED_ENTITIES = 4_096
MAX_FRIENDLY_NAME_CHARS = 120
REQUEST_TIMEOUT_SECONDS = 5.0
EXPECTED_SCHEME = "http"
EXPECTED_HOST = "192.168.1.127"
EXPECTED_PORT = 8123
ALL_ENTITIES_SENTINEL = "*"
ENTITY_ID_RE = re.compile(r"^[a-z0-9_]{1,64}\.[a-z0-9_]{1,200}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{20,4096}$")
NUMBER_STATE_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
SENSITIVE_TEXT_RE = re.compile(
    r"(?:https?://|\bbearer\b|\btoken\b|\bsecret\b|\bpassword\b|"
    r"ignore\s+(?:all\s+)?previous|system\s+prompt|"
    r"игнорир\S*\s+инструкц\S*|системн\S*\s+промпт\S*|"
    r"\bтокен\S*\b|\bсекрет\S*\b|\bпарол\S*\b)",
    re.IGNORECASE,
)
ENUM_STATES = frozenset({
    "off", "on", "open", "closed", "opening", "closing", "locked",
    "unlocked", "home", "not_home", "idle", "playing", "paused",
    "standby", "docked", "cleaning", "returning", "error", "ok",
    "problem", "active", "inactive", "disarmed", "armed_home",
    "armed_away", "triggered", "heat", "cool", "auto", "dry",
})
STATUSES = frozenset({
    "not_configured", "dns_failure", "host_unreachable", "port_closed",
    "unauthorized", "api_unavailable", "stale_data", "healthy",
})


class AdapterError(RuntimeError):
    """Classified, secret-free adapter failure."""

    def __init__(self, status: str, *, configured: bool = True) -> None:
        self.status = status if status in STATUSES else "api_unavailable"
        self.configured = configured
        super().__init__(self.status)


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


def _read_private_file(
    path: Path, limit: int, *, allowed_modes: frozenset[int] = frozenset({0o400, 0o600, 0o640})
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
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(metadata.st_mode) not in allowed_modes
            or not 0 < metadata.st_size <= limit
        ):
            raise AdapterError("api_unavailable")
        raw = os.read(descriptor, limit + 1)
        if len(raw) > limit:
            raise AdapterError("api_unavailable")
        return raw
    finally:
        os.close(descriptor)


def _parse_env(raw: bytes) -> dict[str, str]:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise AdapterError("api_unavailable") from error
    allowed = {
        "HOME_ASSISTANT_URL", "HOME_ASSISTANT_TOKEN_FILE",
        "HOME_ASSISTANT_ALLOWED_ENTITIES", "HOME_ASSISTANT_MODE",
    }
    values: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise AdapterError("api_unavailable")
        key, value = line.split("=", 1)
        if key not in allowed or key in values:
            raise AdapterError("api_unavailable")
        values[key] = value.strip()
    if set(values) != allowed:
        raise AdapterError("api_unavailable")
    return values


def _load_token(reference: str) -> str:
    if reference != "systemd-credential:home-assistant.token":
        raise AdapterError("api_unavailable")
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        raise AdapterError("not_configured", configured=False)
    try:
        raw = _read_private_file(Path(directory) / CREDENTIAL_NAME, MAX_TOKEN_BYTES)
        token = raw.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise AdapterError("api_unavailable") from error
    if TOKEN_RE.fullmatch(token) is None:
        raise AdapterError("api_unavailable")
    return token


def load_config(path: Path | None = None) -> AdapterConfig:
    selected = path
    if selected is None:
        selected = next((candidate for candidate in CONFIG_CANDIDATES if candidate.exists()), None)
    if selected is None:
        raise AdapterError("not_configured", configured=False)
    values = _parse_env(_read_private_file(
        selected, MAX_CONFIG_BYTES,
        allowed_modes=frozenset({0o400, 0o444, 0o600, 0o640, 0o644}),
    ))
    parsed = urlsplit(values["HOME_ASSISTANT_URL"])
    try:
        port = parsed.port
    except ValueError as error:
        raise AdapterError("api_unavailable") from error
    if (
        parsed.scheme != EXPECTED_SCHEME or parsed.hostname != EXPECTED_HOST
        or port != EXPECTED_PORT or parsed.path not in {"", "/"}
        or parsed.query or parsed.fragment or parsed.username or parsed.password
    ):
        raise AdapterError("api_unavailable")
    mode = values["HOME_ASSISTANT_MODE"]
    raw_entities = values["HOME_ASSISTANT_ALLOWED_ENTITIES"]
    read_all = mode == "read_all" and raw_entities == ALL_ENTITIES_SENTINEL
    if mode not in {"read_all", "allowlist"}:
        raise AdapterError("api_unavailable")
    entities = () if read_all else tuple(item for item in raw_entities.split(",") if item)
    if not read_all and (
        not entities or len(entities) > MAX_ALLOWED_ENTITIES
        or len(set(entities)) != len(entities)
        or any(ENTITY_ID_RE.fullmatch(item) is None for item in entities)
    ):
        raise AdapterError("api_unavailable")
    return AdapterConfig(
        parsed.scheme, parsed.hostname, port, _load_token(values["HOME_ASSISTANT_TOKEN_FILE"]),
        entities, read_all,
    )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AdapterError("api_unavailable")
        result[key] = value
    return result


def strict_json_loads(raw: bytes) -> Any:
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AdapterError("api_unavailable") from error
    nodes = 0
    stack: list[tuple[Any, int]] = [(document, 1)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise AdapterError("api_unavailable")
        if isinstance(value, dict):
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
    return document


def _validate_entity_id(value: Any) -> str:
    if not isinstance(value, str) or ENTITY_ID_RE.fullmatch(value) is None:
        raise AdapterError("api_unavailable")
    return value


def sanitize_friendly_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not 1 <= len(normalized) <= MAX_FRIENDLY_NAME_CHARS or SENSITIVE_TEXT_RE.search(normalized):
        return None
    if any(
        not (unicodedata.category(char).startswith(("L", "N")) or char in " _-.()")
        for char in normalized
    ):
        return None
    return normalized


def _connection(config: AdapterConfig) -> http.client.HTTPConnection:
    if config.scheme == "https":
        return http.client.HTTPSConnection(
            config.host, config.port, timeout=REQUEST_TIMEOUT_SECONDS,
            context=ssl.create_default_context(),
        )
    return http.client.HTTPConnection(config.host, config.port, timeout=REQUEST_TIMEOUT_SECONDS)


def request_json(
    config: AdapterConfig,
    path: str,
    *,
    connection_factory: Callable[[AdapterConfig], http.client.HTTPConnection] = _connection,
    deadline_seconds: float = REQUEST_TIMEOUT_SECONDS,
) -> Any:
    del deadline_seconds  # connection timeout is the single bound
    if path not in {"/api/", "/api/states"}:
        raise AdapterError("api_unavailable")
    connection: http.client.HTTPConnection | None = None
    try:
        connection = connection_factory(config)
        connection.request("GET", path, headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {config.token}",
            "Connection": "close",
        })
        response = connection.getresponse()
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
        raise AdapterError("api_unavailable") from error
    finally:
        if connection is not None:
            connection.close()


def _state(entity_id: str, raw: Any, now: datetime) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("entity_id") != entity_id:
        raise AdapterError("api_unavailable")
    updated = raw.get("last_reported") or raw.get("last_updated")
    if not isinstance(updated, str) or not 1 <= len(updated) <= 64:
        raise AdapterError("api_unavailable")
    try:
        parsed = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except ValueError as error:
        raise AdapterError("api_unavailable") from error
    if parsed.tzinfo is None:
        raise AdapterError("api_unavailable")
    value = raw.get("state")
    kind = "redacted"
    safe_value: str | float | None = None
    if isinstance(value, str) and 1 <= len(value) <= 128 and not SENSITIVE_TEXT_RE.search(value):
        if value == "unknown":
            kind = "unknown"
        elif value == "unavailable":
            kind = "unavailable"
        elif NUMBER_STATE_RE.fullmatch(value):
            number = float(value)
            if math.isfinite(number) and abs(number) <= 1_000_000_000_000:
                kind, safe_value = "number", number
        elif value in ENUM_STATES:
            kind, safe_value = "enum", value
        elif not any(unicodedata.category(char).startswith("C") for char in value):
            kind, safe_value = "text", value
    return {
        "entity_id": entity_id,
        "state_kind": kind,
        "state_value": safe_value,
        "observed_at": now.isoformat(timespec="seconds"),
        "source_last_updated_at": parsed.astimezone(timezone.utc).isoformat(timespec="seconds"),
    }


def _states(config: AdapterConfig) -> tuple[str, list[dict[str, Any]]]:
    raw = request_json(config, "/api/states")
    if not isinstance(raw, list) or len(raw) > MAX_LISTED_ENTITIES:
        raise AdapterError("api_unavailable")
    wanted = None if config.read_all_entities else set(config.allowed_entities)
    indexed: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise AdapterError("api_unavailable")
        candidate = item.get("entity_id")
        if not isinstance(candidate, str) or ENTITY_ID_RE.fullmatch(candidate) is None:
            continue
        if wanted is None or candidate in wanted:
            if candidate in indexed:
                raise AdapterError("api_unavailable")
            indexed[candidate] = item
    selected = sorted(indexed) if wanted is None else list(config.allowed_entities)
    now = datetime.now(timezone.utc)
    entities = []
    for entity_id in selected:
        item = indexed.get(entity_id)
        if item is None:
            entities.append({
                "entity_id": entity_id, "state_kind": "unavailable", "state_value": None,
                "observed_at": now.isoformat(timespec="seconds"),
                "source_last_updated_at": None,
            })
        else:
            entities.append(_state(entity_id, item, now))
    status = "healthy" if entities and all(
        item["state_kind"] not in {"unavailable", "redacted"} for item in entities
    ) else "stale_data"
    return status, entities


def execute(command: str, entity_id: str | None = None) -> dict[str, Any]:
    config = load_config()
    base = {"schema_version": 1, "observed_at": _now_iso(), "configured": True}
    if command == "probe":
        result = request_json(config, "/api/")
        if not isinstance(result, dict) or not isinstance(result.get("message"), str):
            raise AdapterError("api_unavailable")
        return {**base, "status": "healthy", "service_calls": 0}
    status, entities = _states(config)
    if command == "list":
        return {
            **base, "status": status, "entity_count": len(entities),
            "entity_ids": [item["entity_id"] for item in entities], "service_calls": 0,
        }
    if command == "get":
        selected = _validate_entity_id(entity_id)
        match = next((item for item in entities if item["entity_id"] == selected), None)
        if match is None:
            raise AdapterError("api_unavailable")
        return {**base, "status": status, "entity": match, "service_calls": 0}
    if command == "snapshot":
        return {
            **base, "status": status, "entity_count": len(entities),
            "available_entity_count": sum(item["state_kind"] not in {"unavailable", "redacted"} for item in entities),
            "unavailable_entity_count": sum(item["state_kind"] == "unavailable" for item in entities),
            "redacted_entity_count": sum(item["state_kind"] == "redacted" for item in entities),
            "entities": entities, "service_calls": 0,
        }
    raise AdapterError("api_unavailable")


def execute_safely(command: str, entity_id: str | None = None) -> tuple[dict[str, Any], int]:
    try:
        return execute(command, entity_id), 0
    except AdapterError as error:
        return {
            "schema_version": 1, "observed_at": _now_iso(),
            "configured": error.configured, "status": error.status, "service_calls": 0,
        }, 3
    except Exception:
        return {
            "schema_version": 1, "observed_at": _now_iso(), "configured": True,
            "status": "api_unavailable", "service_calls": 0,
        }, 3


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GET-only Home Assistant adapter")
    parser.add_argument("command", choices=("probe", "list", "get", "snapshot"))
    parser.add_argument("entity_id", nargs="?")
    arguments = parser.parse_args(argv)
    if (arguments.command == "get") != (arguments.entity_id is not None):
        parser.error("entity_id is required only for get")
    result, exit_code = execute_safely(arguments.command, arguments.entity_id)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run())
