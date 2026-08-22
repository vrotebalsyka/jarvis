#!/usr/bin/env python3
"""Strict Home Assistant control boundary for bounded device features."""

from __future__ import annotations

import argparse
import http.client
import json
import math
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_read as ha_read  # noqa: E402


ACTION_PATHS = {
    ("switch", "turn_on"): "/api/services/switch/turn_on",
    ("switch", "turn_off"): "/api/services/switch/turn_off",
    ("switch", "toggle"): "/api/services/switch/toggle",
    ("button", "press"): "/api/services/button/press",
    ("light", "turn_on"): "/api/services/light/turn_on",
    ("light", "turn_off"): "/api/services/light/turn_off",
    ("light", "toggle"): "/api/services/light/toggle",
    ("fan", "turn_on"): "/api/services/fan/turn_on",
    ("fan", "turn_off"): "/api/services/fan/turn_off",
    ("fan", "toggle"): "/api/services/fan/toggle",
    ("humidifier", "turn_on"): "/api/services/humidifier/turn_on",
    ("humidifier", "turn_off"): "/api/services/humidifier/turn_off",
    ("humidifier", "toggle"): "/api/services/humidifier/toggle",
    ("siren", "turn_on"): "/api/services/siren/turn_on",
    ("siren", "turn_off"): "/api/services/siren/turn_off",
    ("siren", "toggle"): "/api/services/siren/toggle",
    ("vacuum", "start"): "/api/services/vacuum/start",
    ("vacuum", "stop"): "/api/services/vacuum/stop",
    ("vacuum", "return_home"): "/api/services/vacuum/return_to_base",
    ("number", "set_value"): "/api/services/number/set_value",
    ("select", "set_option"): "/api/services/select/select_option",
}
MAX_RESPONSE_BYTES = 4 * 1_048_576
VERIFY_ATTEMPTS = 6
VERIFY_INTERVAL_SECONDS = 0.35


