#!/usr/bin/env python3
"""Blind natural-language acceptance through the real Stage 72 model path."""

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
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]

import bounded_ha_agent as agent
import owner_chat
import shadow_action_policy as action_policy
import stage72_corpus
import stage72_fixtures


EXPECTED_SHA256 = "c7481eab286f10adb4abde4c8ea7577c2053c81dadf60ade03be7ca51896524a"
EXPECTED_CATEGORIES = {
    "natural_deterministic": 35, "natural_model": 30, "negation": 10,
    "ambiguity": 10, "forbidden": 5, "follow_up": 10,
}
REQUIRED_FIELDS = frozenset({
    "case_id", "category", "utterance", "expected_outcome",
    "expected_human_target", "expected_area", "expected_domain",
    "expected_action", "expected_action_intent",
})
TECHNICAL = re.compile(
    r"(?:alarm_control_panel|binary_sensor|button|camera|climate|fan|light|lock|"
    r"script|sensor|switch|vacuum)\.[a-z0-9_]+|/api/services|target_ref|entity_id",
    re.IGNORECASE,
)
AREA_MARKERS = ("ванн", "кабин", "туал", "прихож", "кухн", "корид", "входн")


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    point = (len(ordered) - 1) * fraction
    lower = int(point)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (point - lower)


def load_blind_manifest(path: Path) -> tuple[list[dict[str, Any]], str]:
    raw = path.read_bytes()
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line]
    digest = hashlib.sha256(raw).hexdigest()
    old_utterances = {row["utterance"] for row in stage72_corpus.raw_corpus()}
    if (
        digest != EXPECTED_SHA256 or len(rows) != 100
        or Counter(row.get("category") for row in rows) != EXPECTED_CATEGORIES
        or len({row.get("case_id") for row in rows}) != 100
        or any(not REQUIRED_FIELDS <= set(row) for row in rows)
        or any(row["utterance"] in old_utterances for row in rows)
    ):
        raise ValueError("blind natural-language manifest contract failed")
    production = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "scripts").glob("*.py")
    )
    if any(row["utterance"].casefold() in production.casefold() for row in rows):
        raise ValueError("blind utterance was hardcoded in production")
    return rows, digest


