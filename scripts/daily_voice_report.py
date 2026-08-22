#!/usr/bin/env python3
"""Speak one deterministic Home Butler status report every day at 13:00."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import sqlite3
import stat
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_notify as ha_notify  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402
import diagnostic_monitor  # noqa: E402
import incident_status  # noqa: E402
import incident_timeline  # noqa: E402
import model_ha_proof  # noqa: E402
import heartbeat  # noqa: E402
from ollama_endpoint import (  # noqa: E402
    EndpointConfigError,
    load_runtime_ollama_endpoint,
)


DEFAULT_INVENTORY_PATH = Path(
    "/home/homebutler/.local/state/home-butler/incidents/inventory.json"
)
DEFAULT_INCIDENT_PATH = DEFAULT_INVENTORY_PATH.with_name("incidents.sqlite3")
DEFAULT_STATUS_PATH = Path(
    "/home/homebutler/.local/state/home-butler/daily-report-status.json"
)
MAX_INVENTORY_BYTES = 8 * 1_048_576
MAX_INVENTORY_ENTITIES = 4_096
DEVICE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
PHYSICAL_DEVICE_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
UNAVAILABLE_KINDS = {"unavailable", "absent"}
DAILY_REPORT_SPEAKER = ha_notify.FALLBACK_SPEAKER
SPEAKER_VERIFY_SECONDS = 12
SPEAKER_VERIFY_INTERVAL_SECONDS = 1.0


class DailyReportError(RuntimeError):
    """A fixed, secret-free daily report failure."""


def _load_inventory(path: Path = DEFAULT_INVENTORY_PATH) -> dict[str, Any]:
    try:
        metadata = path.stat()
    except OSError as error:
        raise DailyReportError("private inventory is unavailable") from error
    expected_owners = {os.geteuid()}
    if path == DEFAULT_INVENTORY_PATH:
        try:
            expected_owners.add(pwd.getpwnam("homebutler").pw_uid)
        except KeyError:
            pass
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid not in expected_owners
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or not 1 <= metadata.st_size <= MAX_INVENTORY_BYTES
    ):
        raise DailyReportError("private inventory is unsafe")
    try:
        document = ha_read.strict_json_loads(path.read_bytes())
    except (OSError, ha_read.AdapterError) as error:
        raise DailyReportError("private inventory is invalid") from error
    if (
        not isinstance(document, dict)
        or document.get("schema_version") not in {1, 2}
    ):
        raise DailyReportError("private inventory is invalid")
    entities = document.get("entities")
    if not isinstance(entities, list) or len(entities) > MAX_INVENTORY_ENTITIES:
        raise DailyReportError("private inventory is invalid")
    return document


def device_availability_counts(
    inventory: dict[str, Any], snapshot: dict[str, Any]
) -> tuple[int, int]:
    """Classify actual HA registry devices from their freshly read entities."""

    inventory_entities = inventory.get("entities")
    snapshot_entities = snapshot.get("entities")
    if not isinstance(inventory_entities, list) or not isinstance(snapshot_entities, list):
        raise DailyReportError("device data is unavailable")
    current_states: dict[str, str] = {}
    for item in snapshot_entities:
        if not isinstance(item, dict):
            continue
        entity_id = item.get("entity_id")
        state_kind = item.get("state_kind")
        if isinstance(entity_id, str) and isinstance(state_kind, str):
            current_states[entity_id] = state_kind

    devices: dict[str, list[str]] = {}
    seen_entities: set[str] = set()
    for item in inventory_entities:
        if not isinstance(item, dict):
            raise DailyReportError("device data is invalid")
        entity_id = item.get("entity_id")
        device_id = item.get("device_id")
        physical_hash = item.get("physical_device_hash")
        if not isinstance(entity_id, str):
            raise DailyReportError("device data is invalid")
        try:
            ha_read._validate_entity_id(entity_id)
        except ha_read.AdapterError as error:
            raise DailyReportError("device data is invalid") from error
        if entity_id in seen_entities:
            raise DailyReportError("device data is ambiguous")
        seen_entities.add(entity_id)
        if device_id is None:
            continue
        if not isinstance(device_id, str) or not DEVICE_ID_RE.fullmatch(device_id):
            raise DailyReportError("device data is invalid")
        if physical_hash is not None and (
            not isinstance(physical_hash, str)
            or not PHYSICAL_DEVICE_HASH_RE.fullmatch(physical_hash)
        ):
            raise DailyReportError("device data is invalid")
        devices.setdefault(physical_hash or device_id, []).append(
            current_states.get(entity_id, "absent")
        )
    if not devices:
        raise DailyReportError("no Home Assistant devices are mapped")
    available = sum(
        any(state not in UNAVAILABLE_KINDS for state in states)
        for states in devices.values()
    )
    unavailable = len(devices) - available
    return available, unavailable


def _read_cpu_totals(path: Path = Path("/proc/stat")) -> tuple[int, int]:
    try:
        first = path.read_text(encoding="ascii").splitlines()[0].split()
        values = [int(value) for value in first[1:9]]
    except (OSError, IndexError, ValueError) as error:
        raise DailyReportError("CPU data is unavailable") from error
    if first[0] != "cpu" or len(values) != 8 or any(value < 0 for value in values):
        raise DailyReportError("CPU data is invalid")
    total = sum(values)
    idle = values[3] + values[4]
    return total, idle


def current_cpu_percent(
    *,
    reader: Callable[[], tuple[int, int]] = _read_cpu_totals,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    total_before, idle_before = reader()
    sleeper(0.2)
    total_after, idle_after = reader()
    total_delta = total_after - total_before
    idle_delta = idle_after - idle_before
    if total_delta <= 0 or idle_delta < 0 or idle_delta > total_delta:
        raise DailyReportError("CPU sample is invalid")
    return max(0, min(100, round(100 * (total_delta - idle_delta) / total_delta)))


def memory_percent(path: Path = Path("/proc/meminfo")) -> int:
    try:
        values = {}
        for line in path.read_text(encoding="ascii").splitlines():
            key, separator, remainder = line.partition(":")
            if separator and key in {"MemTotal", "MemAvailable"}:
                values[key] = int(remainder.strip().split()[0])
        total = values["MemTotal"]
        available = values["MemAvailable"]
    except (OSError, KeyError, IndexError, ValueError) as error:
        raise DailyReportError("memory data is unavailable") from error
    if total <= 0 or not 0 <= available <= total:
        raise DailyReportError("memory data is invalid")
    return max(0, min(100, round(100 * (total - available) / total)))


def uptime_seconds(path: Path = Path("/proc/uptime")) -> int:
    try:
        value = float(path.read_text(encoding="ascii").split()[0])
    except (OSError, IndexError, ValueError) as error:
        raise DailyReportError("server uptime is unavailable") from error
    if value < 0 or value > 10 * 365 * 24 * 60 * 60:
        raise DailyReportError("server uptime is invalid")
    return int(value)


def _plural(value: int, forms: tuple[str, str, str]) -> str:
    if value % 100 in range(11, 15):
        return forms[2]
    if value % 10 == 1:
        return forms[0]
    if value % 10 in range(2, 5):
        return forms[1]
    return forms[2]


def format_uptime(seconds: int) -> str:
    minutes = max(0, seconds // 60)
    days, remainder = divmod(minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    if days:
        parts = [f"{days} {_plural(days, ('день', 'дня', 'дней'))}"]
        if hours:
            parts.append(f"{hours} {_plural(hours, ('час', 'часа', 'часов'))}")
        return " ".join(parts)
    if hours:
        parts = [f"{hours} {_plural(hours, ('час', 'часа', 'часов'))}"]
        if minutes:
            parts.append(f"{minutes} {_plural(minutes, ('минута', 'минуты', 'минут'))}")
        return " ".join(parts)
    if minutes:
        return f"{minutes} {_plural(minutes, ('минута', 'минуты', 'минут'))}"
    return "меньше минуты"


def _device_phrase(value: int) -> str:
    return f"{value} {_plural(value, ('устройство', 'устройства', 'устройств'))}"


def _cause_phrase(cause_code: str) -> str:
    return {
        "yandex_cloud_unreachable": "нет связи с облаком Яндекса",
        "dns_resolution_failed": "ошибка DNS",
        "upstream_timeout": "облако не ответило вовремя",
        "automation_action_failed": "ошибка автоматизации",
        "command_not_confirmed": "команда не подтвердилась",
        "device_not_observed_on_lan": "пропало из сети",
        "confirmed_ip_change": "устройство сменило адрес",
        "tuya_integration_unavailable": "интеграция Tuya была недоступна",
        "stale_entity_data": "данные слишком долго не обновлялись",
        "home_assistant_unreachable": "был недоступен",
        "integration_not_loaded": "интеграция не была загружена",
        "integration_unavailable": "интеграция была недоступна",
        "partial_entity_unavailable": "часть функций была недоступна",
        "tls_failure": "ошибка защищённого соединения",
        "unknown": "причина не подтверждена",
    }.get(cause_code, "технический сбой")


def _duration_phrase(seconds: int) -> str:
    if not isinstance(seconds, int) or not 0 <= seconds <= 7 * 86400:
        raise DailyReportError("incident duration is invalid")
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"около {minutes} {_plural(minutes, ('минуты', 'минут', 'минут'))}"
    hours = max(1, round(minutes / 60))
    return f"около {hours} {_plural(hours, ('часа', 'часов', 'часов'))}"


def _recovery_phrase(item: dict[str, Any]) -> str:
    mode = str(item.get("recovery_mode"))
    action = str(item.get("recovery_action_code", "none"))
    if mode == "self":
        return "восстановилось само"
    if mode == "unresolved":
        return "ещё неисправно"
    if mode != "agent":
        raise DailyReportError("incident recovery is invalid")
    return {
        "retry_original_intent_once": "я один раз повторил команду и проверил результат",
        "repair_helper_state": "я исправил состояние сценария",
        "reload_yandex_entry_once": "я перезагрузил интеграцию Яндекса",
        "reload_integration_entry_once": "я перечитал интеграцию и проверил её",
        "reload_local_integration_once": "я перечитал локальную интеграцию и проверил её",
        "homeassistant.reload_config_entry": "я перезагрузил запись интеграции",
        "localtuya.reload": "я перезагрузил LocalTuya",
        "homeassistant.restart": "я перезапустил Home Assistant и проверил его",
        "out_of_band_restart": "я восстановил Home Assistant через резервный канал",
        "none": "я восстановил и проверил результат",
    }.get(action, "я безопасно восстановил и проверил результат")


def _significant_incidents(details: Any) -> list[dict[str, Any]]:
    """Return every event that materially affected service or needed action."""

    if not isinstance(details, list):
        raise DailyReportError("incident report is invalid")
    selected: list[dict[str, Any]] = []
    for item in details:
        if not isinstance(item, dict):
            raise DailyReportError("incident report is invalid")
        duration = item.get("duration_seconds")
        occurrences = item.get("occurrences", 1)
        recovery = item.get("recovery_mode")
        announced = item.get("announced")
        if (
            not isinstance(duration, int)
            or isinstance(duration, bool)
            or not 0 <= duration <= 7 * 86400
            or not isinstance(occurrences, int)
            or isinstance(occurrences, bool)
            or occurrences < 1
            or recovery not in {"agent", "self", "unresolved"}
            or announced is not None and not isinstance(announced, bool)
        ):
            raise DailyReportError("incident report is invalid")
        if announced is not None:
            significant = announced or recovery == "agent"
        else:
            significant = (
                recovery in {"agent", "unresolved"}
                or duration >= 120
                or occurrences > 1
            )
        if significant:
            selected.append(item)
    return selected


def render_message(facts: dict[str, Any]) -> str:
    incident_details_prefix = ""
    message = (
        "Отчёт. Home Assistant на связи. "
        f"Доступно {_device_phrase(facts['available_devices'])}, "
        f"недоступно {_device_phrase(facts['unavailable_devices'])}. "
    )
    total = facts.get("incidents_total")
    if isinstance(total, int):
        if total == 0:
            message += "За последние сутки подтверждённых сбоев не было. "
        else:
            message += (
                f"За сутки: сбоев {total}, я восстановил "
                f"{facts['agent_recovered']}, сами {facts['self_recovered']}, "
                f"открыто {facts['unresolved_incidents']}. "
            )
            details = _significant_incidents(facts.get("incident_details", []))
            if details:
                rendered: list[str] = []
                for item in details:
                    name = " ".join(str(item.get("display_name", "")).split())
                    cause = _cause_phrase(str(item.get("cause_code", "unknown")))
                    duration = _duration_phrase(
                        int(item.get("duration_seconds", 0))
                    )
                    recovery = _recovery_phrase(item)
                    if not name:
                        raise DailyReportError("incident report is invalid")
                    rendered.append(
                        f"{name}: {cause}, {duration}, {recovery}"
                    )
                incident_details_prefix = "События: " + "; ".join(rendered) + ". "
                message += incident_details_prefix
    diagnostic_count = facts.get("diagnostic_alert_count")
    if isinstance(diagnostic_count, int) and diagnostic_count >= 0:
        message += (
            "Предупреждений по ошибкам и расходникам нет. "
            if diagnostic_count == 0
            else f"Активных предупреждений по ошибкам и расходникам: {diagnostic_count}. "
        )
    elif diagnostic_count == -1:
        message += "Проверка ошибок и расходников сейчас недоступна. "
    model_status = facts.get("model_status")
    if model_status == "loaded":
        mode = {
            "gpu": "полностью на GPU",
            "mixed": "в смешанном режиме",
            "cpu": "на CPU",
        }.get(facts.get("model_accelerator"), "в неизвестном режиме")
        message += f"Модель {mode}. "
    elif model_status == "unloaded":
        message += "Модель на связи, но выгружена из памяти. "
    elif model_status == "unavailable":
        message += "Модель недоступна. "
    message += (
        f"Сервер работает {format_uptime(facts['uptime_seconds'])}; "
        f"нагрузка процессора {facts['cpu_percent']} процентов, "
        f"памяти {facts['memory_percent']} процентов."
    )
    if len(message) > ha_notify.MAX_MESSAGE_CHARS and incident_details_prefix:
        message = message.replace(
            incident_details_prefix,
            "Подробности событий сохранены в журнале. ",
            1,
        )
    if len(message) > ha_notify.MAX_MESSAGE_CHARS:
        raise DailyReportError("daily report is too long")
    return message


def choose_daily_report_speaker(snapshot: dict[str, Any]) -> str:
    """Use only the owner's fixed Yandex Station Max for the daily report."""

    entities = snapshot.get("entities")
    if not isinstance(entities, list):
        raise DailyReportError("daily report speaker is unavailable")
    for item in entities:
        if not isinstance(item, dict) or item.get("entity_id") != DAILY_REPORT_SPEAKER:
            continue
        if item.get("state_kind") in {None, "unavailable", "redacted", "absent"}:
            break
        return DAILY_REPORT_SPEAKER
    raise DailyReportError("daily report speaker is unavailable")


