#!/usr/bin/env python3
"""Fixed Home Assistant YandexStation TTS boundary for critical alerts."""

from __future__ import annotations

import http.client
import json
import socket
import sys
from pathlib import Path
from typing import Any, Callable


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_read as ha_read  # noqa: E402


SERVICE_PATH = "/api/services/tts/yandex_station_say"
PRIMARY_SPEAKER = "media_player.yandex_station_m10vgng0005wxb"
FALLBACK_SPEAKER = "media_player.yandex_station_x10x2a000qpm2b"
ALLOWED_SPEAKERS = (PRIMARY_SPEAKER, FALLBACK_SPEAKER)
MAX_MESSAGE_CHARS = 480
MAX_RESPONSE_BYTES = 1_048_576
NOTIFICATION_TIMEOUT_SECONDS = 25


class NotifyError(RuntimeError):
    """Fixed, secret-free notification failure."""


class NotifyDeliveryUnknown(NotifyError):
    """The request was sent but Home Assistant did not confirm the outcome."""


def _notification_connection(config: ha_read.AdapterConfig) -> http.client.HTTPConnection:
    return http.client.HTTPConnection(
        config.host, config.port, timeout=NOTIFICATION_TIMEOUT_SECONDS
    )


def render_incident_message(subject: str, phase: str) -> str:
    if phase not in {"confirmed", "resolved"}:
        raise NotifyError("unsupported notification phase")
    if subject == "home_assistant.core":
        if phase == "confirmed":
            return (
                "Внимание. Home Assistant недоступен больше минуты. "
                "Автоматическая перезагрузка пока не выполнялась."
            )
        return "Home Assistant снова доступен после подтверждённого сбоя."
    try:
        normalized = ha_read._validate_entity_id(subject)
    except ha_read.AdapterError as error:
        raise NotifyError("invalid incident subject") from error
    object_name = normalized.split(".", 1)[1].replace("_", " ")[:100]
    if phase == "confirmed":
        return f"Внимание. Подтверждён критический сбой устройства: {object_name}."
    return f"Устройство снова доступно: {object_name}."


def render_sensor_message(subject: str, phase: str) -> str:
    if phase not in {"confirmed", "resolved"}:
        raise NotifyError("unsupported notification phase")
    try:
        normalized = ha_read._validate_entity_id(subject)
    except ha_read.AdapterError as error:
        raise NotifyError("invalid incident subject") from error
    if not normalized.startswith(("sensor.", "binary_sensor.")):
        raise NotifyError("sensor notification requires a sensor")
    object_name = normalized.split(".", 1)[1].replace("_", " ")[:100]
    if phase == "confirmed":
        return f"Внимание. Датчик недоступен больше двух минут: {object_name}."
    return f"Датчик снова доступен: {object_name}."


def render_device_message(
    display_name: str,
    phase: str,
    *,
    cause_code: str = "unknown",
    duration_seconds: int | None = None,
) -> str:
    """Render one notice for a physical device, not each HA entity."""
    if phase not in {"confirmed", "resolved"}:
        raise NotifyError("unsupported notification phase")
    normalized_name = " ".join(display_name.strip().split())
    if (
        not normalized_name
        or len(normalized_name) > 100
        or any(ord(character) < 32 for character in normalized_name)
    ):
        raise NotifyError("invalid device display name")
    cause_text = {
        "confirmed_ip_change": " Устройство сменило сетевой адрес.",
        "tuya_integration_unavailable": " Локальная интеграция Tuya не отвечает.",
        "yandex_cloud_unreachable": " Облако Яндекса временно недоступно.",
        "automation_action_failed": " Автоматизация не выполнила действие.",
        "command_not_confirmed": " Команда устройством не подтверждена.",
        "home_assistant_unreachable": " Home Assistant временно недоступен.",
        "stale_entity_data": " Данные устройства давно не обновлялись.",
        "device_not_observed_on_lan": " Устройство не обнаружено в домашней сети.",
        "integration_not_loaded": " Интеграция устройства не загружена.",
        "integration_unavailable": " Интеграция устройства не отвечает.",
        "partial_entity_unavailable": " Home Assistant пометил часть функций устройства как недоступную.",
        "unknown": "",
    }.get(cause_code)
    if cause_text is None:
        raise NotifyError("unsupported device cause")
    if phase == "confirmed":
        message = f"Внимание. Устройство стало недоступно: {normalized_name}.{cause_text}"
    else:
        duration = ""
        if duration_seconds is not None:
            if not isinstance(duration_seconds, int) or not 0 <= duration_seconds <= 31_536_000:
                raise NotifyError("invalid incident duration")
            minutes = max(1, round(duration_seconds / 60))
            duration = f" Сбой длился около {minutes} минут."
        message = f"Устройство снова доступно: {normalized_name}.{duration}"
    if len(message) > MAX_MESSAGE_CHARS:
        raise NotifyError("device notification is too long")
    return message


