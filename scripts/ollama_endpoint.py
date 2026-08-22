#!/usr/bin/env python3
"""Load the one trusted Ollama endpoint and fail closed on stale WSL routing."""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import stat
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit


ENV_KEY = "HOME_BUTLER_OLLAMA_BASE_URL"
ENV_PATH = Path(__file__).resolve().parents[1] / "hermes" / ".env"
ROUTE_PATH = Path("/proc/net/route")
MAX_ENV_BYTES = 262_144
MAX_ROUTE_BYTES = 65_536
OLLAMA_PORT = 11434
LOCAL_FALLBACK_URL = "http://127.0.0.1:11434"
PROBE_TIMEOUT_SECONDS = 2
MAX_STARTUP_WAIT_SECONDS = 120
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class EndpointConfigError(RuntimeError):
    """The pinned endpoint is unsafe, ambiguous, or stale."""


@dataclass(frozen=True)
class OllamaEndpoint:
    base_url: str
    host: str
    port: int


def _read_private_env(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise EndpointConfigError("endpoint configuration is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        runtime_copy = path == ENV_PATH and str(path).startswith("/opt/home-butler/")
        safe_owner_and_mode = (
            metadata.st_uid == 0 and stat.S_IMODE(metadata.st_mode) == 0o644
            if runtime_copy
            else metadata.st_uid == os.geteuid()
            and stat.S_IMODE(metadata.st_mode) == 0o600
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not safe_owner_and_mode
            or metadata.st_nlink != 1
            or metadata.st_size > MAX_ENV_BYTES
        ):
            raise EndpointConfigError("endpoint configuration has unsafe metadata")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_ENV_BYTES + 1)
        if len(raw) > MAX_ENV_BYTES:
            raise EndpointConfigError("endpoint configuration is too large")
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EndpointConfigError("endpoint configuration is malformed") from error
    finally:
        os.close(descriptor)


def _read_default_gateway(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise EndpointConfigError("WSL route table is unavailable") from error
    if len(raw) > MAX_ROUTE_BYTES:
        raise EndpointConfigError("WSL route table is too large")
    candidates: list[tuple[int, str]] = []
    try:
        lines = raw.decode("ascii").splitlines()[1:]
        for line in lines:
            fields = line.split()
            if len(fields) < 8:
                continue
            destination, gateway_hex, flags_hex, metric_text, mask = (
                fields[1], fields[2], fields[3], fields[6], fields[7]
            )
            flags = int(flags_hex, 16)
            if destination != "00000000" or mask != "00000000" or flags & 0x3 != 0x3:
                continue
            gateway = ipaddress.IPv4Address(struct.pack("<I", int(gateway_hex, 16)))
            candidates.append((int(metric_text), str(gateway)))
    except (UnicodeDecodeError, ValueError, struct.error) as error:
        raise EndpointConfigError("WSL route table is malformed") from error
    if not candidates:
        raise EndpointConfigError("WSL default gateway is unavailable")
    return min(candidates)[1]


def load_ollama_endpoint(
    *,
    env_path: Path = ENV_PATH,
    route_path: Path = ROUTE_PATH,
) -> OllamaEndpoint:
    values: list[str] = []
    for raw_line in _read_private_env(env_path).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key == ENV_KEY:
            values.append(value)
    if len(values) != 1:
        raise EndpointConfigError("endpoint configuration is missing or duplicated")

    value = values[0]
    parsed = urlsplit(value)
    try:
        port = parsed.port
        address = ipaddress.IPv4Address(parsed.hostname or "")
    except ValueError as error:
        raise EndpointConfigError("endpoint URL is malformed") from error
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or port != OLLAMA_PORT
        or parsed.hostname != str(address)
        or not any(address in network for network in PRIVATE_NETWORKS)
        or value != f"http://{address}:{OLLAMA_PORT}"
    ):
        raise EndpointConfigError("endpoint URL is outside the trusted private boundary")
    if str(address) != _read_default_gateway(route_path):
        raise EndpointConfigError("endpoint does not match the current WSL gateway")
    return OllamaEndpoint(value, str(address), OLLAMA_PORT)


def _probe(endpoint: OllamaEndpoint) -> bool:
    connection = http.client.HTTPConnection(
        endpoint.host,
        endpoint.port,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    try:
        connection.request("GET", "/api/version", headers={"Connection": "close"})
        response = connection.getresponse()
        raw = response.read(4097)
        if response.status != 200 or not 0 < len(raw) <= 4096:
            return False
        document = json.loads(raw.decode("utf-8"))
        return (
            isinstance(document, dict)
            and set(document) == {"version"}
            and isinstance(document["version"], str)
            and 0 < len(document["version"]) <= 64
        )
    except (
        OSError,
        TimeoutError,
        http.client.HTTPException,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return False
    finally:
        try:
            connection.close()
        except (OSError, http.client.HTTPException):
            pass


def load_runtime_ollama_endpoint() -> OllamaEndpoint:
    """Prefer the guarded Windows GPU endpoint and fail over only to loopback."""

    return wait_for_runtime_ollama_endpoint()


def wait_for_runtime_ollama_endpoint(
    *,
    prefer_primary_seconds: int = 0,
    wait_seconds: int = 0,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> OllamaEndpoint:
    """Wait briefly for the GPU endpoint at boot, then allow loopback fallback."""

    if (
        isinstance(prefer_primary_seconds, bool)
        or isinstance(wait_seconds, bool)
        or not isinstance(prefer_primary_seconds, int)
        or not isinstance(wait_seconds, int)
        or prefer_primary_seconds < 0
        or wait_seconds < 0
        or prefer_primary_seconds > wait_seconds
        or wait_seconds > MAX_STARTUP_WAIT_SECONDS
    ):
        raise EndpointConfigError("endpoint wait policy is invalid")

    started = clock()
    prefer_deadline = started + prefer_primary_seconds
    final_deadline = started + wait_seconds
    fallback = OllamaEndpoint(LOCAL_FALLBACK_URL, "127.0.0.1", OLLAMA_PORT)

    while True:
        try:
            primary = load_ollama_endpoint()
        except EndpointConfigError:
            primary = None
        if primary is not None and _probe(primary):
            return primary

        current = clock()
        if current >= prefer_deadline and _probe(fallback):
            return fallback
        if current >= final_deadline:
            raise EndpointConfigError("no trusted local Ollama endpoint is reachable")
        sleeper(min(1.0, final_deadline - current))


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    prefer_primary_seconds = 0
    wait_seconds = 0
    if arguments:
        if len(arguments) != 4 or arguments[0] != "--prefer-primary-seconds" or arguments[2] != "--wait-seconds":
            print("Ollama endpoint guard usage error", file=sys.stderr)
            return 2
        try:
            prefer_primary_seconds = int(arguments[1], 10)
            wait_seconds = int(arguments[3], 10)
        except ValueError:
            print("Ollama endpoint guard usage error", file=sys.stderr)
            return 2
    try:
        endpoint = wait_for_runtime_ollama_endpoint(
            prefer_primary_seconds=prefer_primary_seconds,
            wait_seconds=wait_seconds,
        )
    except EndpointConfigError:
        print("Ollama endpoint guard failed", file=sys.stderr)
        return 2
    print(endpoint.base_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
