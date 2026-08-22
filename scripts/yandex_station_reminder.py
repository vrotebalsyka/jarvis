#!/usr/bin/env python3
"""Fail-closed reminder creation through one fixed Yandex Station Max."""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import socket
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_notify as ha_notify  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402
import model_workspace  # noqa: E402


STATION_MAX = "media_player.yandex_station_x10x2a000qpm2b"
SERVICE_PATH = "/api/services/yandex_station/send_command?return_response"
TIMEZONE = ZoneInfo("Asia/Yekaterinburg")
CONFIRMATION = "Напоминание установлено."
ACTIVE_GOAL_PATH = "notes/ACTIVE-GOAL.json"
LAST_REMINDER_PATH = "notes/LAST-REMINDER.json"
MAX_REQUEST_CHARS = 500
MAX_REMINDER_TEXT_CHARS = 180
MAX_COMMAND_CHARS = 320
MAX_RESPONSE_BYTES = 1_048_576
REQUEST_TIMEOUT_SECONDS = 12
SUCCESS_STATUSES = {"OK", "SUCCESS"}
FAILURE_MARKERS = (
    "не могу", "не удалось", "не поняла", "не понял", "уточни", "уточните",
    "ошибка", "извините", "что-то пошло не так",
)
UNSAFE_TEXT_RE = re.compile(
    r"(?:https?://|\bbearer\b|\btoken\b|\bsecret\b|\bпарол\S*\b|"
    r"\bтокен\S*\b|\bentity_id\b|\b(?:curl|powershell|bash|cmd\.exe)\b)",
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r"(?:\b(?:на|в)\s*)?(?P<hour>[01]?\d|2[0-3])[.:](?P<minute>[0-5]\d)\b"
)
WEEKDAYS = {
    "понедельник": 0,
    "понедельника": 0,
    "вторник": 1,
    "вторника": 1,
    "среду": 2,
    "среда": 2,
    "четверг": 3,
    "четверга": 3,
    "пятницу": 4,
    "пятница": 4,
    "субботу": 5,
    "суббота": 5,
    "воскресенье": 6,
    "воскресенья": 6,
}
MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


class ReminderError(RuntimeError):
    """A safe reminder failure that contains no secrets."""


class ReminderDeliveryUnknown(ReminderError):
    """The reminder request may have reached the Station and must not retry."""


class ReminderRejected(ReminderError):
    """The Station returned an explicit negative or malformed response."""


@dataclass(frozen=True)
class ReminderRequest:
    due_at: datetime
    text: str
    command: str
    fingerprint: str


def _normalize_request(value: object) -> str:
    if not isinstance(value, str):
        raise ReminderError("текст напоминания не распознан")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if (
        not 1 <= len(normalized) <= MAX_REQUEST_CHARS
        or any(ord(character) < 32 for character in normalized)
        or UNSAFE_TEXT_RE.search(normalized)
    ):
        raise ReminderError("текст напоминания небезопасен или слишком длинный")
    return normalized


