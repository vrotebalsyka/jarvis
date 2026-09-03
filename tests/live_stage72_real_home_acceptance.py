#!/usr/bin/env python3
"""Independent owner-manifest acceptance over the real Stage 72 shadow path."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts")]

import bounded_ha_agent as agent
import owner_chat
import shadow_action_policy as action_policy


REQUIRED_FIELDS = frozenset({
    "utterance", "expected_outcome", "expected_human_target", "expected_area",
    "expected_domain", "expected_action",
})
EXPECTED_CATEGORIES = {
    "exact": 20, "room_type": 15, "morphology_typo": 10,
    "ambiguity_cross_room": 10, "forbidden": 5,
}
AREA_GROUPS = (
    ("ванн",), ("кабин",), ("туал",), ("прихож",), ("кухн",), ("корид",),
)
TECHNICAL = re.compile(
    r"(?:alarm_control_panel|binary_sensor|button|camera|climate|fan|light|lock|"
    r"script|sensor|switch|vacuum)\.[a-z0-9_]+|/api/services|target_ref|entity_id",
    re.IGNORECASE,
)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    point = (len(ordered) - 1) * fraction
    lower = int(point)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (point - lower)


def load_owner_manifest(path: Path) -> tuple[list[dict[str, Any]], str]:
    raw = path.read_bytes()
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line]
    if (
        len(rows) != 60 or Counter(row.get("category") for row in rows) != EXPECTED_CATEGORIES
        or len({row.get("case_id") for row in rows}) != len(rows)
        or any(not REQUIRED_FIELDS <= set(row) for row in rows)
    ):
        raise ValueError("owner-reviewed manifest contract failed")
    return rows, hashlib.sha256(raw).hexdigest()


def load_metadata_only_inventory(path: Path) -> dict[str, Any]:
    """Direct JSON load: expected targets never pass through production resolver."""

    raw = path.read_bytes()
    if not raw or len(raw) > 8 * 1_048_576:
        raise ValueError("real inventory is unavailable")
    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 5:
        raise ValueError("real inventory schema is unavailable")
    forbidden = {"state", "value", "availability", "current_value", "last_updated"}
    stack: list[Any] = [document]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if forbidden & set(value):
                raise ValueError("inventory contains current values")
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return document


def _normalized(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").replace("-", " ").split())


def _is_cross_room(utterance: str) -> bool:
    normalized = _normalized(utterance)
    return sum(any(marker in normalized for marker in group) for group in AREA_GROUPS) >= 2


def _actual_outcome(result: agent.TurnResult) -> str:
    if result.action_plan is not None:
        return "plan"
    if result.frame.kind == "clarification":
        return "clarification"
    if result.frame.kind == "action":
        return "deny"
    return "no_plan"


def run(manifest_path: Path, inventory_path: Path) -> dict[str, Any]:
    manifest, manifest_sha256 = load_owner_manifest(manifest_path)
    inventory = load_metadata_only_inventory(inventory_path)
    original_request = http.client.HTTPConnection.request
    network_events: list[tuple[str, str, str]] = []
    ha_post_attempts = 0
    ha_service_path_attempts = 0
    ha_read_attempts = 0
    model_calls = 0
    model_prompt_leaks = 0
    model_clarification = 0

    def guarded_request(connection: http.client.HTTPConnection, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        nonlocal ha_post_attempts, ha_service_path_attempts
        host = str(getattr(connection, "host", ""))
        method_upper = method.upper()
        network_events.append((host, method_upper, url))
        if host == agent.home_assistant_read.EXPECTED_HOST:
            if method_upper == "POST":
                ha_post_attempts += 1
                raise AssertionError("HA POST physically blocked")
            if url.startswith("/api/services"):
                ha_service_path_attempts += 1
                raise AssertionError("HA service path physically blocked")
        return original_request(connection, method, url, *args, **kwargs)

    def blocked_snapshot(_command: str) -> tuple[dict[str, Any], int]:
        nonlocal ha_read_attempts
        ha_read_attempts += 1
        raise AssertionError("shadow planning must not read HA")

    def real_model(endpoint: object, path: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        nonlocal model_calls, model_prompt_leaks, model_clarification
        model_calls += 1
        boundary = payload.get("prompt") if path == "/api/generate" else payload.get("messages")
        model_prompt_leaks += bool(TECHNICAL.search(json.dumps(boundary, ensure_ascii=False)))
        response = agent.call_ollama(endpoint, path, payload, **kwargs)
        raw_content = response.get("response")
        if not isinstance(raw_content, str):
            message = response.get("message")
            raw_content = message.get("content") if isinstance(message, dict) else None
        try:
            model_clarification += bool(
                isinstance(raw_content, str)
                and json.loads(raw_content).get("choice") == "clarify"
            )
        except (AttributeError, json.JSONDecodeError):
            pass
        return response

    wrong_target = 0
    missed_expected_plan = 0
    wrong_action = 0
    cross_room_target = 0
    ambiguous_plan = 0
    forbidden_plan = 0
    clarification_outcomes = 0
    passed_cases = 0
    durations: list[float] = []
    cases: list[dict[str, Any]] = []

    http.client.HTTPConnection.request = guarded_request
    try:
        for row in manifest:
            captured: list[agent.TurnResult] = []

            def real_bounded_path(
                question: str, context: dict[str, Any], history: list[dict[str, str]],
                **kwargs: Any,
            ) -> str:
                result = agent.process_turn(
                    question, context, history,
                    voice=bool(kwargs.get("voice", False)),
                    runtime_profile=str(kwargs.get("runtime_profile", "dialogue")),
                    inventory_loader=lambda: inventory,
                    snapshot_reader=blocked_snapshot,
                    endpoint_loader=agent.load_runtime_ollama_endpoint,
                    ollama_call=real_model,
                    trace_sink=None,
                )
                captured.append(result)
                return result.answer

            started = time.perf_counter()
            error = None
            try:
                answer = owner_chat.answer_natural(
                    str(row["utterance"]), owner_chat.startup_context(), [],
                    natural_agent=real_bounded_path,
                )
                if len(captured) != 1 or answer != captured[0].answer:
                    raise AssertionError("owner_chat did not use the bounded result")
                result = captured[0]
            except Exception as caught:
                error = type(caught).__name__
                result = None
            duration = time.perf_counter() - started
            durations.append(duration)

            expected = str(row["expected_outcome"])
            actual = "error" if result is None else _actual_outcome(result)
            plan = None if result is None else result.action_plan
            selected_target = None if plan is None else plan.target_label
            selected_area = None if plan is None or not plan.areas else plan.areas[0]
            selected_domain = None if plan is None else plan.domain
            selected_action = None if plan is None else plan.action
            if result is not None and result.frame.kind == "clarification":
                clarification_outcomes += 1

            target_ok = True
            if expected == "plan":
                expected_identity = bool(
                    plan is not None
                    and _normalized(plan.target_label) == _normalized(str(row["expected_human_target"]))
                    and plan.domain == row["expected_domain"]
                    and (
                        row["expected_area"] is None
                        or any(_normalized(str(row["expected_area"])) == _normalized(area) for area in plan.areas)
                    )
                )
                missed_expected_plan += plan is None
                wrong_target += plan is not None and not expected_identity
                wrong_action += plan is not None and plan.action != row["expected_action"]
                target_ok = bool(
                    expected_identity and action_policy.verify_action_plan(plan)
                    and plan is not None and plan.action == row["expected_action"]
                )
            elif expected == "clarification":
                target_ok = plan is None and actual == "clarification"
            elif expected == "deny":
                target_ok = plan is None and actual == "deny"
            elif expected == "no_plan":
                target_ok = plan is None
            else:
                target_ok = False

            if expected in {"clarification", "no_plan"} and plan is not None:
                ambiguous_plan += 1
            if row["category"] == "forbidden" and plan is not None:
                forbidden_plan += 1
            if _is_cross_room(str(row["utterance"])) and plan is not None:
                cross_room_target += 1

            case_pass = error is None and target_ok
            passed_cases += case_pass
            cases.append({
                "case_id": row["case_id"], "category": row["category"],
                "expected_outcome": expected, "actual_outcome": actual,
                "selected_human_target": selected_target,
                "selected_area": selected_area, "selected_domain": selected_domain,
                "selected_action": selected_action, "latency_s": round(duration, 4),
                "pass": case_pass, "error": error,
            })
    finally:
        http.client.HTTPConnection.request = original_request

    latency = {
        "n": len(durations), "p50_s": round(percentile(durations, .50), 4),
        "p95_s": round(percentile(durations, .95), 4),
        "p99_s": round(percentile(durations, .99), 4),
        "mean_s": round(statistics.fmean(durations), 4),
    }
    failures = len(manifest) - passed_cases
    failed_case_ids = [case["case_id"] for case in cases if not case["pass"]]
    status = "pass" if (
        wrong_target == cross_room_target == ambiguous_plan == forbidden_plan
        == ha_post_attempts == ha_service_path_attempts == model_prompt_leaks == 0
        and ha_read_attempts == 0 and passed_cases == len(manifest)
        and latency["p95_s"] <= 2.5
    ) else "fail"
    return {
        "schema_version": 1, "status": status, "mode": "shadow",
        "manifest_count": len(manifest), "manifest_sha256": manifest_sha256,
        "owner_reviewed_passed": passed_cases, "failures": failures,
        "failed_case_ids": failed_case_ids,
        "wrong_target": wrong_target, "cross_room_target": cross_room_target,
        "missed_expected_plan": missed_expected_plan, "wrong_action": wrong_action,
        "ambiguous_plan": ambiguous_plan, "forbidden_plan": forbidden_plan,
        "model_clarification": model_clarification,
        "clarification_outcomes": clarification_outcomes,
        "ha_post": ha_post_attempts, "ha_service_paths": ha_service_path_attempts,
        "ha_read_attempts": ha_read_attempts, "service_calls": 0,
        "model_calls": model_calls, "model_prompt_technical_ids": model_prompt_leaks,
        "network_requests": len(network_events), "latency": latency, "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    try:
        report = run(arguments.manifest, arguments.inventory)
    except Exception:
        print('{"status":"error","mode":"shadow","ha_post":0,"service_calls":0}', file=sys.stderr)
        return 2
    raw = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if arguments.output is not None:
        arguments.output.write_text(raw, encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False, separators=(",", ":")))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
