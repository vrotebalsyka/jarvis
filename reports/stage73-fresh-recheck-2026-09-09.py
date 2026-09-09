"""One-off read/shadow recheck; never install or execute the live runner."""
import hashlib
import http.client
import json
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
import canary_contract as contract
import home_assistant_read as reader
import home_assistant_inventory as graph
import live_stage71_acceptance as read_suite
import live_stage72_natural_language_acceptance as natural
import live_stage72_real_home_room_type_acceptance as room_type
import live_stage73_shadow_capability as shadow
import run_stage73_regressions as regressions
import websocket

STAMP = "2026-09-09-restored"
TOKEN = Path("/root/Jarvis/home-butler/secrets/home-assistant.token")
network = Counter(HA_POST=0, SERVICE_CALLS=0, HA_GET=0, REGISTRY_READS=0, BLOCKED=0)
original_http, original_ws = http.client.HTTPConnection.request, websocket.WebSocket.send


def http_guard(connection, method, path, *args, **kwargs):
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
        raise RuntimeError("read_only_boundary")
    return original_http(connection, method, path, *args, **kwargs)


def ws_guard(connection, payload, *args, **kwargs):
    kind = json.loads(payload).get("type")
    network["SERVICE_CALLS"] += int(kind == "call_service")
    if kind not in {"auth", "config/entity_registry/list", "config/device_registry/list", "config/area_registry/list"}:
        network["BLOCKED"] += 1
        raise RuntimeError("read_only_registry_boundary")
    network["REGISTRY_READS"] += int(kind != "auth")
    return original_ws(connection, payload, *args, **kwargs)


def emit(name, result):
    print(json.dumps({"suite": name, **{k: v for k, v in result.items() if k != "cases"}}, ensure_ascii=False), flush=True)


