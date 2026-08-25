#!/usr/bin/env python3
"""Monitor validated HA diagnostic/consumable rules and announce transitions."""

from __future__ import annotations

import argparse
import hashlib
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


def _is_available(state: dict[str, Any] | None) -> bool:
    return isinstance(state, dict) and state.get("state_kind") not in {
        "unavailable", "redacted", "absent", None,
    }


def _condition_triggered(state: dict[str, Any], condition: dict[str, Any]) -> bool:
    operator = condition.get("operator")
    threshold = condition.get("threshold")
    state_kind = state.get("state_kind")
    value = state.get("state_value")
    if operator == "unavailable":
        return state_kind in {"unavailable", "absent"}
    if state_kind in {"unavailable", "redacted", "absent"}:
        return False
    if operator == "none":
        return False
    if operator == "on":
        return value == "on"
    if operator == "nonzero":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value != 0
        )
    if operator in {"less_or_equal", "greater_or_equal"}:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not isinstance(threshold, (int, float))
            or isinstance(threshold, bool)
        ):
            return False
        return value <= threshold if operator == "less_or_equal" else value >= threshold
    if operator == "nonempty":
        return isinstance(value, str) and bool(value.strip())
    raise MonitorError("diagnostic monitoring condition is invalid")


def _owner_explanation(profile: dict[str, Any], value: Any) -> str:
    component = ha_read.sanitize_friendly_name(profile.get("component"))
    if component is None:
        component = "отдельный компонент"
    issue_class = profile.get("issue_class")
    if issue_class == "error_code":
        return (
            f"{component}: код ошибки {value}; точное значение интеграция не передала"
        )
    if issue_class == "consumable_level":
        return f"{component}: осталось {round(float(value))} процентов ресурса"
    if issue_class == "consumable_shortage":
        return f"{component}: требуется пополнить расходник"
    if issue_class == "maintenance":
        return f"{component}: требуется обслуживание"
    if issue_class == "connectivity":
        return f"{component}: интеграция сообщает проблему связи"
    return f"{component}: устройство сообщает о проблеме"


def evaluate(snapshot: dict[str, Any], catalog: dict[str, Any]) -> list[dict[str, Any]]:
    entities = snapshot.get("entities")
    migrated = ha_model_study.migrate_catalog_document(catalog)
    profiles = migrated.get("profiles")
    if not isinstance(entities, list) or not isinstance(profiles, list):
        raise MonitorError("diagnostic input is invalid")
    states = {
        item.get("entity_id"): item
        for item in entities
        if isinstance(item, dict) and isinstance(item.get("entity_id"), str)
    }
    profiles_by_device: dict[str, list[dict[str, Any]]] = {}
    for profile in profiles:
        if isinstance(profile, dict) and isinstance(profile.get("physical_device_id"), str):
            profiles_by_device.setdefault(str(profile["physical_device_id"]), []).append(profile)
    active: list[dict[str, Any]] = []
    for profile in profiles:
        if not isinstance(profile, dict):
            raise MonitorError("diagnostic catalog is invalid")
        entity_id = profile.get("entity_id")
        condition = profile.get("recommended_monitoring_condition")
        state = states.get(entity_id) if isinstance(entity_id, str) else None
        if not isinstance(state, dict) or not isinstance(condition, dict):
            raise MonitorError("diagnostic catalog is invalid")
        if _condition_triggered(state, condition):
            physical_id = profile.get("physical_device_id")
            siblings = profiles_by_device.get(str(physical_id), [])
            sibling_states = [
                states.get(item.get("entity_id"))
                for item in siblings
                if isinstance(item.get("entity_id"), str)
            ]
            available_features = sum(_is_available(item) for item in sibling_states)
            value = state.get("state_value")
            finding_seed = (
                f"{profile.get('profile_id')}\0{profile.get('issue_class')}\0{entity_id}"
            )
            timestamp = state.get("source_last_updated_at")
            active.append({
                "finding_id": hashlib.sha256(finding_seed.encode("utf-8")).hexdigest(),
                "physical_device_id": physical_id,
                "physical_display_name": profile.get("physical_display_name"),
                "entity_id": entity_id,
                "feature_id": entity_id,
                "component": profile.get("component"),
                "issue_class": profile.get("issue_class"),
                "observed_value": value,
                "severity": profile.get("severity_policy", "unknown"),
                "confidence": profile.get("classification_confidence", 0.0),
                "evidence_refs": [
                    value for value in (
                        profile.get("profile_id"), profile.get("metadata_hash"), timestamp
                    ) if isinstance(value, str)
                ],
                "first_observed": timestamp,
                "last_observed": timestamp,
                "owner_explanation": _owner_explanation(profile, value),
                "suggested_playbook_ids": [],
                "actionability": "observe_only",
                "resolution_condition": {
                    "operator": "not_triggered",
                    "source_condition": condition,
                },
                "physical_device_available": available_features > 0,
                "available_feature_count": available_features,
                "total_feature_count": len(sibling_states),
            })
    return sorted(active, key=lambda item: str(item["finding_id"]))


def render_message(alerts: list[dict[str, Any]], *, resolved: bool = False) -> str:
    if not alerts:
        raise MonitorError("diagnostic alert set is empty")
    parts: list[str] = []
    for alert in alerts[:3]:
        name = ha_read.sanitize_friendly_name(alert.get("physical_display_name"))
        component = ha_read.sanitize_friendly_name(alert.get("component"))
        if name is None:
            name = "Устройство"
        if component is None:
            component = "отдельный компонент"
        if resolved:
            parts.append(f"{name}, {component}: проблема устранена")
        elif alert.get("physical_device_available") is True:
            parts.append(f"{name}: сам прибор доступен. {alert.get('owner_explanation')}")
        else:
            parts.append(f"{name}: {alert.get('owner_explanation')}")
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
    old = {
        item.get("finding_id") or item.get("entity_id"): item
        for item in previous if isinstance(item, dict)
    }
    new = {item["finding_id"]: item for item in current}
    for key in set(old) & set(new):
        if old[key].get("first_observed") is not None:
            new[key]["first_observed"] = old[key]["first_observed"]
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
        "schema_version": 2,
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
                state_path(), missing={"schema_version": 2, "active_alerts": []}
            ),
        )
        write_state(document)
    except (MonitorError, ha_read.AdapterError, ha_notify.NotifyError, OSError):
        print('{"schema_version":2,"status":"failed"}')
        return 3
    print(json.dumps({
        "schema_version": 2,
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
