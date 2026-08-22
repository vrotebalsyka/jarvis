#!/usr/bin/env python3
"""Turn compact HA system-log failures into sanitized operational incidents."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_read as ha_read  # noqa: E402
import incident_monitor  # noqa: E402


CURSOR_NAME = "system_log_v1"
MAX_LOG_ENTRIES = 512
MAX_FIELD_CHARS = 65_536
SAFE_SOURCE_RE = re.compile(r"^[a-z0-9_.:-]{1,160}$")
ENTITY_TOKEN_RE = re.compile(r"\b[a-z0-9_]{1,64}\.[a-z0-9_]{1,200}\b")
INTEGRATION_MARKERS = (
    ("tuya_sharing", "tuya"),
    ("custom_components.localtuya", "localtuya"),
    ("custom_components.tuya_local", "tuya_local"),
    ("midea_ac_lan", "midea_ac_lan"),
    ("xiaomi_miot", "xiaomi_miot"),
    ("yandex_smart_home", "yandex_smart_home"),
    ("yandex_station", "yandex_station"),
)


class SystemLogError(RuntimeError):
    """Secret-free system-log diagnostics failure."""


def _bounded_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value[:MAX_FIELD_CHARS]


def _entry_text(entry: dict[str, Any]) -> str:
    return "\n".join(
        _bounded_text(entry.get(name))
        for name in ("name", "message", "exception", "source")
    ).casefold()


def _integration_domain(text: str) -> str:
    for marker, domain in INTEGRATION_MARKERS:
        if marker in text:
            return domain
    custom = re.search(r"custom_components\.([a-z0-9_]{1,64})", text)
    if custom is not None:
        return custom.group(1)
    component = re.search(r"homeassistant\.components\.([a-z0-9_]{1,64})", text)
    if component is not None:
        return component.group(1)
    return "homeassistant"


def classify_entry(entry: Any) -> dict[str, object] | None:
    if not isinstance(entry, dict):
        raise SystemLogError("invalid Home Assistant system log")
    timestamp = entry.get("timestamp")
    count = entry.get("count", 1)
    if (
        not isinstance(timestamp, (int, float))
        or timestamp < 0
        or not isinstance(count, int)
        or not 1 <= count <= 1_000_000_000
    ):
        raise SystemLogError("invalid Home Assistant system log")
    text = _entry_text(entry)
    integration = _integration_domain(text)
    if "sign invalid" in text or "(-9999999)" in text:
        error_code = "cloud_signature_invalid"
        cause_code = "tuya_integration_unavailable"
        confidence = "confirmed"
    elif any(marker in text for marker in (
        "name or service not known", "temporary failure in name resolution",
        "nodename nor servname", "dns resolution", "getaddrinfo failed",
    )):
        error_code = "dns_resolution_failed"
        cause_code = "dns_resolution_failed"
        confidence = "confirmed"
    elif any(marker in text for marker in (
        "certificate verify failed", "sslerror", "tls failure", "ssl:",
    )):
        error_code = "tls_failure"
        cause_code = "tls_failure"
        confidence = "confirmed"
    elif any(marker in text for marker in (
        "timed out", "timeout", "asyncio.timeouterror",
    )):
        error_code = "upstream_timeout"
        cause_code = "upstream_timeout"
        confidence = "probable"
    elif any(marker in text for marker in (
        "network error", "network unreachable", "connection refused",
        "clientconnectorerror", "server disconnected", "connection reset",
    )):
        error_code = "network_failure"
        cause_code = (
            "yandex_cloud_unreachable"
            if integration.startswith("yandex_") else "upstream_timeout"
        )
        confidence = "probable"
    elif any(marker in text for marker in (
        "integration not loaded", "config entry not loaded",
        "setup failed", "setup_retry",
    )):
        error_code = "integration_not_loaded"
        cause_code = "integration_not_loaded"
        confidence = "confirmed"
    else:
        return None
    if cause_code == "upstream_timeout" and integration.startswith("yandex_"):
        cause_code = "yandex_cloud_unreachable"
    source_ref = integration if SAFE_SOURCE_RE.fullmatch(integration) else "homeassistant"
    seed = json.dumps(
        {
            "count": count,
            "source_ref": source_ref,
            "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "timestamp": round(float(timestamp), 6),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "event_hash": hashlib.sha256(seed.encode("ascii")).hexdigest(),
        "observed_epoch": int(float(timestamp)),
        "source_ref": source_ref,
        "error_code": error_code,
        "cause_code": cause_code,
        "cause_confidence": confidence,
        "text": text,
    }


def correlate_service_call(
    classification: dict[str, object],
    calls: list[dict[str, object]],
) -> tuple[str, str | None]:
    text = str(classification["text"])
    mentioned = set(ENTITY_TOKEN_RE.findall(text))
    candidates: list[dict[str, object]] = []
    for call in calls:
        action = f"{call.get('domain')}.{call.get('service')}"
        slash_action = action.replace(".", "/", 1)
        entity_ids = {
            value for value in call.get("entity_ids", [])
            if isinstance(value, str)
        }
        if mentioned & entity_ids or action in text or slash_action in text:
            candidates.append(call)
    if not candidates and len(calls) == 1 and any(
        marker in text for marker in ("service", "action", "turn_on", "turn_off")
    ):
        candidates = calls
    if len(candidates) != 1:
        return "service_action", None
    selected = candidates[0]
    domain = str(selected.get("domain"))
    service = str(selected.get("service"))
    action = f"{domain}.{service}"
    if re.fullmatch(r"[a-z0-9_.]{1,64}", action) is None:
        action = "service_action"
    entity_ids = [
        value for value in selected.get("entity_ids", [])
        if isinstance(value, str)
    ]
    return action, entity_ids[0] if len(entity_ids) == 1 else None


def run_once(
    store: incident_monitor.IncidentStore,
    entries: list[dict[str, Any]],
    *,
    observed_epoch: int,
) -> dict[str, int]:
    if observed_epoch < 0 or len(entries) > MAX_LOG_ENTRIES:
        raise SystemLogError("invalid Home Assistant system log")
    baseline = not store.diagnostic_cursor_exists(CURSOR_NAME)
    counts = {"entries": len(entries), "classified": 0, "recorded": 0, "incidents": 0}
    for entry in entries:
        classification = classify_entry(entry)
        if classification is None:
            continue
        counts["classified"] += 1
        event_epoch = int(classification["observed_epoch"])
        calls = store.recent_service_calls(event_epoch)
        action_code, target = correlate_service_call(classification, calls)
        source_ref = str(classification["source_ref"])
        result = store.record_operational_failure(
            event_hash=str(classification["event_hash"]),
            source_type="system_log",
            source_ref=source_ref,
            observed_epoch=event_epoch,
            error_code=str(classification["error_code"]),
            cause_code=str(classification["cause_code"]),
            cause_confidence=str(classification["cause_confidence"]),
            action_code=action_code,
            target_entity_id=target,
            display_name=(
                target.split(".", 1)[1].replace("_", " ")
                if target is not None else source_ref.replace("_", " ")
            ),
            evidence_code="ha_system_log_compact",
            baseline=baseline,
        )
        counts["recorded"] += int(bool(result["recorded"]))
        counts["incidents"] += int(result["incident_id"] is not None)
    store.mark_diagnostic_cursor(CURSOR_NAME, observed_epoch)
    return counts


def read_system_log(
    config: ha_read.AdapterConfig,
    *,
    connector: Callable[[ha_read.AdapterConfig], Any] = incident_monitor._connect,
) -> list[dict[str, Any]]:
    socket = connector(config)
    try:
        incident_monitor.authenticate(socket, config.token)
        socket.send(incident_monitor._json({"id": 90, "type": "system_log/list"}))
        for _attempt in range(64):
            response = incident_monitor._message(socket.recv())
            if response.get("id") != 90:
                continue
            if response.get("type") != "result" or response.get("success") is not True:
                raise SystemLogError("Home Assistant system log failed")
            entries = response.get("result")
            if not isinstance(entries, list) or len(entries) > MAX_LOG_ENTRIES:
                raise SystemLogError("Home Assistant system log failed")
            return entries
        raise SystemLogError("Home Assistant system log response is missing")
    finally:
        try:
            socket.close()
        except Exception:
            pass


def main() -> int:
    state_dir = incident_monitor._state_dir()
    try:
        incident_monitor._validate_directory(state_dir)
        store = incident_monitor.IncidentStore(
            state_dir / incident_monitor.DATABASE_NAME
        )
        try:
            result = run_once(
                store,
                read_system_log(ha_read.load_config()),
                observed_epoch=int(time.time()),
            )
        finally:
            store.close()
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 0
    except (
        SystemLogError,
        incident_monitor.MonitorError,
        ha_read.AdapterError,
        OSError,
        sqlite3.Error,
    ):
        print("HOME_ASSISTANT_SYSTEM_LOG_DIAGNOSTICS_FAILED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
