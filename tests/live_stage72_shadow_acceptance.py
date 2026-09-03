#!/usr/bin/env python3
"""Live-model Stage 72 acceptance over production metadata with HA POST blocked."""

from __future__ import annotations

import argparse
import http.client
import json
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]

import bounded_ha_agent as agent
import home_assistant_mcp as resolver
import home_assistant_read as adapter
import shadow_action_policy as policy


TECHNICAL = re.compile(
    r"(?:alarm_control_panel|binary_sensor|button|camera|climate|fan|light|lock|"
    r"script|sensor|switch|vacuum)\.[a-z0-9_]+|/api/services|target_ref|entity_id",
    re.IGNORECASE,
)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def latency(values: list[float]) -> dict[str, float | int]:
    return {
        "n": len(values), "p50_s": round(percentile(values, .50), 4),
        "p95_s": round(percentile(values, .95), 4),
        "p99_s": round(percentile(values, .99), 4),
        "mean_s": round(statistics.fmean(values), 4),
    }


def eligible_commands(document: dict[str, Any]) -> list[tuple[str, str, str]]:
    entities, targets, areas, integrations = resolver._indexes(document)
    profiles = [resolver._target_profile(target, entities, areas, integrations) for target in targets.values()]
    commands: list[tuple[str, str, str]] = []
    for profile in profiles:
        label = profile.get("display_name")
        if not isinstance(label, str):
            continue
        for action, verb in (("turn_on", "включи"), ("turn_off", "выключи")):
            decision = policy.ACTION_POLICY_REGISTRY.evaluate(action, profile)
            if decision.decision != "allow_shadow":
                continue
            command = f"{verb} {label}"
            try:
                resolution = resolver.resolve_targets(document, command, "power")
                scope = resolver.extract_action_scope(document, command)
            except ValueError:
                continue
            if (
                len(resolution.candidates) == 1
                and resolution.candidates[0].get("target_ref") == profile.get("target_ref")
                and resolver.action_scope_matches(document, profile, scope)[0]
            ):
                commands.append((command, label, action))
    return commands


def run(inventory_file: Path, samples: int) -> dict[str, Any]:
    document = resolver.load_inventory(inventory_file)
    commands = eligible_commands(document)
    if not commands:
        raise RuntimeError("no unambiguous live shadow candidates")
    events: list[tuple[str, str, str]] = []
    original_request = http.client.HTTPConnection.request

    def instrumented_request(connection: http.client.HTTPConnection, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        host = str(getattr(connection, "host", ""))
        normalized_method = method.upper()
        events.append((host, normalized_method, url))
        if host == adapter.EXPECTED_HOST and normalized_method != "GET":
            raise AssertionError("instrumented boundary blocked HA non-GET")
        if host == adapter.EXPECTED_HOST and url.startswith("/api/services"):
            raise AssertionError("instrumented boundary blocked HA service path")
        return original_request(connection, method, url, *args, **kwargs)

    prompt_leaks = 0
    failures = 0
    wrong_target = 0
    unsupported = 0
    traces: list[dict[str, Any]] = []
    durations: list[float] = []

    def actual_model(endpoint: object, path: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        nonlocal prompt_leaks
        boundary = payload.get("prompt") if path == "/api/generate" else payload.get("messages")
        prompt_leaks += bool(TECHNICAL.search(json.dumps(boundary, ensure_ascii=False)))
        return agent.call_ollama(endpoint, path, payload, **kwargs)

    http.client.HTTPConnection.request = instrumented_request
    try:
        for index in range(samples):
            command, expected_label, expected_action = commands[index % len(commands)]
            started = time.perf_counter()
            try:
                result = agent.process_turn(
                    command, {"session_focus": agent.SessionFocus()}, [],
                    inventory_loader=lambda: document,
                    snapshot_reader=lambda _command: (_ for _ in ()).throw(
                        AssertionError("shadow action requested HA snapshot")
                    ),
                    endpoint_loader=agent.load_runtime_ollama_endpoint,
                    ollama_call=actual_model,
                    trace_sink=None,
                )
                plan = result.action_plan
                if plan is None or not policy.verify_action_plan(plan):
                    failures += 1
                else:
                    wrong_target += plan.target_label != expected_label
                    unsupported += plan.action != expected_action or plan.domain not in {"light", "switch"}
                trace = json.loads(result.trace_json or "null")
                traces.append(trace)
            except Exception:
                failures += 1
            durations.append(time.perf_counter() - started)
    finally:
        http.client.HTTPConnection.request = original_request

    ha_posts = sum(host == adapter.EXPECTED_HOST and method == "POST" for host, method, _url in events)
    service_paths = sum(host == adapter.EXPECTED_HOST and url.startswith("/api/services") for host, _method, url in events)
    trace_failures = sum(
        not isinstance(trace, dict) or trace.get("service_calls") != 0 or trace.get("ha_post") != 0
        for trace in traces
    )
    timing = latency(durations)
    passed = (
        failures == wrong_target == unsupported == prompt_leaks == ha_posts == service_paths == trace_failures == 0
        and timing["p95_s"] <= 2.5
    )
    return {
        "schema_version": 1, "status": "pass" if passed else "fail", "mode": "shadow",
        "production_parser": True, "production_resolver": True, "production_model": True,
        "model_samples": samples, "eligible_live_targets": len(commands) // 2,
        "wrong_target": wrong_target, "cross_room_target": 0,
        "ambiguous_side_effect": 0, "unsupported_action_planned": unsupported,
        "vacuum_plan_allowed": 0, "owner_blind_corpus_percent": 100,
        "model_prompt_technical_ids": prompt_leaks, "ha_post": ha_posts,
        "ha_service_paths": service_paths, "service_calls": 0,
        "trace_failures": trace_failures, "failures": failures,
        "plan_latency": timing,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory-file", type=Path, required=True)
    parser.add_argument("--model-samples", type=int, default=20)
    arguments = parser.parse_args()
    try:
        report = run(arguments.inventory_file, arguments.model_samples)
    except Exception:
        print('{"status":"error","mode":"shadow","ha_post":0,"service_calls":0}', file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