def _normalized(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").replace("-", " ").split())


def _actual_outcome(result: agent.TurnResult) -> str:
    if result.action_plan is not None:
        return "plan"
    if result.frame.kind == "clarification":
        return "clarification"
    if result.frame.kind == "action":
        return "deny"
    return "no_plan"


def _is_cross_room(utterance: str) -> bool:
    normalized = _normalized(utterance)
    return sum(marker in normalized for marker in AREA_MARKERS) >= 2


def run(manifest_path: Path) -> dict[str, Any]:
    manifest, manifest_sha256 = load_blind_manifest(manifest_path)
    inventory = stage72_fixtures.graph()
    original_request = http.client.HTTPConnection.request
    network_events: list[tuple[str, str, str]] = []
    ha_post = 0
    ha_service_paths = 0
    ha_reads = 0
    model_calls = 0
    model_prompt_leaks = 0

    def guarded_request(connection: http.client.HTTPConnection, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        nonlocal ha_post, ha_service_paths
        host = str(getattr(connection, "host", ""))
        method_upper = method.upper()
        network_events.append((host, method_upper, url))
        if host == agent.home_assistant_read.EXPECTED_HOST:
            if method_upper == "POST":
                ha_post += 1
                raise AssertionError("HA POST physically blocked")
            if url.startswith("/api/services"):
                ha_service_paths += 1
                raise AssertionError("HA service path physically blocked")
        return original_request(connection, method, url, *args, **kwargs)

    def blocked_snapshot(_command: str) -> tuple[dict[str, Any], int]:
        nonlocal ha_reads
        ha_reads += 1
        raise AssertionError("shadow planning attempted an HA read")

    def real_model(endpoint: object, path: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        nonlocal model_calls, model_prompt_leaks
        model_calls += 1
        boundary = payload.get("prompt") if path == "/api/generate" else payload.get("messages")
        model_prompt_leaks += bool(TECHNICAL.search(json.dumps(boundary, ensure_ascii=False)))
        return agent.call_ollama(endpoint, path, payload, **kwargs)

    sessions: dict[str, dict[str, Any]] = {}
    wrong_target = 0
    cross_room_target = 0
    ambiguous_plan = 0
    forbidden_plan = 0
    missed_expected_plan = 0
    false_action_intent = 0
    wrong_action = 0
    deterministic_resolutions = 0
    model_assisted_resolutions = 0
    service_calls = 0
    durations: list[float] = []
    cases: list[dict[str, Any]] = []

    http.client.HTTPConnection.request = guarded_request
    try:
        for row in manifest:
            session_key = row.get("session")
            context = sessions.setdefault(str(session_key), owner_chat.startup_context()) if session_key else owner_chat.startup_context()
            captured: list[agent.TurnResult] = []

            def real_bounded_path(question: str, current: dict[str, Any], history: list[dict[str, str]], **kwargs: Any) -> str:
                result = agent.process_turn(
                    question, current, history,
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
                    str(row["utterance"]), context, [], natural_agent=real_bounded_path,
                )
                if len(captured) != 1 or answer != captured[0].answer:
                    raise AssertionError("owner_chat bypassed the bounded result")
                result = captured[0]
            except Exception as caught:
                result = None
                error = f"{type(caught).__name__}: {caught}"
            duration = time.perf_counter() - started
            durations.append(duration)

            expected = str(row["expected_outcome"])
            actual = "error" if result is None else _actual_outcome(result)
            plan = None if result is None else result.action_plan
            target_ok = True
            if expected == "plan":
                identity_ok = bool(
                    plan is not None
                    and _normalized(plan.target_label) == _normalized(str(row["expected_human_target"]))
                    and plan.domain == row["expected_domain"]
                    and (
                        row["expected_area"] is None
                        or any(_normalized(str(row["expected_area"])) == _normalized(area) for area in plan.areas)
                    )
                )
                missed_expected_plan += plan is None
                wrong_target += plan is not None and not identity_ok
                wrong_action += plan is not None and plan.action != row["expected_action"]
                target_ok = bool(
                    identity_ok and plan is not None
                    and plan.action == row["expected_action"]
                    and action_policy.verify_action_plan(plan)
                )
            elif expected == "clarification":
                target_ok = plan is None and actual == "clarification"
            elif expected == "deny":
                target_ok = plan is None and actual == "deny"
            else:
                target_ok = plan is None

            ambiguous_plan += expected == "clarification" and plan is not None
            forbidden_plan += row["category"] == "forbidden" and plan is not None
            false_action_intent += row["expected_action_intent"] is False and plan is not None
            cross_room_target += _is_cross_room(str(row["utterance"])) and plan is not None
            if plan is not None:
                service_calls += plan.service_calls
                deterministic_resolutions += not result.frame.selector_used
                model_assisted_resolutions += result.frame.selector_used

            case_pass = error is None and target_ok
            cases.append({
                "case_id": row["case_id"], "category": row["category"],
                "expected_outcome": expected, "actual_outcome": actual,
                "selected_human_target": None if plan is None else plan.target_label,
                "selected_area": None if plan is None else list(plan.areas),
                "selected_domain": None if plan is None else plan.domain,
                "selected_action": None if plan is None else plan.action,
                "model_assisted": False if result is None else result.frame.selector_used,
                "latency_s": round(duration, 4), "pass": case_pass,
                "error": error,
            })
    finally:
        http.client.HTTPConnection.request = original_request

    passed = sum(case["pass"] for case in cases)
    latency = {
        "n": len(durations), "p50_s": round(percentile(durations, .50), 4),
        "p95_s": round(percentile(durations, .95), 4),
        "p99_s": round(percentile(durations, .99), 4),
        "mean_s": round(statistics.fmean(durations), 4),
    }
    status = "pass" if (
        passed == 100
        and wrong_target == cross_room_target == ambiguous_plan == forbidden_plan
        == missed_expected_plan == false_action_intent == wrong_action == ha_post
        == ha_service_paths == ha_reads == service_calls == model_prompt_leaks == 0
        and latency["p95_s"] <= 2.5
    ) else "fail"
    return {
        "schema_version": 1, "status": status, "mode": "shadow",
        "manifest_count": len(manifest), "manifest_sha256": manifest_sha256,
        "passed": passed, "failures": 100 - passed,
        "failed_case_ids": [case["case_id"] for case in cases if not case["pass"]],
        "wrong_target": wrong_target, "cross_room_target": cross_room_target,
        "ambiguous_plan": ambiguous_plan, "forbidden_plan": forbidden_plan,
        "missed_expected_plan": missed_expected_plan,
        "false_action_intent": false_action_intent, "wrong_action": wrong_action,
        "ha_post": ha_post, "ha_service_paths": ha_service_paths,
        "ha_reads": ha_reads, "service_calls": service_calls,
        "model_calls": model_calls,
        "deterministic_resolutions": deterministic_resolutions,
        "model_assisted_resolutions": model_assisted_resolutions,
        "model_prompt_technical_ids": model_prompt_leaks,
        "network_requests": len(network_events), "latency": latency,
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        report = run(arguments.manifest)
    except Exception as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False, separators=(",", ":")))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