def _due_from_weekday(text: str, now: datetime, hour: int, minute: int) -> datetime:
    folded = text.casefold()
    if "сегодня" in folded:
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due <= now:
            raise ReminderError("указанное время сегодня уже прошло")
        return due
    if "завтра" in folded:
        return (now + timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
    matches = {
        weekday for name, weekday in WEEKDAYS.items()
        if re.search(rf"\b{re.escape(name)}\b", folded)
    }
    if len(matches) != 1:
        raise ReminderError("укажите один день недели, сегодня или завтра")
    weekday = next(iter(matches))
    days = (weekday - now.weekday()) % 7
    due = (now + timedelta(days=days)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if due <= now:
        due += timedelta(days=7)
    return due


def _extract_text(normalized: str, time_match: re.Match[str]) -> str:
    folded = normalized.casefold()
    marker = re.search(r"\bчтобы\b", folded)
    if marker is None:
        raise ReminderError("после слова «чтобы» укажите, о чём напомнить")
    text = normalized[marker.end():]
    text = re.split(
        r"\b(?:через\s+яндекс\s+алис\S*|как\s+напоминание\s+постав\S*)\b",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    text = text.strip(" \t.,;:—-\"'«»")
    if time_match.start() > marker.end() and time_match.group(0) in text:
        text = text.replace(time_match.group(0), "", 1).strip(" \t.,;:—-")
    if (
        not 3 <= len(text) <= MAX_REMINDER_TEXT_CHARS
        or any(ord(character) < 32 for character in text)
        or UNSAFE_TEXT_RE.search(text)
    ):
        raise ReminderError("текст после «чтобы» пустой, небезопасный или слишком длинный")
    return text


def parse_request(value: object, *, now: datetime | None = None) -> ReminderRequest:
    normalized = _normalize_request(value)
    time_matches = list(TIME_RE.finditer(normalized))
    if len(time_matches) != 1:
        raise ReminderError("укажите одно точное время в формате 7:10")
    match = time_matches[0]
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    current = (now or datetime.now(TIMEZONE)).astimezone(TIMEZONE)
    due = _due_from_weekday(normalized, current, hour, minute)
    reminder_text = _extract_text(normalized, match)
    command = (
        f"Поставь напоминание на {due.day} {MONTHS[due.month - 1]} "
        f"{due.year} года в {due.hour} часов {due.minute} минут: {reminder_text}."
    )
    if len(command) > MAX_COMMAND_CHARS:
        raise ReminderError("команда напоминания слишком длинная")
    seed = json.dumps(
        {
            "due_at": due.isoformat(timespec="minutes"),
            "station": STATION_MAX,
            "text": reminder_text.casefold(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    fingerprint = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return ReminderRequest(due, reminder_text, command, fingerprint)


def _connection(config: ha_read.AdapterConfig) -> http.client.HTTPConnection:
    return http.client.HTTPConnection(
        config.host, config.port, timeout=REQUEST_TIMEOUT_SECONDS
    )


def post_reminder_command(
    config: ha_read.AdapterConfig,
    command: str,
    *,
    connection_factory: Callable[[ha_read.AdapterConfig], http.client.HTTPConnection] = _connection,
) -> dict[str, object]:
    if (
        not isinstance(command, str)
        or not 1 <= len(command) <= MAX_COMMAND_CHARS
        or not command.startswith("Поставь напоминание на ")
        or any(ord(character) < 32 for character in command)
        or UNSAFE_TEXT_RE.search(command)
    ):
        raise ReminderError("команда напоминания отклонена")
    body = json.dumps(
        {"entity_id": STATION_MAX, "command": "sendText", "text": command},
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
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ReminderDeliveryUnknown("ответ Home Assistant слишком большой")
        if response.status != 200:
            raise ReminderRejected("Home Assistant отклонил команду напоминания")
        document = ha_read.strict_json_loads(raw)
        if not isinstance(document, dict):
            raise ReminderDeliveryUnknown("Home Assistant не вернул проверяемый ответ")
        service_response = document.get("service_response")
        if not isinstance(service_response, dict):
            raise ReminderDeliveryUnknown("Станция не вернула подтверждение команды")
        error = service_response.get("error")
        if error not in {None, ""}:
            raise ReminderRejected("Станция отклонила команду напоминания")
        status = service_response.get("status")
        if not isinstance(status, str) or status.upper() not in SUCCESS_STATUSES:
            raise ReminderRejected("Станция не подтвердила команду напоминания")
        station_text = service_response.get("text")
        if isinstance(station_text, str):
            safe_text = " ".join(station_text.split())[:300]
            if any(marker in safe_text.casefold() for marker in FAILURE_MARKERS):
                raise ReminderRejected("Алиса сообщила, что напоминание не создано")
            response_text_present = bool(safe_text)
        else:
            response_text_present = False
        return {
            "acknowledged": True,
            "station_status": status.upper(),
            "response_text_present": response_text_present,
        }
    except (ReminderDeliveryUnknown, ReminderRejected):
        raise
    except (OSError, socket.timeout, TimeoutError, http.client.HTTPException) as error:
        if request_sent:
            raise ReminderDeliveryUnknown(
                "доставка команды Станции не подтверждена"
            ) from error
        raise ReminderError("Home Assistant недоступен для напоминания") from error
    finally:
        try:
            connection.close()
        except (UnboundLocalError, OSError, http.client.HTTPException):
            pass


def _record(
    request: ReminderRequest,
    *,
    status: str,
    requested_at_epoch: int,
    actions_performed: int,
    reminder_created: bool,
    voice_confirmation_sent: bool,
    blocker: str | None = None,
    station_status: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "goal_kind": "create_yandex_alice_reminder",
        "status": status,
        "requested_at_epoch": requested_at_epoch,
        "due_at": request.due_at.isoformat(timespec="minutes"),
        "timezone": str(TIMEZONE),
        "reminder_text": request.text,
        "station": "station_max",
        "fingerprint": request.fingerprint,
        "blocker": blocker,
        "actions_performed": actions_performed,
        "reminder_created": reminder_created,
        "voice_confirmation_sent": voice_confirmation_sent,
        "station_status": station_status,
        "automatic_retry_allowed": False,
    }


def _write_record(
    record: dict[str, object],
    writer: Callable[[object, object], dict[str, Any]],
) -> None:
    content = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    writer(LAST_REMINDER_PATH, content)
    writer(ACTIVE_GOAL_PATH, content)


def _existing_record(
    reader: Callable[[object], dict[str, Any]],
) -> dict[str, Any] | None:
    try:
        result = reader(LAST_REMINDER_PATH)
        content = result.get("content") if isinstance(result, dict) else None
        parsed = json.loads(content) if isinstance(content, str) else None
        return parsed if isinstance(parsed, dict) else None
    except (model_workspace.WorkspaceError, json.JSONDecodeError, TypeError, ValueError):
        return None


def create_reminder(
    question: str,
    *,
    now: datetime | None = None,
    observed_epoch: int | None = None,
    config_loader: Callable[[], ha_read.AdapterConfig] = ha_read.load_config,
    snapshot_reader: Callable[[str], tuple[dict[str, Any], int]] = ha_read.execute_safely,
    command_caller: Callable[[ha_read.AdapterConfig, str], dict[str, object]] = post_reminder_command,
    tts_caller: Callable[[ha_read.AdapterConfig, str, str], None] = ha_notify.post_tts,
    workspace_reader: Callable[[object], dict[str, Any]] = model_workspace.read_text,
    workspace_writer: Callable[[object, object], dict[str, Any]] = model_workspace.write_text,
) -> str:
    timestamp = int(datetime.now(TIMEZONE).timestamp()) if observed_epoch is None else observed_epoch
    if not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp < 0:
        raise ReminderError("время запроса напоминания некорректно")
    try:
        request = parse_request(question, now=now)
    except ReminderError as error:
        return f"Напоминание не установлено: {error}."

    existing = _existing_record(workspace_reader)
    if existing is not None and existing.get("fingerprint") == request.fingerprint:
        status = existing.get("status")
        if status == "completed":
            return "Это напоминание уже установлено; повторно его не создавал."
        if status in {"dispatching", "delivery_unknown", "command_acknowledged"}:
            return (
                "Повторно не отправляю: предыдущая команда могла уже создать это "
                "напоминание, а безопасного readback у Станции нет."
            )

    prepared = _record(
        request,
        status="dispatching",
        requested_at_epoch=timestamp,
        actions_performed=0,
        reminder_created=False,
        voice_confirmation_sent=False,
    )
    try:
        _write_record(prepared, workspace_writer)
    except model_workspace.WorkspaceError:
        return (
            "Напоминание не установлено: не удалось безопасно записать защиту "
            "от повторной отправки."
        )

    snapshot, exit_code = snapshot_reader("snapshot")
    try:
        speaker = ha_notify.choose_speaker(
            snapshot if exit_code == 0 else {}, required_speaker=STATION_MAX
        )
        config = config_loader()
    except (ha_notify.NotifyError, ha_read.AdapterError):
        failed = dict(prepared)
        failed.update(status="blocked", blocker="station_max_or_home_assistant_unavailable")
        _write_record(failed, workspace_writer)
        return "Напоминание не установлено: Станция Макс или Home Assistant недоступны."

    try:
        result = command_caller(config, request.command)
    except ReminderDeliveryUnknown:
        unknown = _record(
            request,
            status="delivery_unknown",
            requested_at_epoch=timestamp,
            actions_performed=1,
            reminder_created=False,
            voice_confirmation_sent=False,
            blocker="station_delivery_unknown_no_retry",
        )
        _write_record(unknown, workspace_writer)
        return (
            "Команду напоминания отправил, но Станция не подтвердила результат. "
            "Автоматически не повторяю, чтобы не создать дубль."
        )
    except ReminderRejected as error:
        rejected = _record(
            request,
            status="blocked",
            requested_at_epoch=timestamp,
            actions_performed=1,
            reminder_created=False,
            voice_confirmation_sent=False,
            blocker="station_rejected_reminder",
        )
        _write_record(rejected, workspace_writer)
        return f"Напоминание не установлено: {error}."
    except ReminderError as error:
        failed = _record(
            request,
            status="blocked",
            requested_at_epoch=timestamp,
            actions_performed=0,
            reminder_created=False,
            voice_confirmation_sent=False,
            blocker="home_assistant_unavailable_before_dispatch",
        )
        _write_record(failed, workspace_writer)
        return f"Напоминание не установлено: {error}."

    acknowledged = result.get("acknowledged") is True
    station_status = result.get("station_status")
    if not acknowledged or not isinstance(station_status, str):
        unknown = _record(
            request,
            status="delivery_unknown",
            requested_at_epoch=timestamp,
            actions_performed=1,
            reminder_created=False,
            voice_confirmation_sent=False,
            blocker="station_acknowledgement_missing_no_retry",
        )
        _write_record(unknown, workspace_writer)
        return (
            "Команду напоминания отправил, но проверяемого ответа Станции нет. "
            "Повторно не отправляю."
        )

    acknowledged_record = _record(
        request,
        status="command_acknowledged",
        requested_at_epoch=timestamp,
        actions_performed=1,
        reminder_created=True,
        voice_confirmation_sent=False,
        station_status=station_status,
    )
    _write_record(acknowledged_record, workspace_writer)
    voice_sent = False
    try:
        tts_caller(config, speaker, CONFIRMATION)
        voice_sent = True
    except (ha_notify.NotifyError, ha_read.AdapterError):
        pass
    completed = _record(
        request,
        status="completed",
        requested_at_epoch=timestamp,
        actions_performed=2 if voice_sent else 1,
        reminder_created=True,
        voice_confirmation_sent=voice_sent,
        blocker=None if voice_sent else "voice_confirmation_not_verified",
        station_status=station_status,
    )
    _write_record(completed, workspace_writer)
    if voice_sent:
        return (
            f"Напоминание установлено на {request.due_at:%d.%m.%Y в %H:%M}. "
            "Станция Макс подтвердила команду; фразу «напоминание установлено» отправил."
        )
    return (
        f"Напоминание установлено на {request.due_at:%d.%m.%Y в %H:%M}, "
        "но голосовое подтверждение Станция не приняла."
    )
