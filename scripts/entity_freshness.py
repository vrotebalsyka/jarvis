#!/usr/bin/env python3
"""Detect learned, long-overdue HA telemetry without touching devices."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_read as ha_read  # noqa: E402
import incident_monitor  # noqa: E402


FAST_TELEMETRY_TOKENS = {
    "temperature", "temperatura", "humidity", "vlazhnosti", "pressure",
    "illuminance", "co2", "pm1", "pm2", "pm10", "voc", "power",
    "voltage", "current", "signal", "moisture",
}
SLOW_TELEMETRY_TOKENS = {
    "battery", "batareia", "energy", "consumption", "total",
}
FAST_MINIMUM_STALE_SECONDS = 2 * 3600
SLOW_MINIMUM_STALE_SECONDS = 48 * 3600
CONFIRM_AFTER_SECONDS = 60


class FreshnessError(RuntimeError):
    """Fixed, secret-free telemetry freshness failure."""


def minimum_stale_seconds(entity_id: str) -> int | None:
    try:
        normalized = ha_read._validate_entity_id(entity_id)
    except ha_read.AdapterError as error:
        raise FreshnessError("invalid freshness entity") from error
    if not normalized.startswith("sensor."):
        return None
    tokens = set(normalized.split(".", 1)[1].split("_"))
    if tokens & SLOW_TELEMETRY_TOKENS:
        return SLOW_MINIMUM_STALE_SECONDS
    if tokens & FAST_TELEMETRY_TOKENS:
        return FAST_MINIMUM_STALE_SECONDS
    return None


def _source_epoch(value: Any) -> int:
    if not isinstance(value, str) or len(value) > 64:
        raise FreshnessError("invalid freshness timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise FreshnessError("invalid freshness timestamp") from error
    if parsed.tzinfo is None:
        raise FreshnessError("invalid freshness timestamp")
    return int(parsed.astimezone(timezone.utc).timestamp())


def run_once(
    store: incident_monitor.IncidentStore,
    *,
    observed_epoch: int,
    snapshot_reader: Callable[[str], tuple[dict[str, Any], int]] = ha_read.execute_safely,
) -> dict[str, int]:
    if observed_epoch < 0:
        raise FreshnessError("invalid freshness time")
    snapshot, exit_code = snapshot_reader("snapshot")
    entities = snapshot.get("entities") if isinstance(snapshot, dict) else None
    if exit_code != 0 or not isinstance(entities, list) or len(entities) > 4096:
        raise FreshnessError("Home Assistant freshness snapshot failed")
    counts = {
        "eligible": 0, "learning": 0, "fresh": 0,
        "stale": 0, "observed": 0, "resolved": 0,
    }
    stale_subjects: list[str] = []
    for item in entities:
        if not isinstance(item, dict):
            raise FreshnessError("Home Assistant freshness snapshot failed")
        entity_id = item.get("entity_id")
        if not isinstance(entity_id, str):
            raise FreshnessError("Home Assistant freshness snapshot failed")
        floor = minimum_stale_seconds(entity_id)
        if floor is None:
            continue
        counts["eligible"] += 1
        if item.get("state_kind") in {"unavailable", "redacted"}:
            continue
        source_epoch = _source_epoch(item.get("source_last_updated_at"))
        observation = store.record_freshness_observation(
            entity_id,
            source_epoch=source_epoch,
            observed_epoch=observed_epoch,
            minimum_stale_seconds=floor,
        )
        status = str(observation["status"])
        counts[status] += 1
        stale = bool(observation["stale"])
        result = store.observe(
            entity_id,
            "entity",
            "stale" if stale else "available",
            observed_epoch,
            unavailable=stale,
            source="freshness_audit",
        )
        if result is not None and result["event"] in {"observed", "resolved"}:
            counts[str(result["event"])] += 1
        if stale:
            stale_subjects.append(entity_id)
    store.confirm_due(observed_epoch, CONFIRM_AFTER_SECONDS)
    store.reconcile_device_incidents(observed_epoch)
    for entity_id in stale_subjects:
        store.diagnose_device_incident_for_subject(
            entity_id,
            cause_code="stale_entity_data",
            cause_confidence="probable",
            evidence_code="learned_report_cadence_exceeded",
        )
    return counts


def main() -> int:
    state_dir = incident_monitor._state_dir()
    try:
        incident_monitor._validate_directory(state_dir)
        store = incident_monitor.IncidentStore(
            state_dir / incident_monitor.DATABASE_NAME
        )
        try:
            result = run_once(store, observed_epoch=int(time.time()))
        finally:
            store.close()
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 0
    except (
        FreshnessError,
        incident_monitor.MonitorError,
        ha_read.AdapterError,
        OSError,
        sqlite3.Error,
    ):
        print("HOME_ASSISTANT_FRESHNESS_FAILED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
