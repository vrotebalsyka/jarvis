#!/usr/bin/env python3
"""Build a complete read-only, model-annotated Home Assistant entity report."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import ha_entity_query  # noqa: E402
import heartbeat  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402
import model_ha_proof  # noqa: E402
import model_runtime_policy  # noqa: E402
from ollama_endpoint import load_runtime_ollama_endpoint  # noqa: E402


MODEL = model_runtime_policy.get_profile("diagnostic").model
BATCH_SIZE = 12
MAX_ENTITIES = 4096
MAX_TEXT_CHARS = 600
REPORT_PATH = Path(os.environ.get(
    "HOME_BUTLER_FULL_ENTITY_REPORT_PATH",
    str(Path.home() / ".local/state/home-butler/ha-full-entity-report.md"),
))
FORBIDDEN_TEXT_RE = re.compile(
    r"(?:https?://|bearer|token|secret|password|system\s+prompt|ignore\s+previous|"
    r"токен|секрет|парол|игнорир\S*\s+инструкц)", re.IGNORECASE
)


class FullReportError(RuntimeError):
    """A secret-free full report failure."""


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        raise FullReportError("model annotation is invalid")
    text = " ".join(value.split())[:MAX_TEXT_CHARS]
    if not text or FORBIDDEN_TEXT_RE.search(text) or any(ord(ch) > 0xFFFF for ch in text):
        raise FullReportError("model annotation is unsafe")
    return text


def collect_entities(
    snapshot: dict[str, Any], inventory: dict[str, Any]
) -> list[dict[str, Any]]:
    states = {
        item.get("entity_id"): item
        for item in snapshot.get("entities", [])
        if isinstance(item, dict) and isinstance(item.get("entity_id"), str)
    }
    raw_entities = inventory.get("entities")
    devices = inventory.get("physical_devices")
    if (
        not isinstance(raw_entities, list)
        or not isinstance(devices, list)
        or len(raw_entities) > MAX_ENTITIES
        or len(devices) > MAX_ENTITIES
    ):
        raise FullReportError("inventory is invalid")
    device_names: dict[str, str] = {}
    for device in devices:
        if not isinstance(device, dict):
            continue
        physical_hash = device.get("physical_device_hash")
        display_name = ha_read.sanitize_friendly_name(device.get("display_name"))
        if isinstance(physical_hash, str) and display_name is not None:
            device_names[physical_hash] = display_name
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_entities:
        if not isinstance(raw, dict):
            continue
        try:
            entity_id = ha_read._validate_entity_id(raw.get("entity_id"))
        except ha_read.AdapterError:
            continue
        if entity_id in seen or entity_id not in states:
            continue
        seen.add(entity_id)
        state = states[entity_id]
        physical_hash = raw.get("physical_device_hash")
        platform = raw.get("platform")
        result.append({
            "device_name": device_names.get(physical_hash, "Без физического устройства"),
            "entity_id": entity_id,
            "friendly_name": ha_read.sanitize_friendly_name(raw.get("friendly_name")),
            "domain": entity_id.split(".", 1)[0],
            "platform": platform if isinstance(platform, str) else "runtime",
            "state_kind": state.get("state_kind"),
            "state_value": state.get("state_value"),
            "source_last_updated_at": state.get("source_last_updated_at"),
        })
    if not result or len(result) != len(states):
        missing = sorted(set(states) - seen)
        for entity_id in missing:
            state = states[entity_id]
            result.append({
                "device_name": "Без физического устройства",
                "entity_id": entity_id,
                "friendly_name": None,
                "domain": entity_id.split(".", 1)[0],
                "platform": "runtime",
                "state_kind": state.get("state_kind"),
                "state_value": state.get("state_value"),
                "source_last_updated_at": state.get("source_last_updated_at"),
            })
    return sorted(result, key=lambda item: (
        str(item["device_name"]).casefold(), str(item["entity_id"])
    ))


def _schema(entity_ids: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "entity_id": {"type": "string", "enum": entity_ids},
                        "analysis_ru": {"type": "string"},
                    },
                    "required": ["entity_id", "analysis_ru"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["entities"],
        "additionalProperties": False,
    }


def ask_model(batch: list[dict[str, Any]]) -> list[dict[str, str]]:
    entity_ids = [str(item["entity_id"]) for item in batch]
    prompt = (
        "Ты локальный Home Butler и работаешь строго в режиме чтения. Разбери "
        "КАЖДУЮ переданную сущность Home Assistant. Не пропускай и не добавляй "
        "entity_id. Для каждой объясни назначение, буквально истолкуй текущее "
        "состояние, укажи что требует внимания, и какие действия возможны для "
        "этого domain. Объедини это в одно ёмкое русское заключение максимум из "
        "трёх предложений. Не утверждай неизвестное и не выполняй действий. FACTS="
        + json.dumps(batch, ensure_ascii=False, separators=(",", ":"))
    )
    runtime_profile = model_runtime_policy.get_profile("diagnostic")
    response = model_ha_proof.call_ollama(
        load_runtime_ollama_endpoint(),
        "/api/chat",
        model_runtime_policy.build_chat_payload(
            "diagnostic",
            [{"role": "user", "content": prompt}],
            response_format=_schema(entity_ids),
        ),
        timeout=runtime_profile.request_timeout_seconds,
    )
    message = response.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or len(content.encode("utf-8")) > 512_000:
        raise FullReportError("model report response is invalid")
    try:
        document = ha_read.strict_json_loads(content.encode("utf-8"))
    except ha_read.AdapterError as error:
        raise FullReportError("model report response is invalid") from error
    raw = document.get("entities") if isinstance(document, dict) else None
    if not isinstance(raw, list) or len(raw) != len(batch):
        raise FullReportError("model omitted report entities")
    by_id: dict[str, dict[str, str]] = {}
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"entity_id", "analysis_ru"}:
            raise FullReportError("model report item is invalid")
        entity_id = item.get("entity_id")
        if entity_id not in entity_ids or entity_id in by_id:
            raise FullReportError("model changed report scope")
        try:
            analysis = _clean_text(item.get("analysis_ru"))
        except FullReportError:
            analysis = (
                "Автоматическое заключение скрыто защитным фильтром. "
                "Точные безопасные факты Home Assistant приведены выше."
            )
        by_id[str(entity_id)] = {"analysis_ru": analysis}
    if set(by_id) != set(entity_ids):
        raise FullReportError("model report is incomplete")
    return [{"entity_id": entity_id, **by_id[entity_id]} for entity_id in entity_ids]


def _fallback_annotations(batch: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "entity_id": str(item["entity_id"]),
            "analysis_ru": (
                "Локальная модель не вернула надёжное отдельное заключение для "
                "этой сущности. Точные безопасные факты Home Assistant приведены выше."
            ),
        }
        for item in batch
    ]


def _render_value(value: Any) -> str:
    if value is None:
        return "нет значения"
    return str(value).replace("\n", " ")[:300]


def build_report() -> tuple[str, int, int]:
    snapshot, exit_code = ha_read.execute_safely("snapshot")
    if exit_code != 0 or snapshot.get("status") not in {"healthy", "stale_data"}:
        raise FullReportError("Home Assistant snapshot is unavailable")
    entities = collect_entities(snapshot, ha_entity_query.load_inventory())
    annotations: dict[str, dict[str, str]] = {}
    for offset in range(0, len(entities), BATCH_SIZE):
        batch = entities[offset:offset + BATCH_SIZE]
        print(json.dumps({
            "event": "model_batch",
            "from": offset + 1,
            "to": offset + len(batch),
            "total": len(entities),
        }, separators=(",", ":")), flush=True)
        try:
            batch_annotations = ask_model(batch)
        except (FullReportError, model_ha_proof.ProofError, OSError):
            batch_annotations = _fallback_annotations(batch)
            print(json.dumps({
                "event": "model_batch_fallback",
                "from": offset + 1,
                "to": offset + len(batch),
            }, separators=(",", ":")), flush=True)
        for item in batch_annotations:
            annotations[item["entity_id"]] = item
    if len(annotations) != len(entities):
        raise FullReportError("model report is incomplete")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in entities:
        grouped.setdefault(str(item["device_name"]), []).append(item)
    lines = [
        "# Полный отчёт локальной модели по Home Assistant",
        "",
        f"Сформирован: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"Физических групп: {len(grouped)}; сущностей: {len(entities)}.",
        "Режим: только чтение; действий и service call: 0.",
        "",
    ]
    for device_name, members in grouped.items():
        lines.extend((f"## {device_name}", ""))
        for item in members:
            note = annotations[str(item["entity_id"])]
            lines.extend((
                f"### `{item['entity_id']}` — {item.get('friendly_name') or 'без имени'}",
                "",
                f"- Domain/platform: `{item['domain']}` / `{item['platform']}`",
                f"- Текущее значение: `{_render_value(item.get('state_value'))}` "
                f"(`{item.get('state_kind')}`)",
                f"- Обновлено источником: `{item.get('source_last_updated_at') or 'неизвестно'}`",
                f"- Заключение локальной модели: {note['analysis_ru']}",
                "",
            ))
    return "\n".join(lines), len(grouped), len(entities)


def main() -> int:
    try:
        report, device_count, entity_count = build_report()
        heartbeat._validate_state_dir(REPORT_PATH.parent)
        heartbeat._atomic_write(REPORT_PATH, report.encode("utf-8"))
    except (FullReportError, ha_read.AdapterError, model_ha_proof.ProofError, OSError) as error:
        print(json.dumps({
            "status": "failed",
            "error": str(error) if isinstance(error, FullReportError) else "report_failed",
        }, ensure_ascii=False, separators=(",", ":")))
        return 3
    print(json.dumps({
        "status": "completed",
        "device_count": device_count,
        "entity_count": entity_count,
        "actions_performed": 0,
        "path": str(REPORT_PATH),
    }, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