def render_operational_message(
    display_name: str,
    phase: str,
    *,
    cause_code: str,
    action_code: str,
    duration_seconds: int | None,
    detected_was_announced: bool,
    agent_recovered: bool,
    source_type: str = "automation",
) -> str:
    if phase not in {"detected", "resolved"}:
        raise NotifyError("unsupported operational phase")
    name = " ".join(display_name.strip().split())
    if (
        not 1 <= len(name) <= 100
        or any(ord(character) < 32 for character in name)
        or source_type not in {
            "automation", "integration", "service_call", "system_log"
        }
    ):
        raise NotifyError("invalid operational display name")
    action = {
        "light.turn_on": "включение света",
        "light.turn_off": "выключение света",
        "switch.turn_on": "включение реле",
        "switch.turn_off": "выключение реле",
        "service_action": "действие сценария",
        "integration.health": "проверку состояния интеграции",
    }.get(action_code)
    cause = {
        "yandex_cloud_unreachable": "у Home Assistant не было связи с облаком Яндекса",
        "dns_resolution_failed": "Home Assistant не смог определить адрес облачного сервиса",
        "upstream_timeout": "облачный сервис не ответил вовремя",
        "tls_failure": "защищённое соединение с облачным сервисом не установилось",
        "integration_not_loaded": "интеграция не была загружена",
        "integration_unavailable": "интеграция перестала отвечать",
        "tuya_integration_unavailable": "интеграция Tuya отклонила запрос",
        "home_assistant_unreachable": "Home Assistant был недоступен",
        "automation_action_failed": "Home Assistant не подтвердил действие автоматизации",
        "command_not_confirmed": "реле осталось в прежнем состоянии после трёх проверок",
    }.get(cause_code)
    if action is None or cause is None:
        raise NotifyError("unsupported operational incident")
    if phase == "detected":
        if source_type == "integration":
            message = (
                f"Интеграция {name} недоступна: {cause}. "
                "Я проверю её и применю только безопасное восстановление."
            )
        else:
            message = (
                f"Сценарий {name} не выполнил {action}: {cause}. "
                "Я проверю состояние и применю только безопасное восстановление."
            )
    else:
        if duration_seconds is None or not 0 <= duration_seconds <= 31_536_000:
            raise NotifyError("invalid operational duration")
        minutes = max(1, round(duration_seconds / 60))
        recovery = (
            "я восстановил его и проверил результат"
            if agent_recovered else "он восстановился без управляющего действия"
        )
        if source_type == "integration":
            prefix = "Интеграция" if detected_was_announced else "Был сбой интеграции"
            message = (
                f"{prefix} {name} снова в норме: {recovery}. "
                f"Сбой длился около {minutes} минут."
            )
        else:
            prefix = "Сценарий" if detected_was_announced else "Был сбой сценария"
            message = (
                f"{prefix} {name} снова в норме: {recovery}. "
                f"Сбой длился около {minutes} минут."
            )
    if len(message) > MAX_MESSAGE_CHARS:
        raise NotifyError("operational notification is too long")
    return message


def choose_speaker(
    snapshot: dict[str, Any], *, required_speaker: str | None = None
) -> str:
    entities = snapshot.get("entities")
    if not isinstance(entities, list):
        raise NotifyError("speaker snapshot is unavailable")
    states = {
        item.get("entity_id"): item.get("state_kind")
        for item in entities
        if isinstance(item, dict)
    }
    if required_speaker is not None and required_speaker not in ALLOWED_SPEAKERS:
        raise NotifyError("speaker is not allowed")
    speakers = (required_speaker,) if required_speaker is not None else ALLOWED_SPEAKERS
    for speaker in speakers:
        if states.get(speaker) not in {None, "unavailable", "redacted"}:
            return speaker
    raise NotifyError("no allowed speaker is available")


def post_tts(
    config: ha_read.AdapterConfig,
    speaker_entity_id: str,
    message: str,
    *,
    connection_factory: Callable[[ha_read.AdapterConfig], http.client.HTTPConnection] = _notification_connection,
) -> None:
    if speaker_entity_id not in ALLOWED_SPEAKERS:
        raise NotifyError("speaker is not allowed")
    if (
        not isinstance(message, str)
        or not 1 <= len(message) <= MAX_MESSAGE_CHARS
        or any(ord(character) < 32 for character in message)
    ):
        raise NotifyError("invalid notification message")
    body = json.dumps(
        {"entity_id": speaker_entity_id, "message": message},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    request_sent = False
    try:
        connection = connection_factory(config)
        connection.request(
            "POST",
            SERVICE_PATH,
            body=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {config.token}",
                "Content-Type": "application/json",
                "Connection": "close",
            },
        )
        request_sent = True
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if response.status != 200 or len(raw) > MAX_RESPONSE_BYTES:
            raise NotifyError("Home Assistant rejected the notification")
        if not isinstance(ha_read.strict_json_loads(raw), (list, dict)):
            raise NotifyError("Home Assistant returned an invalid notification response")
    except NotifyError:
        raise
    except (OSError, socket.timeout, TimeoutError, http.client.HTTPException) as error:
        if request_sent:
            raise NotifyDeliveryUnknown("Home Assistant notification delivery is unknown") from error
        raise NotifyError("Home Assistant notification failed") from error
    finally:
        try:
            connection.close()
        except (UnboundLocalError, OSError, http.client.HTTPException):
            pass


