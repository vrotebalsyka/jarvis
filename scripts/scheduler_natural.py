#!/usr/bin/env python3
"""Turn one natural Russian scheduling request into one validated TaskSpec change."""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo


sys.dont_write_bytecode = True
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import model_ha_proof  # noqa: E402
import model_runtime_policy  # noqa: E402
import persistent_scheduler as scheduler  # noqa: E402
from ollama_endpoint import load_runtime_ollama_endpoint  # noqa: E402


MAX_REQUEST_CHARS = 500
OPERATIONS = frozenset({"create", "update", "cancel", "list", "get", "clarify"})
MODEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "operation", "kind", "natural_description", "canonical_payload",
        "timezone", "next_run_local", "recurrence", "query", "clarification",
    ],
    "properties": {
        "operation": {"type": "string", "enum": sorted(OPERATIONS)},
        "kind": {"type": ["string", "null"], "enum": [*sorted(scheduler.TASK_KINDS), None]},
        "natural_description": {"type": ["string", "null"], "maxLength": 500},
        "canonical_payload": {
            "anyOf": [{"type": "null"}, scheduler.canonical_payload_tool_schema()]
        },
        "timezone": {"type": ["string", "null"], "maxLength": 64},
        "next_run_local": {"type": ["string", "null"], "maxLength": 64},
        "recurrence": {
            "anyOf": [{"type": "null"}, scheduler.recurrence_tool_schema()]
        },
        "query": {"type": ["string", "null"], "maxLength": 128},
        "clarification": {"type": ["string", "null"], "maxLength": 160},
    },
}


class NaturalScheduleError(RuntimeError):
    """A safe natural-time parsing failure."""


def _question(value: object) -> str:
    if not isinstance(value, str):
        raise NaturalScheduleError("запрос расписания не распознан")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if (
        not 1 <= len(normalized) <= MAX_REQUEST_CHARS
        or any(ord(character) < 32 for character in normalized)
    ):
        raise NaturalScheduleError("запрос расписания некорректен")
    return normalized


def _model_document(question: str, now: datetime) -> dict[str, Any]:
    prompt = (
        "Преобразуй запрос владельца в один JSON для локального планировщика. "
        "Не выполняй задачу и не придумывай отсутствующие дату, время или объект. "
        "Если действительно неоднозначно, operation=clarify и задай один короткий вопрос. "
        "Для обычного напоминания используй kind=local_reminder, payload {text}. "
        "Для ежедневного отчёта используй kind=daily_report, payload "
        "{report:home_status}. next_run_local — ISO 8601 с offset. "
        "Recurrence: none, daily/time, weekdays/time, weekly/time/weekday/interval_weeks "
        "или interval/seconds. Отмена и поиск используют query. "
        f"Текущее локальное время: {now.isoformat(timespec='seconds')}. "
        f"Timezone: {scheduler.DEFAULT_TIMEZONE}. Запрос: {question}"
    )
    endpoint = load_runtime_ollama_endpoint()
    profile = model_runtime_policy.get_profile("structured")
    response = model_ha_proof.call_ollama(
        endpoint,
        "/api/generate",
        model_runtime_policy.build_generate_payload(
            "structured", prompt, response_format=MODEL_SCHEMA
        ),
        timeout=profile.request_timeout_seconds,
    )
    content = response.get("response")
    if not isinstance(content, str) or not content:
        raise NaturalScheduleError("модель не вернула расписание")
    try:
        document = json.loads(content)
    except json.JSONDecodeError as error:
        raise NaturalScheduleError("модель вернула некорректное расписание") from error
    if not isinstance(document, dict):
        raise NaturalScheduleError("модель вернула некорректное расписание")
    return document


