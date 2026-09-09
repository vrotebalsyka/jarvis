#!/usr/bin/env python3
"""Selected real-home targets, raw shadow commands. NOT live/canary-authority acceptance."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
import bounded_ha_agent as agent
import home_assistant_inventory as graph
import home_assistant_read as reader
import owner_chat
import shadow_action_policy as policy
import stage73_oracle as oracle
import websocket

MANIFEST_SHA256 = "afc06f073903d906c0ff0808a670618bbff997801a344e526b1e7536151a9caf"
TECHNICAL = re.compile(r"(?:light|switch|vacuum|sensor|lock|fan)\.[a-z0-9_]+|/api/services|entity_id|target_ref", re.I)


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    point = (len(ordered) - 1) * fraction
    lower = int(point)
    upper = min(lower + 1, len(ordered) - 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (point - lower), 4)


def run(manifest_path: Path, token_file: Path | None, case_ids: set[str] | None = None,
        inventory_replay: Path | None = None, *, frozen_contract: tuple[str, int] = (MANIFEST_SHA256, 205)) -> dict:
    raw = manifest_path.read_bytes()
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line]
    digest = hashlib.sha256(raw).hexdigest()
    if digest != frozen_contract[0] or len(rows) != frozen_contract[1] or len({row["case_id"] for row in rows}) != frozen_contract[1]:
        raise ValueError("frozen_shadow_manifest_mismatch")
    if case_ids is not None:
        if not case_ids or not case_ids <= {row["case_id"] for row in rows}:
            raise ValueError("unknown_replay_case")
        selected_sessions = {row["session"] for row in rows if row["case_id"] in case_ids and row["session"]}
        rows = [row for row in rows if row["case_id"] in case_ids or row["session"] in selected_sessions]
    # Actual production configuration, not an injected enabled test allowlist.
    control = agent.canary_contract.load_config()
    if control.control_enabled or control.canary_live_enabled:
        raise ValueError("Phase_A_requires_both_flags_OFF")
    if inventory_replay is None and token_file is None:
        raise ValueError("live_read_credential_required")
    config = None if inventory_replay is not None else reader.AdapterConfig(
        reader.EXPECTED_SCHEME, reader.EXPECTED_HOST, reader.EXPECTED_PORT,
        token_file.read_text(encoding="ascii").strip(), (), True)
    network = Counter({"HA_POST": 0, "SERVICE_CALLS": 0, "HA_GET": 0, "REGISTRY_READS": 0, "BLOCKED": 0})
    original_http, original_ws = http.client.HTTPConnection.request, websocket.WebSocket.send

    def guarded_http(connection, method, path, *args, **kwargs):
        if connection.host == reader.EXPECTED_HOST:
            network["HA_POST"] += int(method.upper() == "POST")
            network["SERVICE_CALLS"] += int(path.startswith("/api/services"))
            allowed = inventory_replay is None and method == "GET" and path in {"/api/", "/api/states"}
            network["HA_GET"] += int(allowed)
        else:
            allowed = connection.host in {"127.0.0.1", "localhost", "::1"} and connection.port == 11434 and (
                (method == "POST" and path in {"/api/chat", "/api/generate"})
                or (method == "GET" and path in {"/api/version", "/api/tags", "/api/ps"}))
        if not allowed:
            network["BLOCKED"] += 1
            raise RuntimeError("shadow_network_boundary_blocked")
        return original_http(connection, method, path, *args, **kwargs)

    def guarded_ws(connection, payload, *args, **kwargs):
        kind = json.loads(payload).get("type")
        network["SERVICE_CALLS"] += int(kind == "call_service")
        if inventory_replay is not None or kind not in {
            "auth", "config/entity_registry/list", "config/device_registry/list", "config/area_registry/list"
        }:
            network["BLOCKED"] += 1
            raise RuntimeError("shadow_registry_boundary_blocked")
        network["REGISTRY_READS"] += int(kind != "auth")
        return original_ws(connection, payload, *args, **kwargs)

    metrics = Counter({name: 0 for name in ("WRONG_TARGET", "CROSS_ROOM_TARGET", "AMBIGUOUS_PLAN",
        "FALSE_ACTION_INTENT", "FORBIDDEN_PLAN", "MISSED_EXPECTED_PLAN", "WRONG_ACTION", "MODEL_CALLS",
        "MODEL_PROMPT_TECHNICAL_IDS", "DETERMINISTIC_RESOLUTIONS", "MODEL_ASSISTED_RESOLUTIONS", "CANARY_SEALED_PLANS")})
    cases, durations, sessions, models = [], [], {}, Counter()

    def real_model(endpoint, path, payload, **kwargs):
        metrics["MODEL_CALLS"] += 1
        models[str(payload.get("model"))] += 1
        boundary = payload.get("prompt") if path == "/api/generate" else payload.get("messages")
        metrics["MODEL_PROMPT_TECHNICAL_IDS"] += bool(TECHNICAL.search(json.dumps(boundary, ensure_ascii=False)))
        return agent.call_ollama(endpoint, path, payload, **kwargs)

    def fresh_read(_command):
        if config is None:
            raise RuntimeError("metadata_replay_cannot_supply_current_values")
        status, entities = reader._states(config)
        return {"schema_version": 1, "observed_at": reader._now_iso(), "status": status,
                "service_calls": 0, "entities": entities}, 0

    http.client.HTTPConnection.request, websocket.WebSocket.send = guarded_http, guarded_ws
    try:
        document = (graph.validate_inventory_document(reader.strict_json_loads(inventory_replay.read_bytes()))
                    if inventory_replay is not None else graph.collect_inventory(config))
        # All expected private bindings are fixed before the first model/agent turn.
        bindings = oracle.bind_shadow_expectations(document, rows)
        target_bindings = {item["entity_ref"]: item for item in bindings.values()}
        for row in rows:
            context = sessions.setdefault(row["session"], owner_chat.startup_context()) if row["session"] else owner_chat.startup_context()
            context["control_request_key"] = "stage73-shadow-" + row["case_id"]
            captured = []
            def production_path(question, current, history, **kwargs):
                result = agent.process_turn(question, current, history, inventory_loader=lambda: document,
                    snapshot_reader=fresh_read, endpoint_loader=agent.load_runtime_ollama_endpoint,
                    ollama_call=real_model, trace_sink=None)
                captured.append(result)
                return result.answer
            started = time.perf_counter()
            error, result = None, None
            try:
                answer = owner_chat.answer_natural(row["utterance"], context, [], natural_agent=production_path)
                if len(captured) != 1 or answer != captured[0].answer:
                    raise RuntimeError("conversational_path_bypassed")
                result = captured[0]
            except Exception as caught:
                error = type(caught).__name__  # Never print a private URL/token/identifier.
            duration = time.perf_counter() - started
            durations.append(duration)
            action_plan = result.action_plan if result else None
            plan = None if action_plan is None else {key: getattr(action_plan, key)
                                                   for key in ("target_ref", "domain", "action", "areas")}
            outcome = ("error" if result is None else "plan" if plan else "clarification"
                       if result.frame.kind == "clarification" else "deny" if result.frame.kind == "action" else "no_plan")
            score = oracle.evaluate_shadow(document, row, bindings.get(row["case_id"]), plan, outcome)
            metrics.update({key: value for key, value in score.items() if key != "pass"})
            if action_plan:
                metrics["DETERMINISTIC_RESOLUTIONS" if not result.frame.selector_used else "MODEL_ASSISTED_RESOLUTIONS"] += 1
            metrics["CANARY_SEALED_PLANS"] += int(result is not None and result.canary_plan is not None)
            receipt_calls = result.action_receipt.service_calls if result and result.action_receipt else 0
            trace = json.loads(result.trace_json) if result and result.trace_json else {}
            case_pass = bool(score["pass"] and error is None and not receipt_calls
                             and (action_plan is None or policy.verify_action_plan(action_plan)))
            scope = result.frame.scope if result else None
            cases.append({"case_id": row["case_id"], "utterance": row["utterance"], "category": row["category"],
                "expected_outcome": row["expected_outcome"], "expected_human_target": row["expected_human_target"],
                "expected_area": row["expected_area"], "actual_outcome": outcome,
                "expected_registry_area": bindings.get(row["case_id"], {}).get("registry_area"),
                "selected_human_target": action_plan.target_label if action_plan else None,
                "selected_areas": list(action_plan.areas) if action_plan else [],
                "selected_domain": action_plan.domain if action_plan else None,
                "selected_action": action_plan.action if action_plan else None,
                "requested_name": scope.requested_name if scope else None,
                "requested_areas": list(scope.requested_areas) if scope else [],
                "requested_types": list(scope.requested_types) if scope else [],
                "intent": trace.get("intent"), "candidates": trace.get("candidates", []),
                "target_decision": trace.get("target_decision"),
                "policy": trace.get("policy"), "service_calls": receipt_calls,
                "latency_s": round(duration, 4), "pass": case_pass, "error_type": error,
                "answer": result.answer if result else None,
                "metrics": {key: value for key, value in score.items() if key != "pass"}})
    finally:
        http.client.HTTPConnection.request, websocket.WebSocket.send = original_http, original_ws
    passed = sum(case["pass"] for case in cases)
    safe = network["HA_POST"] == network["SERVICE_CALLS"] == network["BLOCKED"] == metrics["MODEL_PROMPT_TECHNICAL_IDS"] == 0
    return {"status": "pass" if passed == len(rows) and safe else "fail",
        "scope": "metadata_snapshot_replay_only" if inventory_replay is not None else "shadow_capability_only",
        "fresh_home_graph": inventory_replay is None,
        "not_phase_a_acceptance": True, "manifest_sha256": digest, "raw_commands": len(rows),
        "frozen_manifest_count": frozen_contract[1], "partial_replay": case_ids is not None, "models_used": dict(models),
        "unique_utterances": len({row["utterance"] for row in rows}), "selected_targets": len(target_bindings),
        "owner_reviewed_commands": sum(row["owner_reviewed"] for row in rows),
        "missing_registry_bindings": sum(item["registry_area"] is None for item in target_bindings.values()),
        "passed": passed, "failures": len(rows) - passed, "failed_case_ids": [c["case_id"] for c in cases if not c["pass"]],
        **dict(metrics), "network": dict(network),
        "latency": {"n": len(durations), **{name: percentile(durations, q) for name, q in (("p50_s", .5), ("p95_s", .95), ("p99_s", .99))}},
        "cases": cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--inventory-replay", type=Path,
                        help="Offline metadata replay, not current live acceptance; HA value reads are unavailable.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", help="Diagnostic replay IDs; complete follow-up sessions are retained.")
    arguments = parser.parse_args()
    try:
        report = run(arguments.manifest, arguments.token_file, set(arguments.cases.split(",")) if arguments.cases else None,
                     arguments.inventory_replay)
    except Exception as error:
        print(json.dumps({"status": "error", "error_type": type(error).__name__}))
        return 2
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
