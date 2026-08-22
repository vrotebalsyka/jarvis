#!/usr/bin/env python3
"""Let the local model study sanitized HA diagnostics without changing HA."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import ha_entity_query  # noqa: E402
import heartbeat  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402
import model_ha_proof  # noqa: E402
from ollama_endpoint import load_runtime_ollama_endpoint  # noqa: E402


MODEL = "qwen3.5:4b-q4_K_M"
CATALOG_NAME = "ha-model-study.json"
MAX_FINDINGS = 128
CANDIDATE_RE = re.compile(
    r"(?:error|problem|fault|filter|brush|consum|life|wear|salt|rinse|dust|"
    r"ошиб|проблем|фильтр|щет|износ|соль|ополас)", re.IGNORECASE
)
CATEGORIES = {
    "problem_flag", "error_code", "remaining_life", "consumable_shortage", "ignore"
}
CONDITIONS = {"on", "nonzero", "at_or_below_10", "never"}


class StudyError(RuntimeError):
    """Secret-free model study failure."""


def catalog_path() -> Path:
    return Path(os.environ.get(
        "HOME_BUTLER_MODEL_STUDY_PATH",
        str(Path.home() / ".local/state/home-butler/ha-model-study.json"),
    ))


def expected_rule(item: dict[str, Any]) -> tuple[str, str]:
    text = f"{item['entity_id']} {item.get('friendly_name') or ''}".casefold()
    if "life" in text and item.get("state_kind") == "number":
        return "remaining_life", "at_or_below_10"
    if "error_code" in text or "код ошибки" in text:
        return "error_code", "nonzero"
    if item.get("domain") == "binary_sensor" and any(
        marker in text for marker in ("problem", "fault", "проблем")
    ):
        return "problem_flag", "on"
    if item.get("domain") == "binary_sensor" and any(
        marker in text for marker in ("rinse", "salt", "ополас", "соль")
    ):
        return "consumable_shortage", "on"
    return "ignore", "never"


def collect_candidates(
    snapshot: dict[str, Any], inventory: dict[str, Any]
) -> list[dict[str, Any]]:
    total = ha_entity_query.search_entities(snapshot, inventory, limit=64)
    candidates: list[dict[str, Any]] = []
    for offset in range(0, int(total["matched_entity_count"]), 64):
        page = ha_entity_query.search_entities(
            snapshot, inventory, offset=offset, limit=64
        )
        for item in page["entities"]:
            text = f"{item['entity_id']} {item.get('friendly_name') or ''}"
            if not CANDIDATE_RE.search(text):
                continue
            expected_category, expected_condition = expected_rule(item)
            if expected_category == "ignore":
                continue
            candidates.append({
                "entity_id": item["entity_id"],
                "friendly_name": item.get("friendly_name"),
                "domain": item["domain"],
                "platform": item["platform"],
                "physical_device_hash": item["physical_device_hash"],
                "state_kind": item["state_kind"],
                "state_value": item.get("state_value"),
                "expected_category": expected_category,
                "expected_condition": expected_condition,
            })
    if not candidates or len(candidates) > MAX_FINDINGS:
        raise StudyError("diagnostic candidate set is invalid")
    return sorted(candidates, key=lambda item: str(item["entity_id"]))


def schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "category": {"enum": sorted(CATEGORIES)},
                    "alert_condition": {"enum": sorted(CONDITIONS)},
                    "reason_ru": {"type": "string"},
                },
                "required": ["entity_id", "category", "alert_condition", "reason_ru"],
                "additionalProperties": False,
            },
        }},
        "required": ["findings"],
        "additionalProperties": False,
    }


def ask_model(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    safe = [{
        key: item[key] for key in (
            "entity_id", "friendly_name", "domain", "platform", "state_kind", "state_value"
        )
    } for item in candidates]
    prompt = (
        "Ты локальный диагност Home Assistant. Изучи только безопасный список. "
        "Для КАЖДОЙ сущности выбери категорию и условие тревоги. "
        "Суффикс life означает остаток ресурса в процентах: remaining_life и "
        "at_or_below_10. error_code: error_code и nonzero. Бинарная нехватка "
        "соли/ополаскивателя: consumable_shortage и on. problem: problem_flag и on. "
        "Ничего не меняй и не добавляй сущности. Данные: "
        + json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    )
    return model_ha_proof.call_ollama(
        load_runtime_ollama_endpoint(),
        "/api/chat",
        {
            "model": MODEL,
            "stream": False,
            "think": False,
            "format": schema(),
            "keep_alive": "5m",
            "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 2048},
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=180,
    )


def validate_findings(
    response: dict[str, Any], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    message = response.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or len(content.encode("utf-8")) > 128_000:
        raise StudyError("model study response is invalid")
    try:
        document = ha_read.strict_json_loads(content.encode("utf-8"))
    except ha_read.AdapterError as error:
        raise StudyError("model study response is invalid") from error
    raw = document.get("findings") if isinstance(document, dict) else None
    if not isinstance(raw, list) or len(raw) != len(candidates):
        raise StudyError("model study response is incomplete")
    by_id: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict) or set(item) != {
            "entity_id", "category", "alert_condition", "reason_ru"
        }:
            raise StudyError("model study finding is invalid")
        entity_id = item.get("entity_id")
        raw_reason = item.get("reason_ru")
        reason_text = " ".join(raw_reason.split())[:120] if isinstance(raw_reason, str) else ""
        reason = ha_read.sanitize_friendly_name(reason_text)
        if not isinstance(entity_id, str) or entity_id in by_id:
            raise StudyError("model study entity identity is invalid")
        if (
            item.get("category") not in CATEGORIES
            or item.get("alert_condition") not in CONDITIONS
        ):
            raise StudyError("model study classification is invalid")
        if reason is None:
            reason = "Предложение локальной модели"
        by_id[entity_id] = {**item, "reason_ru": reason}
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        entity_id = str(candidate["entity_id"])
        proposed = by_id.get(entity_id)
        if proposed is None:
            raise StudyError("model study response changed entity scope")
        category = str(candidate["expected_category"])
        condition = str(candidate["expected_condition"])
        agreed = (
            proposed["category"] == category
            and proposed["alert_condition"] == condition
        )
        result.append({
            "entity_id": entity_id,
            "friendly_name": candidate.get("friendly_name"),
            "physical_device_hash": candidate["physical_device_hash"],
            "platform": candidate["platform"],
            "state_kind": candidate["state_kind"],
            "category": category,
            "alert_condition": condition,
            "model_proposed_category": proposed["category"],
            "model_proposed_condition": proposed["alert_condition"],
            "model_agreed": agreed,
            "model_reason_ru": proposed["reason_ru"],
        })
    return result


def build_catalog(
    *,
    snapshot_reader: Callable[[str], tuple[dict[str, Any], int]] = ha_read.execute_safely,
    inventory_loader: Callable[[], dict[str, Any]] = ha_entity_query.load_inventory,
    model_reader: Callable[[list[dict[str, Any]]], dict[str, Any]] = ask_model,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    snapshot, exit_code = snapshot_reader("snapshot")
    if exit_code != 0:
        raise StudyError("Home Assistant study snapshot is unavailable")
    candidates = collect_candidates(snapshot, inventory_loader())
    findings = validate_findings(model_reader(candidates), candidates)
    observed = int(now())
    body = {
        "schema_version": 1,
        "catalog_version": time.strftime("%Y-%m-%d", time.localtime(observed)),
        "observed_epoch": observed,
        "learning_scope": "read_only",
        "actions_performed": 0,
        "model": MODEL,
        "candidate_count": len(findings),
        "model_agreement_count": sum(item["model_agreed"] for item in findings),
        "model_rejected_count": sum(not item["model_agreed"] for item in findings),
        "findings": findings,
    }
    body["catalog_sha256"] = hashlib.sha256(
        json.dumps(findings, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return body


def write_catalog(document: dict[str, Any], path: Path | None = None) -> None:
    target = catalog_path() if path is None else path
    heartbeat._validate_state_dir(target.parent)
    heartbeat._atomic_write(
        target,
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        document = build_catalog()
        if not args.check:
            write_catalog(document)
    except (StudyError, ha_read.AdapterError, model_ha_proof.ProofError, OSError) as error:
        error_class = {
            StudyError: "study_validation_failed",
            ha_read.AdapterError: "ha_read_failed",
            model_ha_proof.ProofError: "model_call_failed",
            OSError: "local_io_failed",
        }.get(type(error), "study_failed")
        print(json.dumps({
            "schema_version": 1,
            "status": "failed",
            "error_class": error_class,
            "error_code": str(error) if isinstance(error, StudyError) else error_class,
        }, separators=(",", ":")))
        return 3
    print(json.dumps({
        "schema_version": 1,
        "status": "studied" if not args.check else "valid",
        "candidate_count": document["candidate_count"],
        "model_agreement_count": document["model_agreement_count"],
        "model_rejected_count": document["model_rejected_count"],
        "actions_performed": 0,
    }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