def validate_model_document(
    value: object,
    *,
    now: datetime,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(MODEL_SCHEMA["required"]):
        raise NaturalScheduleError("структура расписания некорректна")
    operation = value.get("operation")
    if operation not in OPERATIONS:
        raise NaturalScheduleError("операция расписания некорректна")
    if operation == "clarify":
        clarification = value.get("clarification")
        if not isinstance(clarification, str):
            raise NaturalScheduleError("уточнение расписания некорректно")
        question = " ".join(clarification.split())
        if not 3 <= len(question) <= 160 or question.count("?") > 1:
            raise NaturalScheduleError("уточнение расписания некорректно")
        return {"operation": "clarify", "clarification": question.rstrip(".") + "?"}
    query = value.get("query")
    if operation in {"cancel", "list", "get"}:
        if query is not None and not isinstance(query, str):
            raise NaturalScheduleError("название задачи некорректно")
        normalized_query = " ".join(query.split()) if isinstance(query, str) else None
        if operation in {"cancel", "get"} and not normalized_query:
            raise NaturalScheduleError("не указано название задачи")
        return {"operation": operation, "query": normalized_query}
    kind = value.get("kind")
    description = value.get("natural_description")
    payload = value.get("canonical_payload")
    timezone = value.get("timezone")
    next_run_local = value.get("next_run_local")
    recurrence = value.get("recurrence")
    if (
        kind not in scheduler.TASK_KINDS
        or not isinstance(description, str)
        or not isinstance(payload, dict)
        or not isinstance(timezone, str)
        or not isinstance(next_run_local, str)
        or not isinstance(recurrence, dict)
    ):
        raise NaturalScheduleError("задача расписания неполна")
    try:
        safe_payload = scheduler.validate_payload(kind, payload)
        safe_recurrence = scheduler.validate_recurrence(recurrence)
        zone_name = scheduler._safe_timezone(timezone)
        due = datetime.fromisoformat(next_run_local)
        if due.tzinfo is None:
            raise ValueError
        due = due.astimezone(ZoneInfo(zone_name))
    except (scheduler.SchedulerError, ValueError) as error:
        raise NaturalScheduleError("дата или параметры задачи некорректны") from error
    now_local = now.astimezone(ZoneInfo(zone_name))
    if due <= now_local:
        raise NaturalScheduleError("время задачи уже прошло")
    if operation == "update" and not isinstance(query, str):
        raise NaturalScheduleError("не указано, какую задачу изменить")
    return {
        "operation": operation,
        "kind": kind,
        "natural_description": scheduler._safe_text(
            description, field="natural description"
        ),
        "canonical_payload": safe_payload,
        "timezone": zone_name,
        "next_run": int(due.timestamp()),
        "recurrence": safe_recurrence,
        "query": " ".join(query.split()) if isinstance(query, str) else None,
    }


def parse_natural_request(
    question: object,
    *,
    now: datetime | None = None,
    model_parser: Callable[[str, datetime], Mapping[str, Any]] = _model_document,
) -> dict[str, Any]:
    text = _question(question)
    current = now or datetime.now(ZoneInfo(scheduler.DEFAULT_TIMEZONE))
    try:
        document = model_parser(text, current)
        return validate_model_document(dict(document), now=current)
    except (NaturalScheduleError, model_ha_proof.ProofError, OSError, ValueError):
        fallback = _deterministic_fallback(text, current)
        if fallback is None:
            raise NaturalScheduleError("не смог надёжно определить дату и время")
        return validate_model_document(fallback, now=current)


NUMBER_HOURS = {
    "ноль": 0, "час": 1, "один": 1, "два": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9,
    "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
    "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16,
    "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19,
    "двадцать": 20, "двадцать один": 21, "двадцать два": 22,
    "двадцать три": 23,
}
WEEKDAYS = {
    "понедельник": 0, "вторник": 1, "сред": 2, "четверг": 3,
    "пятниц": 4, "суббот": 5, "воскресень": 6,
}


def _clock(text: str) -> tuple[int, int] | None:
    numeric = re.search(r"\b(?:в|на)\s*(\d{1,2})(?:[:.]([0-5]\d))?\b", text)
    if numeric:
        hour = int(numeric.group(1))
        minute = int(numeric.group(2) or 0)
        return (hour, minute) if 0 <= hour <= 23 else None
    for word, hour in sorted(NUMBER_HOURS.items(), key=lambda item: -len(item[0])):
        if re.search(rf"\b(?:в|на)\s+{re.escape(word)}\b", text):
            return hour, 0
    return None


def _deterministic_fallback(text: str, now: datetime) -> dict[str, Any] | None:
    folded = text.casefold()
    nulls: dict[str, Any] = {
        "kind": None, "natural_description": None, "canonical_payload": None,
        "timezone": None, "next_run_local": None, "recurrence": None,
        "query": None, "clarification": None,
    }
    if re.search(r"\bотмен\S*\b", folded) and re.search(r"\bнапомин\S*\b", folded):
        match = re.search(r"\b(?:про|о)\s+(.+)$", folded)
        query = match.group(1).strip(" .") if match else None
        return {"operation": "cancel", **nulls, "query": query}
    clock = _clock(folded)
    is_report = "отчёт" in folded or "отчет" in folded
    is_reminder = bool(re.search(r"\bнапом(?:ни|нить|инание)\S*\b", folded))
    if not (is_report or is_reminder):
        return None
    if "через полтора часа" in folded:
        due = now + timedelta(minutes=90)
    elif "завтра" in folded:
        if clock is None:
            return None
        due = (now + timedelta(days=1)).replace(
            hour=clock[0], minute=clock[1], second=0, microsecond=0
        )
    elif "сегодня" in folded:
        if clock is None:
            return None
        due = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
    else:
        weekday = next(
            (value for stem, value in WEEKDAYS.items() if stem in folded), None
        )
        if weekday is not None:
            if clock is None:
                return None
            days = (weekday - now.weekday()) % 7
            due = (now + timedelta(days=days)).replace(
                hour=clock[0], minute=clock[1], second=0, microsecond=0
            )
            if due <= now:
                due += timedelta(days=7)
        elif is_report and clock is not None:
            due = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
            if due <= now:
                due += timedelta(days=1)
        else:
            return None
    recurrence: dict[str, Any] = {"type": "none"}
    if "каждый будний" in folded:
        recurrence = {"type": "weekdays", "time": due.strftime("%H:%M")}
    elif "раз в две недели" in folded:
        recurrence = {
            "type": "weekly", "time": due.strftime("%H:%M"),
            "weekday": due.weekday(), "interval_weeks": 2,
        }
    elif is_report and ("ежеднев" in folded or "отчёт" in folded or "отчет" in folded):
        recurrence = {"type": "daily", "time": due.strftime("%H:%M")}
    if is_report:
        return {
            "operation": "update", "kind": "daily_report",
            "natural_description": "Ежедневный отчёт о состоянии дома",
            "canonical_payload": {"report": "home_status"},
            "timezone": scheduler.DEFAULT_TIMEZONE,
            "next_run_local": due.isoformat(timespec="seconds"),
            "recurrence": recurrence, "query": "ежедневный отчёт",
            "clarification": None,
        }
    marker = re.search(r"\bнапом(?:ни|нить)\S*\s+(.+)$", text, re.IGNORECASE)
    reminder_text = marker.group(1).strip(" .") if marker else ""
    reminder_text = re.sub(r"^мне\s+", "", reminder_text, flags=re.IGNORECASE)
    if not reminder_text:
        return None
    return {
        "operation": "create", "kind": "local_reminder",
        "natural_description": f"Напоминание: {reminder_text}",
        "canonical_payload": {"text": reminder_text},
        "timezone": scheduler.DEFAULT_TIMEZONE,
        "next_run_local": due.isoformat(timespec="seconds"),
        "recurrence": recurrence, "query": None, "clarification": None,
    }


def _format_task(task: scheduler.TaskSpec, *, verb: str) -> str:
    zone = ZoneInfo(task.timezone)
    due = datetime.fromtimestamp(task.next_run, zone)
    recurrence = {
        "none": "без повтора",
        "daily": "ежедневно",
        "weekdays": "каждый будний день",
        "weekly": (
            "раз в две недели"
            if task.recurrence.get("interval_weeks") == 2 else "еженедельно"
        ),
        "interval": f"каждые {task.recurrence.get('seconds')} секунд",
    }.get(str(task.recurrence.get("type")), "по сохранённому расписанию")
    return (
        f"Задача {verb}: {due:%d.%m.%Y в %H:%M}, часовой пояс "
        f"{task.timezone}, повтор — {recurrence}."
    )


def apply_plan(plan: Mapping[str, Any], *, store: scheduler.SchedulerStore) -> str:
    operation = plan["operation"]
    if operation == "clarify":
        return str(plan["clarification"])
    if operation == "create":
        task = store.create_task(
            owner_id=scheduler.DEFAULT_OWNER_ID,
            kind=str(plan["kind"]),
            natural_description=str(plan["natural_description"]),
            canonical_payload=dict(plan["canonical_payload"]),
            timezone=str(plan["timezone"]),
            next_run=int(plan["next_run"]),
            recurrence=dict(plan["recurrence"]),
            delivery_target="station_max" if plan["kind"] in {
                "local_reminder", "daily_report", "recurring_report", "one_shot_report"
            } else "local",
            delivery_mode="tts" if plan["kind"] in {
                "local_reminder", "daily_report", "recurring_report", "one_shot_report"
            } else "workspace",
            missed_run_policy="run_once",
            verification_policy=(
                "station_state_transition"
                if plan["kind"] in {
                    "local_reminder", "daily_report", "recurring_report", "one_shot_report"
                } else "none"
            ),
        )
        return _format_task(task, verb="создана")
    if operation == "update":
        if plan.get("kind") == "daily_report":
            task_id = scheduler.SYSTEM_DAILY_REPORT_ID
        else:
            matches = store.list_tasks(query=str(plan.get("query") or ""), limit=3)
            if len(matches) != 1:
                return "Нашёл несколько подходящих задач. Какую именно изменить?"
            task_id = matches[0].task_id
        task = store.update_task(
            task_id,
            natural_description=str(plan["natural_description"]),
            canonical_payload=dict(plan["canonical_payload"]),
            timezone=str(plan["timezone"]),
            next_run=int(plan["next_run"]),
            recurrence=dict(plan["recurrence"]),
            enabled=True,
        )
        return _format_task(task, verb="изменена")
    if operation == "cancel":
        matches = store.list_tasks(query=str(plan["query"]), limit=3)
        if not matches:
            return "Такую активную задачу не нашёл."
        if len(matches) != 1:
            return "Нашёл несколько подходящих задач. Какую именно отменить?"
        task = store.cancel_task(matches[0].task_id)
        return f"Задача отменена: {task.natural_description}."
    if operation in {"list", "get"}:
        matches = store.list_tasks(query=plan.get("query"), limit=20)
        if not matches:
            return "Подходящих задач не нашёл."
        return "Задачи: " + "; ".join(task.natural_description for task in matches) + "."
    raise NaturalScheduleError("операция расписания не поддерживается")


def handle_natural_task_request(
    question: str,
    *,
    store: scheduler.SchedulerStore | None = None,
    now: datetime | None = None,
    model_parser: Callable[[str, datetime], Mapping[str, Any]] = _model_document,
) -> str:
    database = scheduler.SchedulerStore() if store is None else store
    plan = parse_natural_request(question, now=now, model_parser=model_parser)
    return apply_plan(plan, store=database)