def run():
    control = contract.load_config()
    if control.control_enabled or control.canary_live_enabled:
        raise RuntimeError("Phase_A_requires_OFF")
    digest = regressions.source_digest()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    result = {"started_at_utc": datetime.now(timezone.utc).isoformat(), "commit": commit,
              "source_digest": digest, "control_enabled": control.control_enabled,
              "canary_live_enabled": control.canary_live_enabled,
              "owner_allowlist_records": len(control.records), "live_approval": False, "live_cycles": 0,
              "suites": {}, "phase_a_complete": False}
    config = reader.AdapterConfig(reader.EXPECTED_SCHEME, reader.EXPECTED_HOST, reader.EXPECTED_PORT,
                                  TOKEN.read_text(encoding="ascii").strip(), (), True)
    http.client.HTTPConnection.request, websocket.WebSocket.send = http_guard, ws_guard
    try:
        started = time.perf_counter()
        reader.request_json(config, "/api/")
        document = graph.collect_inventory(config)
        result["fresh_metadata"] = {"status": "pass", "elapsed_s": round(time.perf_counter()-started, 4),
            **{key: document[key] for key in ("schema_version", "entity_count", "physical_device_count",
                                             "logical_entity_count", "area_count", "integration_count")}}
        emit("fresh_metadata", result["fresh_metadata"])
        with tempfile.TemporaryDirectory(prefix="stage73-readonly-") as temp_dir:
            metadata_path = Path(temp_dir) / "metadata.json"
            graph._atomic_write(metadata_path, json.dumps(document).encode())
            checks = (
                ("stage71_live_oracle", lambda: read_suite.run(TOKEN)),
                ("stage72_natural", lambda: natural.run(ROOT / "tests/data/stage72_blind_natural_language_100.jsonl")),
                ("stage72_room_type", lambda: room_type.run(ROOT / "tests/data/stage72_real_home_room_type_owner_reviewed.jsonl", metadata_path)),
                ("stage73_shadow", lambda: shadow.run(ROOT / "tests/data/stage73_selected_canary_shadow_205.jsonl", TOKEN)),
                ("stage73_owner_review", lambda: shadow.run(ROOT / "tests/data/stage73_owner_review_25.jsonl", TOKEN,
                    frozen_contract=("b4fce23a0462ba0c23626ac63cd506c368ede500003a8ad5807a9a0007e01f06", 25))),
            )
            for name, check in checks:
                before = dict(network)
                print(json.dumps({"suite": name, "event": "starting"}), flush=True)
                try:
                    report = check()
                except Exception as error:
                    report = {"status": "error", "error_type": type(error).__name__}
                report["outer_instrumented_network"] = {k: v-before.get(k, 0) for k, v in network.items()}
                path = ROOT / "reports" / f"stage73-{name}-{STAMP}.json"
                with path.open("x", encoding="utf-8") as handle:
                    json.dump(report, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                result["suites"][name] = {k: v for k, v in report.items() if k != "cases"}
                emit(name, report)
                if any(network[k] for k in ("HA_POST", "SERVICE_CALLS", "BLOCKED")):
                    result["stopped_for_boundary_violation"] = True
                    break
    except Exception as error:
        result["preflight_error_type"] = type(error).__name__
    finally:
        http.client.HTTPConnection.request, websocket.WebSocket.send = original_http, original_ws
    result["network"] = dict(network)
    result["source_unchanged"] = regressions.source_digest() == digest
    result["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    result["status"] = "pass" if len(result["suites"]) == 5 and all(
        r["status"] == "pass" for r in result["suites"].values()) and result["source_unchanged"] and not any(
        network[k] for k in ("HA_POST", "SERVICE_CALLS", "BLOCKED")) else "fail"
    with (ROOT / "reports" / f"stage73-fresh-recheck-{STAMP}.json").open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    emit("overall", result)
    return 0 if result["status"] == "pass" else 1


def diagnose_n04():
    """One diagnostic repeat, not a replacement for the original failed corpus."""
    import owner_chat
    import bounded_ha_agent as agent
    import stage72_fixtures
    control = contract.load_config()
    if control.control_enabled or control.canary_live_enabled:
        raise RuntimeError("Phase_A_requires_OFF")
    rows, digest = natural.load_blind_manifest(ROOT / "tests/data/stage72_blind_natural_language_100.jsonl")
    row = next(r for r in rows if r["case_id"] == "N04")
    document = stage72_fixtures.graph()
    calls, turns = [], []

    def model(*args, **kwargs):
        started = time.perf_counter()
        measured = {"timeout_s": kwargs.get("timeout")}
        try:
            response = agent.call_ollama(*args, **kwargs)
            measured.update({key: response.get(key) for key in (
                "total_duration", "load_duration", "prompt_eval_duration", "eval_duration",
                "prompt_eval_count", "eval_count", "done_reason")})
            return response
        except Exception as error:
            measured["error_type"] = type(error).__name__
            raise
        finally:
            measured["wall_s"] = round(time.perf_counter()-started, 4)
            calls.append(measured)

    def blocked_read(_command):
        raise RuntimeError("diagnostic_has_no_HA_reads")

    def production(question, context, history, **kwargs):
        turn = agent.process_turn(question, context, history, inventory_loader=lambda: document,
            snapshot_reader=blocked_read, endpoint_loader=agent.load_runtime_ollama_endpoint,
            ollama_call=model, trace_sink=None)
        turns.append(turn)
        return turn.answer

    result = {"scope": "single_case_diagnostic_fixture_real_model", "replaces_corpus": False,
              "started_at_utc": datetime.now(timezone.utc).isoformat(), "case_id": row["case_id"],
              "utterance": row["utterance"], "manifest_sha256": digest}
    http.client.HTTPConnection.request, websocket.WebSocket.send = http_guard, ws_guard
    started = time.perf_counter()
    try:
        answer = owner_chat.answer_natural(row["utterance"], owner_chat.startup_context(), [], natural_agent=production)
        plan = turns[0].action_plan if len(turns) == 1 else None
        result["actual_outcome"] = "plan" if plan else "no_plan"
        result["selected_human_target"] = plan.target_label if plan else None
        result["selected_areas"] = list(plan.areas) if plan else []
        result["selected_action"] = plan.action if plan else None
        result["matches_frozen_expectation"] = bool(plan and plan.target_label == row["expected_human_target"]
            and plan.domain == row["expected_domain"] and plan.action == row["expected_action"]
            and row["expected_area"] in plan.areas)
    except Exception as error:
        result.update(actual_outcome="error", error_type=type(error).__name__, matches_frozen_expectation=False)
    finally:
        http.client.HTTPConnection.request, websocket.WebSocket.send = original_http, original_ws
    result.update(wall_s=round(time.perf_counter()-started, 4), model_calls=calls, network=dict(network))
    with (ROOT / "reports" / f"stage73-n04-diagnostic-{STAMP}.json").open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    emit("N04_diagnostic", result)
    return 0 if result["matches_frozen_expectation"] else 1


if __name__ == "__main__":
    raise SystemExit(diagnose_n04() if sys.argv[1:] == ["--diagnose-n04"] else run())
