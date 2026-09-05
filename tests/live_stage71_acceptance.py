#!/usr/bin/env python3
"""Live Stage 71 read-only acceptance with an independent oracle."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import websocket


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get("HOME_BUTLER_TEST_RUNTIME_DIR", ROOT / "scripts"))
sys.path[:0] = [str(RUNTIME_DIR), str(ROOT / "tests")]

import bounded_ha_agent as agent  # noqa: E402
import home_assistant_inventory as inventory_builder  # noqa: E402
import home_assistant_mcp as resolver  # noqa: E402
import home_assistant_read as adapter  # noqa: E402
import stage71_oracle as oracle  # noqa: E402


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def independent_states(config: adapter.AdapterConfig) -> list[dict[str, Any]]:
    connection = http.client.HTTPConnection(config.host, config.port, timeout=5)
    try:
        connection.request("GET", "/api/states", headers={
            "Authorization": f"Bearer {config.token}", "Accept": "application/json",
            "Connection": "close",
        })
        response = connection.getresponse()
        raw = response.read(4 * 1_048_576 + 1)
        if response.status != 200 or len(raw) > 4 * 1_048_576:
            raise RuntimeError("oracle state read failed")
        value = json.loads(raw)
        if not isinstance(value, list):
            raise RuntimeError("oracle state read failed")
        return [item for item in value if isinstance(item, dict)]
    finally:
        connection.close()


def independent_entity_registry(config: adapter.AdapterConfig) -> list[dict[str, Any]]:
    socket = websocket.create_connection(
        f"ws://{config.host}:{config.port}/api/websocket", timeout=10,
        suppress_origin=True, http_proxy_host=None, http_proxy_port=None,
        http_no_proxy=[config.host],
    )
    try:
        if json.loads(socket.recv()).get("type") != "auth_required":
            raise RuntimeError("oracle registry auth failed")
        socket.send(json.dumps({"type": "auth", "access_token": config.token}))
        if json.loads(socket.recv()).get("type") != "auth_ok":
            raise RuntimeError("oracle registry auth failed")
        socket.send(json.dumps({"id": 71, "type": "config/entity_registry/list"}))
        for _attempt in range(64):
            response = json.loads(socket.recv())
            if response.get("id") == 71:
                result = response.get("result")
                if response.get("success") is not True or not isinstance(result, list):
                    raise RuntimeError("oracle registry read failed")
                return [item for item in result if isinstance(item, dict)]
        raise RuntimeError("oracle registry read failed")
    finally:
        socket.close()


def fresh_snapshot(config: adapter.AdapterConfig) -> dict[str, Any]:
    status, entities = adapter._states(config)
    return {
        "schema_version": 1, "observed_at": adapter._now_iso(), "status": status,
        "entities": entities, "service_calls": 0,
    }


def load_blind_corpus() -> tuple[list[dict[str, Any]], str]:
    path = ROOT / "tests" / "data" / "stage71_blind_owner.jsonl"
    raw = path.read_bytes()
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    if any(not isinstance(row, dict) or "utterance" not in row for row in rows):
        raise RuntimeError("blind corpus is invalid")
    return rows, hashlib.sha256(raw).hexdigest()


def target_patterns(document: Mapping[str, Any], pattern: str) -> set[str]:
    return {
        str(item["target_ref"])
        for item in document.get("physical_nodes", [])
        if isinstance(item, Mapping) and re.search(pattern, str(item.get("display_name") or ""), re.I)
    }


def run(token_file: Path) -> dict[str, Any]:
    token = token_file.read_text(encoding="ascii").strip()
    if adapter.TOKEN_RE.fullmatch(token) is None:
        raise RuntimeError("credential is invalid")
    config = adapter.AdapterConfig(
        adapter.EXPECTED_SCHEME, adapter.EXPECTED_HOST, adapter.EXPECTED_PORT,
        token, (), True,
    )
    document = inventory_builder.collect_inventory(config)
    raw_states = independent_states(config)
    registry = independent_entity_registry(config)
    graph_coverage = oracle.coverage(registry, raw_states, document)
    persistent_current = oracle.persistent_current_fields(document)
    corpus, corpus_digest = load_blind_corpus()

    wrong = invented = lost = model_ids = failures = skips = 0
    deterministic_latencies: list[float] = []
    read_physical: set[str] = set()
    read_logical: set[str] = set()

    def execute(utterance: str) -> tuple[agent.TurnResult, dict[str, Any], float]:
        observed: dict[str, Any] = {}
        def reader(_command: str) -> tuple[dict[str, Any], int]:
            snapshot = fresh_snapshot(config)
            observed["snapshot"] = snapshot
            return snapshot, 0
        started = time.perf_counter()
        result = agent.process_turn(
            utterance, {"session_focus": agent.SessionFocus()}, [],
            inventory_loader=lambda: document, snapshot_reader=reader,
        )
        return result, observed.get("snapshot", {}), time.perf_counter() - started

    for row in corpus:
        expected = target_patterns(document, str(row["target_pattern"]))
        if not expected:
            failures += 1
            continue
        result, snapshot, duration = execute(str(row["utterance"]))
        if not result.frame.selector_used:
            deterministic_latencies.append(duration)
        if row.get("clarification"):
            actual = set(result.frame.clarification_target_refs)
            wrong += len(actual - expected) + len(expected - actual)
            lost += int(result.frame.kind != "clarification")
            continue
        report = oracle.evaluate_turn(
            result, document, snapshot, sorted(expected), [str(row["feature"])],
        )
        wrong += report.wrong_target
        invented += report.invented_facts
        lost += report.lost_requested_values
        model_ids += report.model_generated_entity_ids
        for receipt in result.receipts:
            (read_physical if receipt.target_kind == "physical" else read_logical).add(receipt.target_ref)

    # Exercise raw exact-name reads beyond the frozen corpus, still through process_turn.
    all_nodes = list(document["physical_nodes"]) + list(document["logical_nodes"])
    entity_by_ref = {item["entity_ref"]: item for item in document["entities"]}
    name_counts: dict[str, int] = {}
    for node in all_nodes:
        key = resolver.normalize_text(str(node.get("display_name") or ""))
        name_counts[key] = name_counts.get(key, 0) + 1
    for node in all_nodes:
        target_ref = str(node["target_ref"])
        wanted = read_physical if node["kind"] == "physical" else read_logical
        minimum = 30 if node["kind"] == "physical" else 10
        if len(wanted) >= minimum:
            continue
        label = str(node.get("display_name") or "")
        member_names = [
            str(entity_by_ref[ref].get("display_name") or "")
            for ref in node.get("entity_refs", []) if ref in entity_by_ref
            and not entity_by_ref[ref].get("disabled") and not entity_by_ref[ref].get("hidden")
        ]
        proposed = ([label] if label and name_counts.get(resolver.normalize_text(label)) == 1 else []) + member_names
        query_label = next((
            name for name in proposed if name and resolver.resolve_targets(
                document, f"Покажи статус {name}", "status",
            ).target_refs == (target_ref,)
        ), "")
        if not query_label:
            skips += 1
            continue
        result, snapshot, duration = execute(f"Покажи статус {query_label}")
        if not result.frame.selector_used:
            deterministic_latencies.append(duration)
        report = oracle.evaluate_turn(result, document, snapshot, [target_ref], ["status"])
        wrong += report.wrong_target
        invented += report.invented_facts
        lost += report.lost_requested_values
        model_ids += report.model_generated_entity_ids
        for receipt in result.receipts:
            (read_physical if receipt.target_kind == "physical" else read_logical).add(receipt.target_ref)

    # Stage 72 resolves read targets on the host. The retired standalone model
    # selector is not a production path; all reads above use process_turn.
    def latency(values: list[float]) -> dict[str, float | int]:
        return {
            "n": len(values), "p50_s": round(percentile(values, .50), 4),
            "p95_s": round(percentile(values, .95), 4),
            "p99_s": round(percentile(values, .99), 4),
        }

    deterministic = latency(deterministic_latencies)
    passed = (
        wrong == invented == lost == model_ids == failures == 0
        and not persistent_current
        and graph_coverage["missing_enabled_current"] == 0
        and graph_coverage["enabled_current"] == graph_coverage["represented_enabled_current"]
        and len(read_physical) >= 30 and len(read_logical) >= 10
        and deterministic["p95_s"] <= 1.5
    )
    return {
        "schema_version": 1, "status": "pass" if passed else "fail", "read_only": True,
        "ha_service_calls": 0, "wrong_target": wrong, "invented_facts": invented,
        "lost_requested_values": lost, "persistent_inventory_current_values": len(persistent_current),
        "model_generated_entity_ids": model_ids, "failures": failures, "skips": skips,
        "blind_corpus_count": len(corpus), "blind_corpus_sha256": corpus_digest,
        "inventory_schema": document["schema_version"], "inventory_entities": document["entity_count"],
        "physical_nodes": document["physical_device_count"], "logical_nodes": document["logical_entity_count"],
        "area_nodes": document["area_count"], "integration_nodes": document["integration_count"],
        "enabled_current_entities": graph_coverage["enabled_current"],
        "represented_enabled_current_entities": graph_coverage["represented_enabled_current"],
        "physical_devices_read": len(read_physical), "logical_entities_read": len(read_logical),
        "deterministic_latency": deterministic,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        report = run(arguments.token_file)
    except Exception:
        print('{"status":"error","read_only":true,"ha_service_calls":0}', file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