def read_speaker_state(
    config: ha_read.AdapterConfig,
    speaker: str,
) -> dict[str, Any]:
    """Read only the fixed speaker fields needed for playback verification."""

    if speaker != DAILY_REPORT_SPEAKER:
        raise DailyReportError("daily report speaker is unavailable")
    states = ha_read.request_json(config, "/api/states")
    if not isinstance(states, list):
        raise DailyReportError("daily report speaker is unavailable")
    for item in states:
        if not isinstance(item, dict) or item.get("entity_id") != speaker:
            continue
        attributes = item.get("attributes")
        state = item.get("state")
        updated = item.get("last_updated")
        if (
            not isinstance(attributes, dict)
            or not isinstance(state, str)
            or state in {"unknown", "unavailable"}
            or not isinstance(updated, str)
            or not updated
        ):
            break
        volume = attributes.get("volume_level")
        muted = attributes.get("is_volume_muted")
        return {
            "state": state,
            "last_updated": updated,
            "volume_ready": (
                not isinstance(volume, bool)
                and isinstance(volume, (int, float))
                and volume >= 0.05
            ),
            "muted": muted is True,
        }
    raise DailyReportError("daily report speaker is unavailable")


def verify_speaker_transition(
    config: ha_read.AdapterConfig,
    speaker: str,
    baseline: dict[str, Any],
    *,
    state_reader: Callable[[ha_read.AdapterConfig, str], dict[str, Any]] = read_speaker_state,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    wait_seconds: float = SPEAKER_VERIFY_SECONDS,
) -> bool:
    """Require HA to observe a real state update from the target station."""

    deadline = clock() + wait_seconds
    while True:
        current = state_reader(config, speaker)
        if (
            current.get("last_updated") != baseline.get("last_updated")
            and current.get("volume_ready") is True
            and current.get("muted") is False
        ):
            return True
        if clock() >= deadline:
            return False
        sleeper(min(SPEAKER_VERIFY_INTERVAL_SECONDS, max(0.0, deadline - clock())))


