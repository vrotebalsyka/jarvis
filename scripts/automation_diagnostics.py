#!/usr/bin/env python3
"""Collect sanitized Home Assistant automation failures without replaying them."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_read as ha_read  # noqa: E402
import incident_monitor  # noqa: E402


MAX_AUTOMATIONS = 256
MAX_TRACES_PER_AUTOMATION = 12
MAX_TOTAL_TRACE_DETAILS = 512
MAX_COMMAND_MESSAGES = 32
TRACE_LOOKBACK_SECONDS = 2 * 86_400
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
ACTION_RE = re.compile(
    r"^(?:light|switch|button)\.(?:turn_on|turn_off|toggle|press)$"
)
ERROR_MARKERS = {
    "dns_resolution_failed": (
        "clientconnectordnserror",
        "name or service not known",
        "temporary failure in name resolution",
        "nodename nor servname provided",
        "gaierror",
    ),
    "tls_failure": (
        "sslcertverificationerror",
        "certificate verify failed",
        "clientconnectorsslerror",
        "tls handshake",
    ),
    "upstream_timeout": (
        "timeouterror",
        "server timeout",
        "connection timeout",
        "timed out",
    ),
    "network_unreachable": (
        "network is unreachable",
        "network unreachable",
        "oserror(101",
        "errno 101",
    ),
    "integration_not_loaded": (
        "integration not loaded",
        "config entry not loaded",
    ),
}


class DiagnosticsError(RuntimeError):
    """Secret-free automation diagnostics failure."""


def _command(socket: Any, identifier: int, command_type: str, **fields: str) -> Any:
    payload: dict[str, object] = {"id": identifier, "type": command_type}
    payload.update(fields)
    socket.send(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    for _attempt in range(MAX_COMMAND_MESSAGES):
        response = incident_monitor._message(socket.recv())
        if response.get("id") != identifier:
            continue
        if response.get("type") != "result" or response.get("success") is not True:
            raise DiagnosticsError("Home Assistant trace command failed")
        return response.get("result")
    raise DiagnosticsError("Home Assistant trace response is missing")


def _epoch(value: Any, fallback: int) -> int:
    if isinstance(value, dict):
        value = value.get("start")
    if not isinstance(value, str) or len(value) > 64:
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        return fallback
    result = int(parsed.astimezone(timezone.utc).timestamp())
    if result < 0 or result > fallback + 30:
        return fallback
    return result


def _catalogue(raw_states: Any) -> list[dict[str, str]]:
    if not isinstance(raw_states, list) or len(raw_states) > 8_192:
        raise DiagnosticsError("Home Assistant state catalogue is invalid")
    result: list[dict[str, str]] = []
    for item in raw_states:
        if not isinstance(item, dict):
            continue
        entity_id = item.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id.startswith("automation."):
            continue
        try:
            entity_id = ha_read._validate_entity_id(entity_id)
        except ha_read.AdapterError:
            continue
        attributes = item.get("attributes")
        if not isinstance(attributes, dict):
            continue
        item_id = attributes.get("id")
        if not isinstance(item_id, str) or ITEM_ID_RE.fullmatch(item_id) is None:
            continue
        friendly = ha_read.sanitize_friendly_name(attributes.get("friendly_name"))
        if friendly is None:
            friendly = " ".join(entity_id.split(".", 1)[1].replace("_", " ").split())
        result.append({"entity_id": entity_id, "item_id": item_id, "display_name": friendly})
        if len(result) > MAX_AUTOMATIONS:
            raise DiagnosticsError("too many Home Assistant automations")
    return sorted(result, key=lambda item: item["entity_id"])


def _walk(value: Any, *, depth: int = 0, budget: list[int] | None = None) -> Iterator[tuple[str | None, Any]]:
    remaining = budget if budget is not None else [4_096]
    if depth > 20 or remaining[0] <= 0:
        return
    remaining[0] -= 1
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            yield key, item
            yield from _walk(item, depth=depth + 1, budget=remaining)
    elif isinstance(value, list):
        for item in value[:1_024]:
            yield None, item
            yield from _walk(item, depth=depth + 1, budget=remaining)


def _text_fingerprint(trace: Any) -> str:
    fragments: list[str] = []
    total = 0
    for _key, value in _walk(trace):
        if not isinstance(value, str):
            continue
        fragment = value[:4_096].casefold()
        fragments.append(fragment)
        total += len(fragment)
        if total >= 65_536:
            break
    return "\n".join(fragments)


def classify_failure(trace: Any) -> tuple[str, str, str]:
    """Return fixed error/cause/confidence codes; never return trace text."""
    fingerprint = _text_fingerprint(trace)
    error_code = "automation_action_failed"
    for candidate, markers in ERROR_MARKERS.items():
        if any(marker in fingerprint for marker in markers):
            error_code = candidate
            break
    yandex = "iot.quasar.yandex.ru" in fingerprint
    if error_code == "network_unreachable" and yandex:
        return error_code, "yandex_cloud_unreachable", "confirmed"
    if error_code == "dns_resolution_failed":
        return error_code, "dns_resolution_failed", "confirmed"
    if error_code == "tls_failure":
        return error_code, "tls_failure", "confirmed"
    if error_code == "upstream_timeout":
        cause = "yandex_cloud_unreachable" if yandex else "upstream_timeout"
        return error_code, cause, "probable"
    if error_code == "integration_not_loaded":
        return error_code, "integration_not_loaded", "confirmed"
    return error_code, "automation_action_failed", "probable"


def _has_explicit_error(trace: Any) -> bool:
    for key, value in _walk(trace):
        if key == "error" and value is not None and value != "" and value is not False:
            return True
    return False


def _targets_and_action(trace: Any) -> tuple[list[str], str]:
    targets: set[str] = set()
    actions: set[str] = set()
    for key, value in _walk(trace):
        if key in {"action", "service"} and isinstance(value, str):
            if ACTION_RE.fullmatch(value):
                actions.add(value)
        if key != "entity_id":
            continue
        candidates = [value] if isinstance(value, str) else value if isinstance(value, list) else []
        for candidate in candidates[:32]:
            if not isinstance(candidate, str):
                continue
            try:
                targets.add(ha_read._validate_entity_id(candidate))
            except ha_read.AdapterError:
                continue
    controllable = sorted(
        target for target in targets
        if target.split(".", 1)[0] in {"light", "switch", "button"}
    )
    action = sorted(actions)[0] if len(actions) == 1 else "service_action"
    return controllable, action


def collect(
    config: ha_read.AdapterConfig,
    *,
    connector: Callable[[ha_read.AdapterConfig], Any] = incident_monitor._connect,
    raw_state_reader: Callable[[ha_read.AdapterConfig, str], Any] = ha_read.request_json,
    observed_epoch: int | None = None,
) -> list[dict[str, object]]:
    current = int(time.time()) if observed_epoch is None else observed_epoch
    if current < 0:
        raise DiagnosticsError("invalid diagnostics time")
    automations = _catalogue(raw_state_reader(config, "/api/states"))
    socket = connector(config)
    command_id = 20
    detail_count = 0
    results: list[dict[str, object]] = []
    try:
        incident_monitor.authenticate(socket, config.token)
        for automation in automations:
            traces = _command(
                socket, command_id, "trace/list", domain="automation",
                item_id=automation["item_id"],
            )
            command_id += 1
            if not isinstance(traces, list):
                raise DiagnosticsError("Home Assistant trace list is invalid")
            for summary in traces[:MAX_TRACES_PER_AUTOMATION]:
                if not isinstance(summary, dict):
                    continue
                outcome_text = summary.get("script_execution") or summary.get("state")
                if outcome_text not in {"error", None}:
                    continue
                run_id = summary.get("run_id")
                if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
                    continue
                started = _epoch(summary.get("timestamp"), current)
                if started < current - TRACE_LOOKBACK_SECONDS:
                    continue
                if detail_count >= MAX_TOTAL_TRACE_DETAILS:
                    raise DiagnosticsError("too many Home Assistant trace details")
                trace = _command(
                    socket, command_id, "trace/get", domain="automation",
                    item_id=automation["item_id"], run_id=run_id,
                )
                command_id += 1
                detail_count += 1
                if outcome_text is None and not _has_explicit_error(trace):
                    continue
                error_code, cause_code, confidence = classify_failure(trace)
                targets, action_code = _targets_and_action(trace)
                target = targets[0] if len(targets) == 1 else None
                results.append({
                    "run_hash": hashlib.sha256(
                        f"{automation['item_id']}\0{run_id}".encode("ascii")
                    ).hexdigest(),
                    "automation_entity_id": automation["entity_id"],
                    "automation_item_hash": hashlib.sha256(
                        automation["item_id"].encode("ascii")
                    ).hexdigest(),
                    "outcome": "failed",
                    "started_epoch": started,
                    "observed_epoch": current,
                    "error_code": error_code,
                    "cause_code": cause_code,
                    "cause_confidence": confidence,
                    "action_code": action_code,
                    "target_entity_id": target,
                    "display_name": automation["display_name"],
                })
    finally:
        try:
            socket.close()
        except Exception:
            pass
    return results


def main() -> int:
    try:
        state_dir = incident_monitor._state_dir()
        store = incident_monitor.IncidentStore(
            state_dir / incident_monitor.DATABASE_NAME
        )
        try:
            records = collect(ha_read.load_config())
            recorded = 0
            incidents: set[int] = set()
            for record in records:
                result = store.record_automation_run(**record)
                recorded += int(result["recorded"])
                if result["incident_id"] is not None:
                    incidents.add(int(result["incident_id"]))
        finally:
            store.close()
        print(json.dumps({
            "schema_version": 1,
            "failed_traces_seen": len(records),
            "new_runs_recorded": recorded,
            "open_incidents_touched": len(incidents),
        }, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 0
    except (
        DiagnosticsError,
        incident_monitor.MonitorError,
        ha_read.AdapterError,
        OSError,
        sqlite3.Error,
    ):
        print("HOME_ASSISTANT_AUTOMATION_DIAGNOSTICS_FAILED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