class ControlError(RuntimeError):
    """A fixed, secret-free control failure."""

    def __init__(
        self,
        message: str,
        *,
        status: str = "rejected",
        service_calls: int = 0,
        delivery: str = "not_sent",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.service_calls = service_calls
        self.delivery = delivery


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_request(
    entity_id: str, action: str, value: object = None
) -> tuple[str, str]:
    try:
        normalized_id = ha_read._validate_entity_id(entity_id)
    except ha_read.AdapterError as error:
        raise ControlError("invalid entity") from error
    domain = normalized_id.split(".", 1)[0]
    if (domain, action) not in ACTION_PATHS:
        raise ControlError("unsupported action")
    if action == "set_value" and (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise ControlError("invalid numeric value")
    if action == "set_option" and (
        not isinstance(value, str) or ha_read.sanitize_friendly_name(value) != value
    ):
        raise ControlError("invalid select option")
    if action not in {"set_value", "set_option"} and value is not None:
        raise ControlError("unexpected action value")
    return domain, ACTION_PATHS[(domain, action)]


def post_service(
    config: ha_read.AdapterConfig,
    entity_id: str,
    action: str,
    value: object = None,
    *,
    connection_factory: Callable[[ha_read.AdapterConfig], http.client.HTTPConnection] = ha_read._default_connection,
) -> None:
    _domain, path = validate_request(entity_id, action, value)
    payload: dict[str, object] = {"entity_id": entity_id}
    if action == "set_value":
        payload["value"] = float(value)
    elif action == "set_option":
        payload["option"] = value
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    try:
        connection = connection_factory(config)
    except (OSError, http.client.HTTPException) as error:
        raise ControlError("Home Assistant is unreachable") from error
    try:
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {config.token}",
                "Content-Type": "application/json",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES or response.status != 200:
            raise ControlError(
                "Home Assistant rejected the action",
                status="failed",
                service_calls=1,
                delivery="ha_rejected",
            )
        parsed = ha_read.strict_json_loads(raw)
        if not isinstance(parsed, (list, dict)):
            raise ControlError(
                "Home Assistant returned an invalid action response",
                status="delivery_unknown",
                service_calls=1,
                delivery="response_invalid",
            )
    except ControlError:
        raise
    except ha_read.AdapterError as error:
        raise ControlError(
            "Home Assistant returned an invalid action response",
            status="delivery_unknown",
            service_calls=1,
            delivery="response_invalid",
        ) from error
    except (OSError, socket.timeout, TimeoutError, http.client.HTTPException) as error:
        raise ControlError(
            "Home Assistant action failed",
            status="delivery_unknown",
            service_calls=1,
            delivery="transport_unknown",
        ) from error
    finally:
        try:
            connection.close()
        except (OSError, http.client.HTTPException):
            pass


def _snapshot(
    snapshot_reader: Callable[[str], tuple[dict[str, Any], int]],
) -> dict[str, Any]:
    snapshot, exit_code = snapshot_reader("snapshot")
    if exit_code != 0 or snapshot.get("status") not in {"healthy", "stale_data"}:
        raise ControlError("Home Assistant snapshot is unavailable")
    entities = snapshot.get("entities")
    if not isinstance(entities, list):
        raise ControlError("Home Assistant snapshot is invalid")
    return snapshot


def _find_entity(snapshot: dict[str, Any], entity_id: str) -> dict[str, Any]:
    matches = [
        entity for entity in snapshot["entities"]
        if isinstance(entity, dict) and entity.get("entity_id") == entity_id
    ]
    if len(matches) != 1:
        raise ControlError("entity is absent or ambiguous")
    return matches[0]


def execute(
    entity_id: str,
    action: str,
    value: object = None,
    *,
    snapshot_reader: Callable[[str], tuple[dict[str, Any], int]] = ha_read.execute_safely,
    service_caller: Callable[..., None] = post_service,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], int]:
    domain, _path = validate_request(entity_id, action, value)
    config = ha_read.load_config()
    if action in {"set_value", "set_option"}:
        catalogue, catalogue_exit = snapshot_reader("control-catalog")
        entries = catalogue.get("control_entities") if isinstance(catalogue, dict) else None
        selected = [
            item for item in entries or []
            if isinstance(item, dict) and item.get("entity_id") == entity_id
        ] if isinstance(entries, list) else []
        if catalogue_exit != 0 or len(selected) != 1 or selected[0].get("available") is not True:
            raise ControlError("controllable entity is unavailable")
        feature = selected[0]
        if action == "set_option":
            options = feature.get("options")
            if not isinstance(options, list) or value not in options:
                raise ControlError("select option is unavailable")
        else:
            minimum = feature.get("min")
            maximum = feature.get("max")
            numeric = float(value)
            if (
                not isinstance(minimum, (int, float))
                or not isinstance(maximum, (int, float))
                or not float(minimum) <= numeric <= float(maximum)
            ):
                raise ControlError("numeric value is outside the allowed range")
    before_snapshot = _snapshot(snapshot_reader)
    before = _find_entity(before_snapshot, entity_id)
    before_value = before.get("state_value")
    if domain in {"switch", "light", "fan", "humidifier", "siren"} and before_value not in {"on", "off"}:
        raise ControlError("controllable entity state is unavailable")

    expected_value: object = None
    if action == "turn_on":
        expected_value = "on"
    elif action == "turn_off":
        expected_value = "off"
    elif action == "toggle":
        expected_value = "off" if before_value == "on" else "on"
    elif action == "set_value":
        expected_value = float(value)
    elif action == "set_option":
        expected_value = str(value)

    if value is None:
        service_caller(config, entity_id, action)
    else:
        service_caller(config, entity_id, action, value)
    if domain == "button" or domain == "vacuum":
        after = _find_entity(_snapshot(snapshot_reader), entity_id)
        return (
            {
                "schema_version": 1,
                "ok": True,
                "status": "accepted",
                "entity_id": entity_id,
                "action": action,
                "requested_value": value,
                "before_state": before_value,
                "after_state": after.get("state_value"),
                "verification": "get_readback_completed",
                "observed_at": _now_iso(),
                "http_method": "POST_then_GET",
                "service_calls": 1,
            },
            0,
        )

    after = before
    for attempt in range(VERIFY_ATTEMPTS):
        if attempt:
            sleeper(VERIFY_INTERVAL_SECONDS)
        after = _find_entity(_snapshot(snapshot_reader), entity_id)
        observed_value = after.get("state_value")
        matches = observed_value == expected_value
        if action == "set_value" and isinstance(observed_value, (int, float)):
            matches = math.isclose(
                float(observed_value), float(expected_value), rel_tol=1e-6, abs_tol=1e-6
            )
        if matches:
            return (
                {
                    "schema_version": 1,
                    "ok": True,
                    "status": "verified",
                    "entity_id": entity_id,
                    "action": action,
                    "requested_value": value,
                    "before_state": before_value,
                    "after_state": after.get("state_value"),
                    "verification": "state_matches_expected",
                    "observed_at": _now_iso(),
                    "http_method": "POST_then_GET",
                    "service_calls": 1,
                },
                0,
            )
    return (
        {
            "schema_version": 1,
            "ok": False,
            "status": "not_verified",
            "entity_id": entity_id,
            "action": action,
            "requested_value": value,
            "before_state": before_value,
            "after_state": after.get("state_value"),
            "verification": "state_did_not_match_expected",
            "observed_at": _now_iso(),
            "http_method": "POST_then_GET",
            "service_calls": 1,
        },
        4,
    )


def execute_safely(
    entity_id: str, action: str, value: object = None
) -> tuple[dict[str, Any], int]:
    try:
        return execute(entity_id, action, value)
    except (ControlError, ha_read.AdapterError) as error:
        status = error.status if isinstance(error, ControlError) else "rejected"
        service_calls = (
            error.service_calls if isinstance(error, ControlError) else 0
        )
        delivery = (
            error.delivery if isinstance(error, ControlError) else "not_sent"
        )
        return (
            {
                "schema_version": 1,
                "ok": False,
                "status": status,
                "entity_id": entity_id if isinstance(entity_id, str) else None,
                "action": action if isinstance(action, str) else None,
                "requested_value": value,
                "observed_at": _now_iso(),
                "http_method": "POST" if service_calls else None,
                "service_calls": service_calls,
                "delivery": delivery,
            },
            4 if service_calls else 3,
        )


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("entity_id")
    parser.add_argument(
        "action",
        choices=(
            "turn_on", "turn_off", "toggle", "press", "start", "stop",
            "return_home", "set_value", "set_option",
        ),
    )
    parser.add_argument("value", nargs="?")
    arguments = parser.parse_args(argv)
    value: object = arguments.value
    if arguments.action == "set_value" and value is not None:
        try:
            value = float(value)
        except ValueError:
            pass
    result, exit_code = execute_safely(arguments.entity_id, arguments.action, value)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run())
