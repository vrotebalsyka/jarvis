#!/usr/bin/env python3
"""Probe the private Alice path without changing or recovering anything."""

from __future__ import annotations

import http.client
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import alice_skill_gateway  # noqa: E402
import alice_tailscale_funnel  # noqa: E402


MAX_RESPONSE_BYTES = 65_536
EXPECTED = {
    "ping": "Дворецкий на связи.",
    alice_skill_gateway.HEALTH_MODEL_COMMAND: "Локальная модель отвечает.",
    alice_skill_gateway.HEALTH_HA_READ_COMMAND: "Home Assistant доступен для чтения.",
}


class HealthError(RuntimeError):
    """Secret-free probe failure."""


def health_request(
    config: alice_skill_gateway.GatewayConfig, *, session_id: str, command: str
) -> bytes:
    if config.pending or not config.owner_ids or command not in EXPECTED:
        raise HealthError("configuration")
    return json.dumps({
        "version": "1.0",
        "request": {
            "type": "SimpleUtterance", "original_utterance": command,
            "command": command,
        },
        "session": {
            "session_id": session_id, "message_id": 0, "new": True,
            "skill_id": config.skill_id,
            "user": {"user_id": config.owner_ids[0]},
        },
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _probe(
    config: alice_skill_gateway.GatewayConfig,
    *,
    host: str,
    port: int,
    path: str,
    command: str,
    secure: bool,
) -> None:
    session = f"alice-health-{time.time_ns()}"
    connection = (
        http.client.HTTPSConnection(host, port, timeout=5)
        if secure else http.client.HTTPConnection(host, port, timeout=5)
    )
    try:
        connection.request(
            "POST", path,
            body=health_request(config, session_id=session, command=command),
            headers={"Accept": "application/json", "Content-Type": "application/json", "Connection": "close"},
        )
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, TimeoutError, http.client.HTTPException) as error:
        raise HealthError("probe_failed") from error
    finally:
        connection.close()
    if response.status != 200 or not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise HealthError("probe_failed")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise HealthError("probe_failed") from error
    answer = document.get("response") if isinstance(document, dict) else None
    expected = EXPECTED[command]
    if (
        document.get("version") != "1.0" or not isinstance(answer, dict)
        or answer.get("text") != expected or answer.get("tts") != expected
        or answer.get("end_session") is not False
    ):
        raise HealthError("probe_failed")


def probe_local(config: alice_skill_gateway.GatewayConfig, command: str = "ping") -> None:
    _probe(
        config, host="127.0.0.1", port=config.port,
        path=config.webhook_path, command=command, secure=False,
    )


def probe_public(config: alice_skill_gateway.GatewayConfig) -> None:
    try:
        origin = alice_tailscale_funnel.current_origin()
    except alice_tailscale_funnel.FunnelError as error:
        raise HealthError("public_route_unavailable") from error
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query:
        raise HealthError("public_route_unavailable")
    _probe(
        config, host=parsed.hostname, port=443,
        path=config.webhook_path, command="ping", secure=True,
    )


def run_once(config: alice_skill_gateway.GatewayConfig) -> dict[str, object]:
    probes = (
        ("local_gateway", lambda: probe_local(config)),
        ("local_model", lambda: probe_local(config, alice_skill_gateway.HEALTH_MODEL_COMMAND)),
        ("ha_read", lambda: probe_local(config, alice_skill_gateway.HEALTH_HA_READ_COMMAND)),
        ("public_gateway", lambda: probe_public(config)),
    )
    passed: list[str] = []
    for name, probe in probes:
        probe()
        passed.append(name)
    return {"schema_version": 1, "status": "healthy", "read_only": True, "probes": passed}


def main() -> int:
    try:
        result = run_once(alice_skill_gateway.GatewayConfig.load())
    except (HealthError, alice_skill_gateway.GatewayError):
        print('{"schema_version":1,"status":"unhealthy","read_only":true}', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