def _status_path() -> Path:
    raw = os.environ.get("HOME_BUTLER_DAILY_REPORT_STATUS", "")
    return Path(raw) if raw else DEFAULT_STATUS_PATH


def _load_attempts(path: Path, local_date: str) -> int:
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return 0
    except OSError as error:
        raise DailyReportError("daily report status is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= 16 * 1024
    ):
        raise DailyReportError("daily report status is unsafe")
    try:
        document = ha_read.strict_json_loads(path.read_bytes())
    except (OSError, ha_read.AdapterError) as error:
        raise DailyReportError("daily report status is invalid") from error
    if not isinstance(document, dict):
        raise DailyReportError("daily report status is invalid")
    attempts = document.get("attempts")
    if (
        document.get("schema_version") != 2
        or not isinstance(document.get("local_date"), str)
        or not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or not 1 <= attempts <= 100
    ):
        raise DailyReportError("daily report status is invalid")
    return attempts if document["local_date"] == local_date else 0


def already_verified_today(
    path: Path | None = None,
    *,
    now: Callable[[], float] = time.time,
) -> bool:
    target = _status_path() if path is None else path
    try:
        metadata = target.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or not 1 <= metadata.st_size <= 16 * 1024
        ):
            return False
        document = ha_read.strict_json_loads(target.read_bytes())
    except (OSError, ha_read.AdapterError):
        return False
    observed = int(now())
    local_date = time.strftime("%Y-%m-%d", time.localtime(observed))
    return bool(
        isinstance(document, dict)
        and document.get("schema_version") == 2
        and document.get("local_date") == local_date
        and document.get("status") == "verified"
        and document.get("verified") is True
    )


