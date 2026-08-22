#!/usr/bin/env python3
"""Fail-closed Alice intent bridge for bounded Home Butler reads and controls."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_notify as notify  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402
import incident_monitor  # noqa: E402
import incident_status  # noqa: E402
import model_ha_control  # noqa: E402
import model_ha_proof  # noqa: E402


MAX_PHRASE_CHARS = 128
DEDUPE_SECONDS = 10
SOCKET_TIMEOUT_SECONDS = 5
MAX_RECONNECT_SECONDS = 30
STOP_EVENT = threading.Event()


class VoiceBridgeError(RuntimeError):
    """Secret-free rejection or execution failure."""


class VoiceExecutionError(VoiceBridgeError):
    """Execution failure with conservative service-call accounting."""

    def __init__(self, message: str, control_calls: int, tts_calls: int) -> None:
        super().__init__(message)
        self.control_calls = control_calls
        self.tts_calls = tts_calls


@dataclass(frozen=True)
class Route:
    route_id: str
    kind: str
    entity_id: str | None = None
    action: str | None = None


STATUS_ROUTE = Route("home_status", "status")
INCIDENT_ROUTE = Route("incident_status", "incidents")
CORRIDOR_ON_ROUTE = Route(
    "corridor_light_on", "control", "switch.kavidor_switch_1", "turn_on"
)
CORRIDOR_OFF_ROUTE = Route(
    "corridor_light_off", "control", "switch.kavidor_switch_1", "turn_off"
)

INTENT_ROUTES = {
    "дворецкий статус дома": STATUS_ROUTE,
    "дворецкий что сломалось": INCIDENT_ROUTE,
    "дворецкий включи свет в коридоре": CORRIDOR_ON_ROUTE,
    "дворецкий выключи свет в коридоре": CORRIDOR_OFF_ROUTE,
}
SCENARIO_ROUTES = {
    "home butler 01 статус дома": STATUS_ROUTE,
    "home butler 02 что сломалось": INCIDENT_ROUTE,
    "home butler 03 включить свет в коридоре": CORRIDOR_ON_ROUTE,
    "home butler 04 выключить свет в коридоре": CORRIDOR_OFF_ROUTE,
}


def emit(event: str, **fields: object) -> None:
    """Journal only route/result metadata, never phrases, tokens or attributes."""
    print(
        json.dumps(
            {"event": event, "component": "alice_voice_bridge", **fields},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )


def normalize_phrase(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_PHRASE_CHARS
        or any(ord(character) < 32 for character in value)
    ):
        raise VoiceBridgeError("invalid voice phrase")
    return " ".join(value.casefold().split())


def route_from_event(document: dict[str, Any]) -> tuple[Route, str] | None:
    if document.get("type") != "event":
        return None
    event = document.get("event")
    if not isinstance(event, dict):
        return None
    event_type = event.get("event_type")
    if event_type not in {"yandex_intent", "yandex_scenario"}:
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        raise VoiceBridgeError("invalid voice event")
    raw_name = data.get("text" if event_type == "yandex_intent" else "scenario_name")
    try:
        normalized = normalize_phrase(raw_name)
    except VoiceBridgeError:
        return None
    routes = INTENT_ROUTES if event_type == "yandex_intent" else SCENARIO_ROUTES
    route = routes.get(normalized)
    if route is None:
        return None
    speaker = data.get("entity_id")
    if speaker not in notify.ALLOWED_SPEAKERS:
        raise VoiceBridgeError("voice source speaker is not allowed")
    return route, str(speaker)


class Deduplicator:
    def __init__(self, window_seconds: int = DEDUPE_SECONDS) -> None:
        if window_seconds < 1:
            raise VoiceBridgeError("invalid deduplication window")
        self.window_seconds = window_seconds
        self.seen: dict[tuple[str, str], float] = {}

    def accept(self, route: Route, speaker: str, observed_at: float) -> bool:
        key = (route.route_id, speaker)
        previous = self.seen.get(key)
        self.seen = {
            item: timestamp
            for item, timestamp in self.seen.items()
            if observed_at - timestamp < self.window_seconds
        }
        if previous is not None and observed_at - previous < self.window_seconds:
            return False
        self.seen[key] = observed_at
        return True


def _count(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise VoiceBridgeError("invalid status result")
    return value


def _run_model_control(entity_id: str, action: str, live: bool) -> dict[str, Any]:
    if live:
        return model_ha_control.run_control_proof(entity_id, action)

    def no_control(target: str, requested_action: str) -> tuple[dict[str, Any], int]:
        return (
            {
                "schema_version": 1,
                "ok": True,
                "status": "planned",
                "entity_id": target,
                "action": requested_action,
                "service_calls": 0,
            },
            0,
        )

    return model_ha_control.run_control_proof(
        entity_id, action, control_executor=no_control
    )


def execute_route(
    route: Route,
    speaker: str,
    *,
    live: bool,
    proof_runner: Callable[[], dict[str, Any]] = model_ha_proof.run_proof,
    incident_reader: Callable[[], dict[str, object]] = incident_status.read_summary,
    control_runner: Callable[[str, str, bool], dict[str, Any]] = _run_model_control,
    config_loader: Callable[[], ha_read.AdapterConfig] = ha_read.load_config,
    tts_sender: Callable[[ha_read.AdapterConfig, str, str], None] = notify.post_tts,
) -> dict[str, object]:
    control_calls = 0
    if route.kind == "status":
        proof = proof_runner()
        if proof.get("verified") is not True:
            raise VoiceBridgeError("model Home Assistant proof failed")
        status = proof.get("home_assistant")
        if not isinstance(status, dict):
            raise VoiceBridgeError("model Home Assistant proof is invalid")
        total = _count(status.get("entity_count"))
        available = _count(status.get("available_entity_count"))
        unavailable = _count(status.get("unavailable_entity_count"))
        message = (
            f"Home Butler. В Home Assistant всего {total} сущностей. "
            f"Доступно {available}, недоступно {unavailable}."
        )
    elif route.kind == "incidents":
        summary = incident_reader()
        opened = _count(summary.get("open_count"))
        confirmed = _count(summary.get("confirmed_count"))
        actionable = _count(summary.get("actionable_count"))
        message = (
            f"Home Butler. Открытых сбоев {opened}, подтверждённых {confirmed}, "
            f"требующих реакции {actionable}."
        )
    elif route.kind == "control":
        if route.entity_id is None or route.action is None:
            raise VoiceBridgeError("control route is incomplete")
        proof = control_runner(route.entity_id, route.action, live)
        result = proof.get("control_result")
        if proof.get("tool_call_verified") is not True or not isinstance(result, dict):
            raise VoiceBridgeError("model control proof failed")
        control_calls = _count(result.get("service_calls"))
        if result.get("ok") is not True or result.get("status") not in {
            "verified", "accepted", "planned"
        }:
            raise VoiceBridgeError("Home Assistant control was not verified")
        direction = "включён" if route.action == "turn_on" else "выключен"
        suffix = "" if live else " Сухая проверка, команда не отправлялась."
        message = f"Home Butler. Свет в коридоре {direction}.{suffix}"
    else:
        raise VoiceBridgeError("unsupported voice route")

    tts_calls = 0
    if live:
        try:
            tts_sender(config_loader(), speaker, message)
        except notify.NotifyDeliveryUnknown as error:
            raise VoiceExecutionError(
                "voice reply delivery is unknown", control_calls, 1
            ) from error
        except (notify.NotifyError, ha_read.AdapterError) as error:
            raise VoiceExecutionError(
                "voice reply was rejected", control_calls, 0
            ) from error
        tts_calls = 1
    return {
        "route_id": route.route_id,
        "action_kind": route.kind,
        "status": "completed" if live else "planned",
        "control_service_calls": control_calls,
        "tts_service_calls": tts_calls,
        "message": message,
    }


def process_document(
    document: dict[str, Any],
    store: incident_monitor.IncidentStore,
    deduplicator: Deduplicator,
    *,
    live: bool,
    now: Callable[[], float] = time.time,
    route_executor: Callable[..., dict[str, object]] = execute_route,
) -> dict[str, object] | None:
    selected = route_from_event(document)
    if selected is None:
        return None
    route, speaker = selected
    current = now()
    if not deduplicator.accept(route, speaker, current):
        return {"route_id": route.route_id, "status": "duplicate"}
    attempted_epoch = int(current)
    action_id = uuid.uuid4().hex
    store.record_voice_intent(
        action_id=action_id,
        route_id=route.route_id,
        action_kind=route.kind,
        speaker_entity_id=speaker,
        status="accepted",
        attempted_epoch=attempted_epoch,
        control_service_calls=0,
        tts_service_calls=0,
    )
    try:
        result = route_executor(route, speaker, live=live)
        control_calls = _count(result.get("control_service_calls"))
        tts_calls = _count(result.get("tts_service_calls"))
        store.record_voice_intent(
            action_id=action_id,
            route_id=route.route_id,
            action_kind=route.kind,
            speaker_entity_id=speaker,
            status="completed",
            attempted_epoch=attempted_epoch,
            control_service_calls=control_calls,
            tts_service_calls=tts_calls,
        )
        return result
    except Exception as error:
        control_calls = error.control_calls if isinstance(error, VoiceExecutionError) else 0
        tts_calls = error.tts_calls if isinstance(error, VoiceExecutionError) else 0
        store.record_voice_intent(
            action_id=action_id,
            route_id=route.route_id,
            action_kind=route.kind,
            speaker_entity_id=speaker,
            status="failed",
            attempted_epoch=attempted_epoch,
            control_service_calls=control_calls,
            tts_service_calls=tts_calls,
        )
        raise VoiceBridgeError("voice route execution failed") from error


def authenticate_and_subscribe(socket: Any, token: str) -> None:
    incident_monitor.authenticate(socket, token)
    for message_id, event_type in ((41, "yandex_intent"), (42, "yandex_scenario")):
        socket.send(incident_monitor._json({
            "id": message_id,
            "type": "subscribe_events",
            "event_type": event_type,
        }))
        response = incident_monitor._message(socket.recv())
        if (
            response.get("type") != "result"
            or response.get("id") != message_id
            or response.get("success") is not True
        ):
            raise VoiceBridgeError("voice event subscription failed")


def run_session(
    socket: Any,
    config: ha_read.AdapterConfig,
    store: incident_monitor.IncidentStore,
    *,
    live: bool,
) -> None:
    authenticate_and_subscribe(socket, config.token)
    emit("voice_events_subscribed", live=live, route_count=len(INTENT_ROUTES))
    socket.settimeout(SOCKET_TIMEOUT_SECONDS)
    deduplicator = Deduplicator()
    while not STOP_EVENT.is_set():
        try:
            document = incident_monitor._message(socket.recv())
        except Exception as error:
            if (
                incident_monitor.websocket is not None
                and isinstance(error, incident_monitor.websocket.WebSocketTimeoutException)
            ):
                continue
            raise VoiceBridgeError("Home Assistant voice websocket disconnected") from error
        try:
            result = process_document(document, store, deduplicator, live=live)
        except VoiceBridgeError:
            emit("voice_route_failed")
            continue
        if result is not None:
            emit(
                "voice_route_result",
                route_id=result["route_id"],
                status=result["status"],
            )


def run_forever(store: incident_monitor.IncidentStore, *, live: bool) -> None:
    config = ha_read.load_config()
    delay = 1
    while not STOP_EVENT.is_set():
        socket = None
        try:
            socket = incident_monitor._connect(config)
            run_session(socket, config, store, live=live)
            delay = 1
        except (VoiceBridgeError, incident_monitor.MonitorError, ha_read.AdapterError):
            emit("voice_websocket_reconnect_wait", seconds=delay)
            STOP_EVENT.wait(delay)
            delay = min(MAX_RECONNECT_SECONDS, delay * 2)
        finally:
            if socket is not None:
                try:
                    socket.close()
                except Exception:
                    pass


def _signal_handler(_signum: int, _frame: object) -> None:
    STOP_EVENT.set()


def main() -> int:
    mode = os.environ.get("HOME_BUTLER_ALICE_VOICE", "dry_run")
    if mode not in {"dry_run", "live"}:
        print("ALICE_VOICE_BRIDGE_FAILED", file=sys.stderr)
        return 2
    try:
        state_dir = incident_monitor._state_dir()
        incident_monitor._validate_directory(state_dir)
        store = incident_monitor.IncidentStore(
            state_dir / incident_monitor.DATABASE_NAME
        )
        try:
            signal.signal(signal.SIGTERM, _signal_handler)
            signal.signal(signal.SIGINT, _signal_handler)
            run_forever(store, live=mode == "live")
        finally:
            store.close()
    except (VoiceBridgeError, incident_monitor.MonitorError, ha_read.AdapterError, OSError):
        print("ALICE_VOICE_BRIDGE_FAILED", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
