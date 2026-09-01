#!/usr/bin/env python3
"""Return the one loopback Ollama endpoint after a bounded readiness probe."""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
from dataclasses import dataclass
from typing import Callable, Sequence


OLLAMA_PORT = 11434
LOCAL_URL = f"http://127.0.0.1:{OLLAMA_PORT}"
PROBE_TIMEOUT_SECONDS = 2
MAX_STARTUP_WAIT_SECONDS = 120


class EndpointConfigError(RuntimeError):
    """The fixed local endpoint is unavailable."""


@dataclass(frozen=True)
class OllamaEndpoint:
    base_url: str
    host: str
    port: int


ENDPOINT = OllamaEndpoint(LOCAL_URL, "127.0.0.1", OLLAMA_PORT)


def _probe(endpoint: OllamaEndpoint = ENDPOINT) -> bool:
    connection = http.client.HTTPConnection(
        endpoint.host, endpoint.port, timeout=PROBE_TIMEOUT_SECONDS
    )
    try:
        connection.request("GET", "/api/version", headers={"Connection": "close"})
        response = connection.getresponse()
        raw = response.read(4097)
        document = json.loads(raw.decode("utf-8"))
        return (
            response.status == 200 and 0 < len(raw) <= 4096
            and isinstance(document, dict) and set(document) == {"version"}
            and isinstance(document["version"], str) and bool(document["version"])
        )
    except (OSError, TimeoutError, http.client.HTTPException, UnicodeError, json.JSONDecodeError):
        return False
    finally:
        connection.close()


def wait_for_runtime_ollama_endpoint(
    *,
    prefer_primary_seconds: int = 0,
    wait_seconds: int = 0,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> OllamaEndpoint:
    del prefer_primary_seconds
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, int) or not 0 <= wait_seconds <= MAX_STARTUP_WAIT_SECONDS:
        raise EndpointConfigError("endpoint wait policy is invalid")
    deadline = clock() + wait_seconds
    while True:
        if _probe(ENDPOINT):
            return ENDPOINT
        if clock() >= deadline:
            raise EndpointConfigError("local Ollama endpoint is unavailable")
        sleeper(min(1.0, max(0.0, deadline - clock())))


def load_runtime_ollama_endpoint() -> OllamaEndpoint:
    return wait_for_runtime_ollama_endpoint()


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefer-primary-seconds", type=int, default=0)
    parser.add_argument("--wait-seconds", type=int, default=0)
    arguments = parser.parse_args(argv)
    try:
        endpoint = wait_for_runtime_ollama_endpoint(
            prefer_primary_seconds=arguments.prefer_primary_seconds,
            wait_seconds=arguments.wait_seconds,
        )
    except EndpointConfigError:
        print("OLLAMA_ENDPOINT_UNAVAILABLE", file=sys.stderr)
        return 2
    print(endpoint.base_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