def report_is_delayed(*, now: Callable[[], float] = time.time) -> bool:
    local = time.localtime(now())
    return local.tm_hour > 13 or (local.tm_hour == 13 and local.tm_min > 15)


def write_status(
    result: dict[str, Any],
    *,
    path: Path | None = None,
    now: Callable[[], float] = time.time,
) -> None:
    target = _status_path() if path is None else path
    heartbeat._validate_state_dir(target.parent)
    attempted_epoch = int(now())
    if attempted_epoch < 0:
        raise DailyReportError("daily report clock is invalid")
    local_date = time.strftime("%Y-%m-%d", time.localtime(attempted_epoch))
    attempts = _load_attempts(target, local_date) + 1
    message = result.get("message")
    message_hash = (
        hashlib.sha256(message.encode("utf-8")).hexdigest()
        if isinstance(message, str)
        else None
    )
    document = {
        "schema_version": 2,
        "local_date": local_date,
        "attempted_epoch": attempted_epoch,
        "attempts": attempts,
        "status": result.get("status", "not_sent"),
        "verified": result.get("status") == "verified",
        "verification": result.get("verification", "not_sent"),
        "service_calls": result.get("service_calls", 0),
        "message_sha256": message_hash,
    }
    heartbeat._atomic_write(
        target,
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("ascii")
        + b"\n",
    )


