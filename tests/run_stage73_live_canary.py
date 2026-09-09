#!/usr/bin/env python3
"""Prepared Phase B runner. Default is STOP; never enables owner flags.

Only an unexpired root-owned approval bound to clean Git/runtime/manifest/config
can unlock the CLI. Phase A tests use a loopback fake endpoint, not this CLI.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import stat
import subprocess
import sys
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
import bounded_ha_agent as agent
import canary_contract as contract
import canary_write_adapter as write
import home_assistant_inventory as graph
import home_assistant_read as read
import owner_chat
import stage73_oracle as oracle
import websocket


class LiveStop(ValueError):
    pass


def percentiles(values):
    ordered = sorted(values)
    def at(q):
        if not ordered:
            return None
        point = (len(ordered) - 1) * q
        lo, hi = int(point), min(int(point) + 1, len(ordered) - 1)
        return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (point - lo), 4)
    return dict(n=len(values), P50=at(.5), P95=at(.95), P99=at(.99))


class Boundary:
    """Independently intercept the exact owner-reviewed target/action before POST."""
    def __init__(self, host, port, stop):
        self.host, self.port, self.stop = host, port, stop
        self.metrics = Counter(HA_POST=0, SERVICE_CALLS=0, HA_GET=0, REGISTRY_READS=0,
                              WRONG_TARGET=0, UNAUTHORIZED_ACTION=0, ARBITRARY_SERVICE_CALL=0,
                              DUPLICATE_SIDE_EFFECT=0)
        self.permitted = None
        self.used = False

    @contextmanager
    def permit(self, record=None, action=None):
        self.permitted = (record.entity_id, record.domain, action) if record else None
        self.used = False
        try:
            yield
        finally:
            self.permitted = None

    def reject(self, metric):
        self.metrics[metric] += 1
        # Do not acquire the adapter's already held process lock here. The
        # caller catches this exception after execute has released its lock.
        raise LiveStop("independent_network_boundary_stop")

    @contextmanager
    def installed(self):
        original_http, original_ws = http.client.HTTPConnection.request, websocket.WebSocket.send
        def guarded_http(connection, method, path, *args, **kwargs):
            if connection.host == self.host and connection.port == self.port:
                if method == "GET" and path in {"/api/", "/api/states"}:
                    self.metrics["HA_GET"] += 1
                elif method == "POST":
                    self.metrics["HA_POST"] += 1
                    if self.permitted is None:
                        self.reject("UNAUTHORIZED_ACTION")
                    private_id, domain, action = self.permitted
                    if domain not in {"light", "switch"} or action not in {"turn_on", "turn_off"}:
                        self.reject("UNAUTHORIZED_ACTION")
                    if path != f"/api/services/{domain}/{action}":
                        self.reject("ARBITRARY_SERVICE_CALL")
                    body = kwargs.get("body", args[0] if args else None)
                    if json.loads(body) != {"entity_id": private_id}:
                        self.reject("WRONG_TARGET")
                    if self.used:
                        self.reject("DUPLICATE_SIDE_EFFECT")
                    self.used = True
                    self.metrics["SERVICE_CALLS"] += 1
                else:
                    self.reject("ARBITRARY_SERVICE_CALL")
            elif not (connection.host in {"127.0.0.1", "localhost", "::1"} and connection.port == 11434
                      and ((method == "POST" and path in {"/api/chat", "/api/generate"})
                           or (method == "GET" and path in {"/api/version", "/api/tags", "/api/ps"}))):
                self.reject("ARBITRARY_SERVICE_CALL")
            return original_http(connection, method, path, *args, **kwargs)
        def ws(connection, payload, *args, **kwargs):
            kind = json.loads(payload).get("type")
            if kind not in {"auth", "config/entity_registry/list", "config/device_registry/list", "config/area_registry/list"}:
                self.reject("ARBITRARY_SERVICE_CALL")
            self.metrics["REGISTRY_READS"] += int(kind != "auth")
            return original_ws(connection, payload, *args, **kwargs)
        http.client.HTTPConnection.request, websocket.WebSocket.send = guarded_http, ws
        try:
            yield self
        finally:
            http.client.HTTPConnection.request, websocket.WebSocket.send = original_http, original_ws


def run_cycles(rows, config, adapter, boundary, inventory_loader, oracle_reader, binding_reader,
               *, cycles_per_target=20, authorize, fake=False, sleep=time.sleep):
    """One production conversation, separate owner expectations and direct oracle.

    No retries of unknown delivery. Every primary action and rollback has its
    own one-POST boundary. A stop terminates the remaining matrix immediately.
    """
    authorize()
    if not 1 <= len(rows) <= 30 or not all(row.get("owner_reviewed") is True for row in rows):
        raise LiveStop("owner_review_required")
    doc = inventory_loader()
    bindings = oracle.bind_shadow_expectations(doc, rows)  # before any answer
    pinned = {row["case_id"]: config.record(bindings[row["case_id"]]["entity_ref"])
              for row in rows if row["expected_outcome"] == "plan"}
    targets = {record.canary_id: record for record in pinned.values()}
    if len(targets) < 3 or cycles_per_target < (1 if fake else 20):
        raise LiveStop("insufficient_canary_matrix")
    counts = Counter({key: 0 for key in ("LIVE_CYCLES", "LIVE_TARGETS", "WRONG_TARGET", "CROSS_ROOM_TARGET",
        "FALSE_SUCCESS", "DUPLICATE_SIDE_EFFECT", "UNEXPECTED_CANARY_CHANGE", "ROLLBACK_FAILURE",
        "DELIVERY_UNKNOWN", "DELIVERY_UNKNOWN_AUTO_RETRY", "EXPIRED_PLAN_EXECUTED",
        "FINGERPRINT_MISMATCH_EXECUTED", "UNAUTHORIZED_ACTION", "ARBITRARY_SERVICE_CALL",
        "VERIFIED_RECEIPTS", "ACCEPTED_UNVERIFIED_RECEIPTS", "FAILED_RECEIPTS", "AMBIGUOUS_PLAN")})
    timings = {key: [] for key in ("intent_resolve", "planning", "POST", "verification", "total_receipt")}
    cases, completed, error = [], Counter(), None

    def turn(row, context):
        captured = []
        def core(question, ctx, history, **kwargs):
            start = time.monotonic()
            result = agent.process_turn(question, ctx, history, inventory_loader=inventory_loader,
                canary_adapter=adapter, control_config_loader=lambda: config, trace_sink=None, **kwargs)
            captured.append((result, time.monotonic() - start))
            return result.answer
        answer = owner_chat.answer_natural(row["utterance"], context, [], natural_agent=core)
        if not captured:
            raise LiveStop("production_turn_missing")
        return *captured[0], answer

    with boundary.installed():
        try:
            # Reviewed negative rows are executed with the physical write boundary closed.
            for row in (r for r in rows if r["expected_outcome"] != "plan"):
                authorize()
                with boundary.permit():
                    result, elapsed, answer = turn(row, owner_chat.startup_context())
                if result.action_plan is not None:
                    counts["AMBIGUOUS_PLAN"] += 1
                    raise LiveStop("negative_command_planned")
                cases.append({"utterance": row["utterance"], "outcome": result.frame.kind, "latency": elapsed})
            for record in targets.values():
                for cycle in range(cycles_per_target):
                    authorize()
                    before, identity_before = oracle_reader(), binding_reader()
                    initial = dict(before.states).get(record.canary_id)
                    if initial not in {"on", "off"}:
                        raise LiveStop("initial_state_unavailable")
                    desired_action = "turn_off" if initial == "on" else "turn_on"
                    choices = [r for r in rows if r["case_id"] in pinned and pinned[r["case_id"]] == record
                               and r["expected_action"] == desired_action]
                    if not choices:
                        raise LiveStop("missing_reviewed_on_off_phrase")
                    row = choices[cycle % len(choices)]
                    evidence = {"utterance": row["utterance"], "expected_human_target": row["expected_human_target"],
                                "room": row["expected_area"], "action": row["expected_action"],
                                "before": dict(before.states), "rollback_result": "not_attempted"}
                    cases.append(evidence)
                    context = owner_chat.startup_context()
                    context["control_request_key"] = f"live-{time.time_ns()}-{cycle}"
                    started, prior = time.monotonic(), boundary.metrics["SERVICE_CALLS"]
                    with boundary.permit(record, desired_action):
                        result, duration, answer = turn(row, context)
                    receipt, plan = result.action_receipt, result.canary_plan
                    if receipt is None or plan is None:
                        raise LiveStop("expected_canary_plan_missing")
                    counts[{"verified": "VERIFIED_RECEIPTS", "accepted_unverified": "ACCEPTED_UNVERIFIED_RECEIPTS",
                            "delivery_unknown": "DELIVERY_UNKNOWN"}.get(receipt.status, "FAILED_RECEIPTS")] += 1
                    # A delayed continuation is verification-only and cannot pass the closed POST boundary.
                    while receipt.status in {"accepted_unverified", "delivery_unknown"} and time.monotonic() - started < 30:
                        sleep(.3)
                        events = context["action_results"].poll()
                        if events:
                            evidence["result_events"] = events
                            receipt = adapter.verify_pending(plan)  # durable terminal receipt; no POST
                            if receipt.status == "verified":
                                counts["VERIFIED_RECEIPTS"] += 1
                            break
                    total = time.monotonic() - started
                    after = oracle_reader()
                    sleep(.2)
                    stable, identity_after = oracle_reader(), binding_reader()
                    used = boundary.metrics["SERVICE_CALLS"] - prior
                    score = oracle.evaluate(expected_target=record.target_ref, resolved_target=plan.resolved_target_ref,
                        expected_area=record.registry_area, resolved_area=plan.resolved_area,
                        action=row["expected_action"], before=oracle.OracleRead(before.started_at, before.completed_at,
                            tuple((config.record(next(c.target_ref for c in config.records if c.canary_id == k)).target_ref, v)
                                  for k, v in before.states)),
                        after=oracle.OracleRead(after.started_at, after.completed_at,
                            tuple((next(c.target_ref for c in config.records if c.canary_id == k), v) for k, v in after.states)),
                        stable=oracle.OracleRead(stable.started_at, stable.completed_at,
                            tuple((next(c.target_ref for c in config.records if c.canary_id == k), v) for k, v in stable.states)),
                        received_posts=used, receipt_status=receipt.status)
                    counts.update(score)
                    evidence.update(resolved_human_target=result.action_plan.target_label,
                        after=dict(after.states), stable_after=dict(stable.states), service_call_count=used,
                        receipt_status=receipt.status, safety=score)
                    if identity_before != identity_after:
                        counts["FINGERPRINT_MISMATCH_EXECUTED"] += 1
                    if (any(score.values()) or identity_before != identity_after or used != 1
                            or receipt.status != "verified" or plan.action != row["expected_action"]):
                        raise LiveStop("oracle_or_receipt_failed")
                    # Exact transport duplicate goes through the same production path, no POST permitted.
                    with boundary.permit():
                        duplicate, _, _ = turn(row, context)
                    if duplicate.action_receipt is None or not duplicate.action_receipt.replayed:
                        raise LiveStop("duplicate_receipt_missing")
                    context["control_request_key"] += "-already-desired"
                    with boundary.permit():
                        no_op, _, _ = turn(row, context)
                    if (no_op.action_receipt is None or no_op.action_receipt.status != "verified"
                            or not no_op.action_receipt.no_op or no_op.action_receipt.service_calls):
                        raise LiveStop("noop_verification_failed")
                    authorize()
                    rollback_plan = contract.seal_rollback(plan, initial, config, inventory_loader())
                    with boundary.permit(record, rollback_plan.action):
                        rollback = adapter.execute(rollback_plan)
                    restored = oracle_reader()
                    sleep(.2)
                    restored_stable = oracle_reader()
                    rollback_ok = (rollback.status == "verified" and dict(restored.states) == dict(before.states)
                        and dict(restored_stable.states) == dict(before.states) and binding_reader() == identity_before)
                    if not rollback_ok:
                        counts["ROLLBACK_FAILURE"] += 1
                        raise LiveStop("rollback_failed")
                    completed[record.canary_id] += 1
                    timings["intent_resolve"].append(max(0, duration - result.action_receipt.total_s))
                    # Includes IntentFrame, resolver and sealed planning, not a fabricated isolated phase.
                    timings["planning"].append(max(0, duration - result.action_receipt.total_s))
                    timings["POST"].append(result.action_receipt.post_s)
                    timings["verification"].append(receipt.verification_s)
                    timings["total_receipt"].append(total)
                    evidence.update(rollback_result="verified", latency=total)
        except Exception as exc:
            adapter.emergency_stop()
            error = str(exc) if type(exc) is LiveStop else type(exc).__name__
    counts["LIVE_CYCLES"] = 0 if fake else sum(completed.values())
    counts["LIVE_TARGETS"] = 0 if fake else len(completed)
    return {"status": "FAIL" if error else "PASS", "scope": "fake_runner_only" if fake else "live_canary",
            "error_type": error, "counts": dict(counts), "network": dict(boundary.metrics),
            "fake_cycles": sum(completed.values()) if fake else 0,
            "timings": {key: percentiles(value) for key, value in timings.items()},
            "timing_note": "intent_resolve/planning share the measured conversational planning interval",
            "cases": cases}


def approval(path, manifest, config):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
            raise LiveStop("unsafe_approval")
        value = read.strict_json_loads(os.read(descriptor, 8193))
    finally:
        os.close(descriptor)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip():
        raise LiveStop("dirty_source")
    if (value.get("phase_b_approved") is not True or value.get("initial_flags_off") is not True
            or value.get("rollback_reviewed") is not True or value.get("git_sha") != sha
            or value.get("manifest_sha256") != hashlib.sha256(manifest.read_bytes()).hexdigest()
            or value.get("allowlist_fingerprint") != config.fingerprint
            or not time.time() < value.get("expires_at", 0) <= time.time() + 3600
            or not config.enabled):
        raise LiveStop("explicit_live_approval_required")
    for source in (ROOT / "scripts").glob("*.py"):
        if source.read_bytes() != (Path("/opt/home-butler/scripts") / source.name).read_bytes():
            raise LiveStop("runtime_sha_mismatch")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--approval-file", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.execute_live:
        print(json.dumps({"status": "INCOMPLETE_LIVE_APPROVAL_REQUIRED", "executed": False}))
        return 2
    try:
        cfg = contract.load_config()
        authorize = lambda: approval(args.approval_file, args.manifest, cfg)
        authorize()
        reader = read.load_config()
        from canary_verifier import CanaryVerifier
        adapter = write.CanaryWriteAdapter(CanaryVerifier(reader))
        private = {r.canary_id: r.entity_id for r in cfg.records}
        rows = [json.loads(line) for line in args.manifest.read_text().splitlines() if line]
        result = run_cycles(rows, cfg, adapter, Boundary(reader.host, reader.port, adapter.emergency_stop),
            lambda: graph.collect_inventory(reader),
            lambda: oracle.read_states(reader.host, reader.port, reader.token, private),
            lambda: oracle.read_bindings(reader.host, reader.port, reader.token, private), authorize=authorize)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({key: value for key, value in result.items() if key != "cases"}))
        return 0 if result["status"] == "PASS" else 1
    except Exception:
        print(json.dumps({"status": "NOT_EXECUTED_OR_STOPPED", "reason": "preflight_or_runner_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