def send_incident(
    subject: str,
    phase: str,
    *,
    live: bool,
    snapshot_reader: Callable[[str], tuple[dict[str, Any], int]] = ha_read.execute_safely,
    service_caller: Callable[[ha_read.AdapterConfig, str, str], None] = post_tts,
) -> dict[str, object]:
    message = render_incident_message(subject, phase)
    snapshot, exit_code = snapshot_reader("snapshot")
    if exit_code != 0:
        raise NotifyError("speaker snapshot is unavailable")
    speaker = choose_speaker(snapshot)
    if live:
        service_caller(ha_read.load_config(), speaker, message)
    return {
        "schema_version": 1,
        "ok": True,
        "mode": "live" if live else "dry_run",
        "status": "accepted" if live else "planned",
        "service": "tts.yandex_station_say",
        "speaker_entity_id": speaker,
        "subject": subject,
        "phase": phase,
        "message": message,
        "service_calls": 1 if live else 0,
        "verification": "ha_service_accepted_no_audible_readback" if live else "not_sent",
    }


def send_sensor_incident(
    subject: str,
    phase: str,
    *,
    live: bool,
    snapshot_reader: Callable[[str], tuple[dict[str, Any], int]] = ha_read.execute_safely,
    service_caller: Callable[[ha_read.AdapterConfig, str, str], None] = post_tts,
) -> dict[str, object]:
    message = render_sensor_message(subject, phase)
    snapshot, exit_code = snapshot_reader("snapshot")
    if exit_code != 0:
        raise NotifyError("speaker snapshot is unavailable")
    speaker = choose_speaker(snapshot, required_speaker=FALLBACK_SPEAKER)
    if live:
        service_caller(ha_read.load_config(), speaker, message)
    return {
        "schema_version": 1,
        "ok": True,
        "mode": "live" if live else "dry_run",
        "status": "accepted" if live else "planned",
        "service": "tts.yandex_station_say",
        "speaker_entity_id": speaker,
        "subject": subject,
        "phase": phase,
        "message": message,
        "service_calls": 1 if live else 0,
        "verification": "ha_service_accepted_no_audible_readback" if live else "not_sent",
    }


def send_device_incident(
    display_name: str,
    phase: str,
    *,
    cause_code: str,
    duration_seconds: int | None,
    live: bool,
    snapshot_reader: Callable[[str], tuple[dict[str, Any], int]] = ha_read.execute_safely,
    service_caller: Callable[[ha_read.AdapterConfig, str, str], None] = post_tts,
) -> dict[str, object]:
    message = render_device_message(
        display_name,
        phase,
        cause_code=cause_code,
        duration_seconds=duration_seconds,
    )
    snapshot, exit_code = snapshot_reader("snapshot")
    if exit_code != 0:
        raise NotifyError("speaker snapshot is unavailable")
    speaker = choose_speaker(snapshot, required_speaker=FALLBACK_SPEAKER)
    if live:
        service_caller(ha_read.load_config(), speaker, message)
    return {
        "schema_version": 1,
        "ok": True,
        "mode": "live" if live else "dry_run",
        "status": "accepted" if live else "planned",
        "service": "tts.yandex_station_say",
        "speaker_entity_id": speaker,
        "display_name": display_name,
        "phase": phase,
        "message": message,
        "service_calls": 1 if live else 0,
        "verification": "ha_service_accepted_no_audible_readback" if live else "not_sent",
    }


def send_operational_incident(
    display_name: str,
    phase: str,
    *,
    cause_code: str,
    action_code: str,
    duration_seconds: int | None,
    detected_was_announced: bool,
    agent_recovered: bool,
    live: bool,
    source_type: str = "automation",
    snapshot_reader: Callable[[str], tuple[dict[str, Any], int]] = ha_read.execute_safely,
    service_caller: Callable[[ha_read.AdapterConfig, str, str], None] = post_tts,
) -> dict[str, object]:
    message = render_operational_message(
        display_name, phase, cause_code=cause_code, action_code=action_code,
        duration_seconds=duration_seconds,
        detected_was_announced=detected_was_announced,
        agent_recovered=agent_recovered,
        source_type=source_type,
    )
    snapshot, exit_code = snapshot_reader("snapshot")
    if exit_code != 0:
        raise NotifyError("speaker snapshot is unavailable")
    speaker = choose_speaker(snapshot, required_speaker=FALLBACK_SPEAKER)
    if live:
        service_caller(ha_read.load_config(), speaker, message)
    return {
        "schema_version": 1,
        "ok": True,
        "mode": "live" if live else "dry_run",
        "status": "accepted" if live else "planned",
        "speaker_entity_id": speaker,
        "phase": phase,
        "message": message,
        "service_calls": 1 if live else 0,
    }