def _timeline(path: Path = DEFAULT_INCIDENT_PATH) -> dict[str, object]:
    owner_uid = incident_status._expected_uid()
    incident_status._validate_path(path, owner_uid)
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            return incident_timeline.collect(connection, now=int(time.time()))
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise DailyReportError("incident timeline is unavailable") from error


def _model_resources() -> dict[str, str]:
    try:
        endpoint = load_runtime_ollama_endpoint()
        document = model_ha_proof.get_ollama(endpoint, "/api/ps")
    except (EndpointConfigError, model_ha_proof.ProofError):
        return {"model_status": "unavailable", "model_accelerator": "unknown"}
    try:
        evidence = model_ha_proof.gpu_evidence(document)
    except model_ha_proof.ProofError:
        return {
            "model_status": "unloaded",
            "model_accelerator": "gpu" if endpoint.host != "127.0.0.1" else "cpu",
        }
    if endpoint.host == "127.0.0.1":
        accelerator = "cpu"
    elif evidence["fully_on_gpu"]:
        accelerator = "gpu"
    else:
        accelerator = "mixed"
    return {"model_status": "loaded", "model_accelerator": accelerator}


def _diagnostic_alert_count() -> int:
    try:
        document = diagnostic_monitor._load_private(diagnostic_monitor.state_path())
    except diagnostic_monitor.MonitorError:
        return -1
    value = document.get("active_alert_count")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else -1


