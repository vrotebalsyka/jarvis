#!/usr/bin/env python3
"""Repeat Stage71/72 acceptance for Phase A with a measured read-only boundary."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
import canary_contract as contract
import home_assistant_read as reader
import live_stage71_acceptance as stage71
import live_stage72_natural_language_acceptance as natural
import live_stage72_real_home_room_type_acceptance as room_type
import websocket


def source_digest():
    paths = sorted(path for directory in ("scripts", "tests") for path in (ROOT / directory).rglob("*")
                   if path.is_file() and path.suffix in {".py", ".sh", ".jsonl"})
    entries = [(path.relative_to(ROOT).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()) for path in paths]
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def run(token_file: Path, inventory: Path):
    control = contract.load_config()
    if control.control_enabled or control.canary_live_enabled:
        raise ValueError("Phase_A_requires_OFF")
    digest = source_digest()
    network = Counter(HA_POST=0, SERVICE_CALLS=0, HA_GET=0, REGISTRY_READS=0, BLOCKED=0)
    original_http, original_ws = http.client.HTTPConnection.request, websocket.WebSocket.send

    def guarded_http(connection, method, path, *args, **kwargs):
        if connection.host == reader.EXPECTED_HOST:
            network["HA_POST"] += int(method.upper() == "POST")
            network["SERVICE_CALLS"] += int(path.startswith("/api/services"))
            allowed = method == "GET" and path in {"/api/", "/api/states"}
            network["HA_GET"] += int(allowed)
        else:
            allowed = connection.host in {"127.0.0.1", "localhost", "::1"} and connection.port == 11434 and (
                method == "GET" and path in {"/api/version", "/api/tags", "/api/ps"}
                or method == "POST" and path in {"/api/chat", "/api/generate"})
        if not allowed:
            network["BLOCKED"] += 1
            raise RuntimeError("read_only_network_boundary")
        return original_http(connection, method, path, *args, **kwargs)

    def guarded_ws(connection, payload, *args, **kwargs):
        kind = json.loads(payload).get("type")
        network["SERVICE_CALLS"] += int(kind == "call_service")
        if kind not in {"auth", "config/entity_registry/list", "config/device_registry/list", "config/area_registry/list"}:
            network["BLOCKED"] += 1
            raise RuntimeError("read_only_registry_boundary")
        network["REGISTRY_READS"] += int(kind != "auth")
        return original_ws(connection, payload, *args, **kwargs)

    suites = {}
    http.client.HTTPConnection.request, websocket.WebSocket.send = guarded_http, guarded_ws
    try:
        checks = (
            ("stage71", lambda: stage71.run(token_file)),
            ("stage72_natural", lambda: natural.run(ROOT / "tests/data/stage72_blind_natural_language_100.jsonl")),
            ("stage72_room_type", lambda: room_type.run(ROOT / "tests/data/stage72_real_home_room_type_owner_reviewed.jsonl", inventory)),
        )
        for name, check in checks:
            before = dict(network)
            try:
                result = check()
                result = {key: value for key, value in result.items() if key != "cases"}
            except Exception as error:
                result = {"status": "error", "error_type": type(error).__name__}
            result["instrumented_network"] = {key: value - before.get(key, 0) for key, value in network.items()}
            suites[name] = result
            print(json.dumps({"suite": name, **result}, ensure_ascii=False), flush=True)
            if any(network[key] for key in ("HA_POST", "SERVICE_CALLS", "BLOCKED")):
                break
    finally:
        http.client.HTTPConnection.request, websocket.WebSocket.send = original_http, original_ws
    unchanged = source_digest() == digest
    passed = len(suites) == 3 and all(result["status"] == "pass" for result in suites.values())
    passed = passed and unchanged and not any(network[key] for key in ("HA_POST", "SERVICE_CALLS", "BLOCKED"))
    return {"status": "pass" if passed else "fail", "scope": "stage71_72_regressions_only",
            "phase_a_acceptance_complete": False, "source_digest": digest, "source_unchanged": unchanged,
            "network": dict(network), "suites": suites}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run(args.token_file, args.inventory)
    except Exception as error:
        print(json.dumps({"status": "error", "error_type": type(error).__name__}))
        return 2
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "suites"}, ensure_ascii=False))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
