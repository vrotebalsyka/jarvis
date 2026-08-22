#!/usr/bin/env python3
"""Monitor validated HA diagnostic/consumable rules and announce transitions."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import ha_model_study  # noqa: E402
import heartbeat  # noqa: E402
import home_assistant_notify as ha_notify  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402


MAX_FILE_BYTES = 512 * 1024


class MonitorError(RuntimeError):
    """Secret-free diagnostic monitor failure."""


def state_path() -> Path:
    return Path(os.environ.get(
        "HOME_BUTLER_DIAGNOSTIC_STATE",
        str(Path.home() / ".local/state/home-butler/diagnostic-alerts.json"),
    ))


def _load_private(path: Path, *, missing: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if missing is not None:
            return missing
        raise MonitorError("diagnostic catalog is unavailable")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
        or not 0 < metadata.st_size <= MAX_FILE_BYTES
    ):
        raise MonitorError("diagnostic state is unsafe")
    try:
        document = ha_read.strict_json_loads(path.read_bytes())
    except (OSError, ha_read.AdapterError) as error:
        raise MonitorError("diagnostic state is invalid") from error
    if not isinstance(document, dict):
        raise MonitorError("diagnostic state is invalid")
    return document


def evaluate(snapshot: dict[str, Any], catalog: dict[str, Any]) -> list[dict[str, Any]]:
    entities = snapshot.get("entities")
    findings = catalog.get("findings")
    if not isinstance(entities, list) or not isinstance(findings, list):
        raise MonitorError("diagnostic input is invalid")
    states = {
        item.get("entity_id"): item
        for item in entities
        if isinstance(item, dict) and isinstance(item.get("entity_id"), str)
    }
    active: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            raise MonitorError("diagnostic catalog is invalid")
        entity_id = finding.get("entity_id")
        condition = finding.get("alert_condition")
        category = finding.get("category")
        state = states.get(entity_id) if isinstance(entity_id, str) else None
        if not isinstance(state, dict) or state.get("state_kind") == "unavailable":
            continue
        value = state.get("state_value")
        triggered = (
            condition == "on" and value == "on"
            or condition == "nonzero" and isinstance(value, (int, float))
            and not isinstance(value, bool) and value != 0
            or condition == "at_or_below_10" and isinstance(value, (int, float))
            and not isinstance(value, bool) and 0 <= value <= 10
        )
        if triggered:
            active.append({
                "entity_id": entity_id,
                "friendly_name": finding.get("friendly_name"),
                "category": category,
                "state_value": value,
            })
    return sorted(active, key=lambda item: str(item["entity_id"]))


def render_message(alerts: list[dict[str, Any]], *, resolved: bool = False) -> str:
    if not alerts:
        raise MonitorError("diagnostic alert set is empty")
    parts: list[str] = []
    for alert in alerts[:3]:
        name = ha_read.sanitize_friendly_name(alert.get("friendly_name"))
        if name is None:
            name = str(alert["entity_id"]).split(".", 1)[1].replace("_", " ")[:100]
        category = alert.get("category")
        if resolved:
            parts.append(f"{name}: проблема устранена")
        elif category == "remaining_life":
            parts.append(f"{name}: осталось {round(float(alert['state_value']))} процентов ресурса")
        elif category == "error_code":
            parts.append(f"{name}: код ошибки {alert['state_value']}")
        elif category == "consumable_shortage":
            parts.append(f"{name}: требуется пополнить расходник")
        else:
            parts.append(f"{name}: устройство сообщает о проблеме")
    extra = len(alerts) - len(parts)
    if extra > 0:
        parts.append(f"и ещё {extra}")
    prefix = "Диагностика восстановлена. " if resolved else "Внимание. "
    message = prefix + "; ".join(parts) + "."
    if len(message) > ha_notify.MAX_MESSAGE_CHARS:
        raise MonitorError("diagnostic notification is too long")
    return message


def run_once(
    *,
    live: bool,
    catalog_loader: Callable[[], dict[str, Any]],
    previous_loader: Callable[[], dict[str, Any]],
    snapshot_reader: Callable[[str], tuple[dict[str, Any], int]] = ha_read.execute_safely,
    notifier: Callable[[ha_read.AdapterConfig, str, str], None] = ha_notify.post_tts,
    config_loader: Callable[[], ha_read.AdapterConfig] = ha_read.load_config,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    snapshot, exit_code = snapshot_reader("snapshot")
    if exit_code != 0:
        raise MonitorError("diagnostic snapshot is unavailable")
    current = evaluate(snapshot, catalog_loader())
    previous = previous_loader().get("active_alerts", [])
    if not isinstance(previous, list):
        raise MonitorError("diagnostic previous state is invalid")
    old = {item.get("entity_id"): item for item in previous if isinstance(item, dict)}
    new = {item["entity_id"]: item for item in current}
    detected = [new[key] for key in sorted(set(new) - set(old))]
    resolved = [old[key] for key in sorted(set(old) - set(new))]
    message = render_message(detected) if detected else render_message(resolved, resolved=True) if resolved else None
    service_calls = 0
    delivery = "not_needed"
    if live and message is not None:
        speaker = ha_notify.choose_speaker(snapshot)
        try:
            notifier(config_loader(), speaker, message)
            delivery = "accepted"
        except ha_notify.NotifyDeliveryUnknown:
            delivery = "delivery_unknown"
        service_calls = 1
    return {
        "schema_version": 1,
        "observed_epoch": int(now()),
        "active_alert_count": len(current),
        "detected_count": len(detected),
        "resolved_count": len(resolved),
        "active_alerts": current,
        "message": message,
        "service_calls": service_calls,
        "delivery": delivery,
        "actions_performed": 0,
    }


def write_state(document: dict[str, Any], path: Path | None = None) -> None:
    target = state_path() if path is None else path
    heartbeat._validate_state_dir(target.parent)
    heartbeat._atomic_write(
        target,
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    try:
        document = run_once(
            live=args.live,
            catalog_loader=lambda: _load_private(ha_model_study.catalog_path()),
            previous_loader=lambda: _load_private(
                state_path(), missing={"schema_version": 1, "active_alerts": []}
            ),
        )
        write_state(document)
    except (MonitorError, ha_read.AdapterError, ha_notify.NotifyError, OSError):
        print('{"schema_version":1,"status":"failed"}')
        return 3
    print(json.dumps({
        "schema_version": 1,
        "status": "observed",
        "active_alert_count": document["active_alert_count"],
        "detected_count": document["detected_count"],
        "resolved_count": document["resolved_count"],
        "service_calls": document["service_calls"],
        "actions_performed": 0,
    }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