def build_report(
    *,
    inventory_loader: Callable[[], dict[str, Any]] = _load_inventory,
    snapshot_reader: Callable[[str], tuple[dict[str, Any], int]] = ha_read.execute_safely,
    cpu_reader: Callable[[], int] = current_cpu_percent,
    memory_reader: Callable[[], int] = memory_percent,
    uptime_reader: Callable[[], int] = uptime_seconds,
    timeline_reader: Callable[[], dict[str, object]] = _timeline,
    resource_reader: Callable[[], dict[str, str]] = _model_resources,
    diagnostic_reader: Callable[[], int] = _diagnostic_alert_count,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    snapshot, exit_code = snapshot_reader("snapshot")
    if exit_code != 0 or snapshot.get("status") not in {"healthy", "stale_data"}:
        raise DailyReportError("Home Assistant snapshot is unavailable")
    available, unavailable = device_availability_counts(
        inventory_loader(), snapshot
    )
    timeline = timeline_reader()
    summary = timeline.get("summary") if isinstance(timeline, dict) else None
    incident_details = timeline.get("incidents") if isinstance(timeline, dict) else None
    if not isinstance(summary, dict) or not isinstance(incident_details, list):
        raise DailyReportError("incident timeline is invalid")
    significant_details = _significant_incidents(incident_details)
    resources = resource_reader()
    if (
        resources.get("model_status") not in {"loaded", "unloaded", "unavailable"}
        or resources.get("model_accelerator") not in {"gpu", "mixed", "cpu", "unknown"}
    ):
        raise DailyReportError("model resource status is invalid")
    facts: dict[str, Any] = {
        "available_devices": available,
        "unavailable_devices": unavailable,
        "uptime_seconds": uptime_reader(),
        "cpu_percent": cpu_reader(),
        "memory_percent": memory_reader(),
        "incidents_total": len(significant_details),
        "agent_recovered": sum(
            item.get("recovery_mode") == "agent" for item in significant_details
        ),
        "self_recovered": sum(
            item.get("recovery_mode") == "self" for item in significant_details
        ),
        "unresolved_incidents": sum(
            item.get("recovery_mode") == "unresolved"
            for item in significant_details
        ),
        "incident_details": significant_details,
        "diagnostic_alert_count": diagnostic_reader(),
        **resources,
    }
    return facts, snapshot, render_message(facts)


def execute(
    *,
    live: bool,
    report_builder: Callable[[], tuple[dict[str, int], dict[str, Any], str]] = build_report,
    config_loader: Callable[[], ha_read.AdapterConfig] = ha_read.load_config,
    service_caller: Callable[[ha_read.AdapterConfig, str, str], None] = ha_notify.post_tts,
    state_reader: Callable[[ha_read.AdapterConfig, str], dict[str, Any]] = read_speaker_state,
    delivery_verifier: Callable[
        [ha_read.AdapterConfig, str, dict[str, Any]], bool
    ] = verify_speaker_transition,
    delayed: bool = False,
) -> dict[str, Any]:
    facts, snapshot, message = report_builder()
    if delayed:
        message = "Запоздавший ежедневный отчёт после включения компьютера. " + message
    # The general snapshot intentionally omits media-player details.  The
    # fixed Station Max is validated immediately below through the narrow,
    # live read_speaker_state boundary instead.
    speaker = DAILY_REPORT_SPEAKER
    verified = False
    if live:
        config = config_loader()
        baseline = state_reader(config, speaker)
        if baseline.get("muted") is True or baseline.get("volume_ready") is not True:
            raise DailyReportError("daily report speaker is not audible")
        service_caller(config, speaker, message)
        verified = delivery_verifier(config, speaker, baseline)
    return {
        "schema_version": 1,
        "ok": not live or verified,
        "mode": "live" if live else "dry_run",
        "status": (
            "verified" if verified else "accepted_unverified" if live else "planned"
        ),
        **facts,
        "message": message,
        "service": "tts.yandex_station_say",
        "service_calls": 1 if live else 0,
        "verification": (
            "ha_speaker_state_transition_observed"
            if verified
            else "ha_service_accepted_without_speaker_transition"
            if live
            else "not_sent"
        ),
        "delayed": delayed,
    }


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.live and already_verified_today():
        print('{"schema_version":1,"ok":true,"status":"already_verified"}')
        return 0
    try:
        result = execute(live=arguments.live, delayed=report_is_delayed())
        exit_code = 0 if result["ok"] else 4
    except ha_notify.NotifyDeliveryUnknown:
        result = {"schema_version": 1, "ok": False, "status": "delivery_unknown"}
        exit_code = 4
    except (DailyReportError, ha_notify.NotifyError, ha_read.AdapterError) as error:
        result = {
            "schema_version": 1,
            "ok": False,
            "status": "not_sent",
            "error_code": str(error),
        }
        exit_code = 3
    try:
        write_status(result)
    except (DailyReportError, OSError):
        result = {"schema_version": 1, "ok": False, "status": "not_sent"}
        exit_code = 3
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run())
