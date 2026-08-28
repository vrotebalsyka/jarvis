#!/usr/bin/env python3
"""Real read-only Stage 68 qualification through the live Alice JSON gateway."""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import sqlite3
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


sys.dont_write_bytecode = True
PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import device_learning  # noqa: E402
import ha_entity_query  # noqa: E402
import home_assistant_mcp  # noqa: E402
import home_assistant_read  # noqa: E402


QUESTIONS = (
    "Что с Андреем?",
    "Как там Андрей?",
    "Сколько у Андрея батареи?",
    "Что с фильтром?",
    "Что с основной щёткой?",
    "Что с боковой щёткой?",
    "Какие проблемы у Андрея?",
    "Почему функция недоступна?",
)
ENTITY_ID_RE = re.compile(r"\b[a-z0-9_]{1,64}\.[a-z0-9_]{2,200}\b", re.I)
OPAQUE_ID_RE = re.compile(r"\b(?:cap_[a-f0-9]{24}|[a-f0-9]{64})\b", re.I)
PRIVATE_ADDRESS_RE = re.compile(
    r"\b(?:10|127|169\.254|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b"
)
TASK_RE = re.compile(r"Задача\s+([a-f0-9]{6,32})", re.I)
TASK_RESULT_RE = re.compile(r"^Задача\s+[a-f0-9]{6,32}:\s*", re.I)
MAX_PRIVATE_FILE = 16_384


class EvaluationError(RuntimeError):
    """One secret-free qualification failure."""


def _load_inventory_for_qualification() -> dict[str, Any]:
    if os.geteuid() != 0:
        return ha_entity_query.load_inventory()
    path = ha_entity_query.incident_monitor._state_dir() / "inventory.json"
    try:
        expected_uid = pwd.getpwnam("homebutler").pw_uid
        metadata = path.lstat()
    except (KeyError, OSError) as error:
        raise EvaluationError("private inventory is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or metadata.st_mode & 0o077
        or not 0 < metadata.st_size <= ha_entity_query.MAX_INVENTORY_BYTES
    ):
        raise EvaluationError("private inventory is unsafe")
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        raw = os.read(descriptor, ha_entity_query.MAX_INVENTORY_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > ha_entity_query.MAX_INVENTORY_BYTES:
        raise EvaluationError("private inventory is too large")
    try:
        document = home_assistant_read.strict_json_loads(raw)
    except home_assistant_read.AdapterError as error:
        raise EvaluationError("private inventory is invalid") from error
    if not isinstance(document, dict):
        raise EvaluationError("private inventory is invalid")
    return document


def _private_text(path: Path) -> str:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise EvaluationError("Alice qualification credential is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & 0o077
        or not 0 < metadata.st_size <= MAX_PRIVATE_FILE
    ):
        raise EvaluationError("Alice qualification credential is unsafe")
    descriptor = os.open(
        path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        raw = os.read(descriptor, MAX_PRIVATE_FILE + 1)
    finally:
        os.close(descriptor)
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise EvaluationError("Alice qualification credential is invalid") from error
    if not value or len(raw) > MAX_PRIVATE_FILE:
        raise EvaluationError("Alice qualification credential is invalid")
    return value


def _number_forms(value: str) -> set[str]:
    normalized = value.replace(",", ".")
    forms = {value, normalized, normalized.replace(".", ",")}
    try:
        numeric = float(normalized)
    except ValueError:
        return forms
    if numeric.is_integer():
        forms.add(str(int(numeric)))
    return forms


def _numbers(document: object) -> set[str]:
    raw = document if isinstance(document, str) else json.dumps(document, ensure_ascii=False)
    return {
        form
        for value in re.findall(r"(?<!\d)\d+(?:[.,]\d+)?(?!\d)", raw)
        for form in _number_forms(value)
    }


class AliceClient:
    def __init__(self) -> None:
        secrets = PROJECT_DIR / "secrets"
        self.secret = _private_text(secrets / "alice-skill-secret")
        self.skill_id = _private_text(secrets / "alice-skill-id")
        owners = [
            item.strip()
            for item in _private_text(secrets / "alice-owner-ids").splitlines()
            if item.strip()
        ]
        if len(owners) < 1:
            raise EvaluationError("Alice owner qualification is unavailable")
        self.owner_id = owners[0]
        self.session_id = f"stage68-eval-{int(time.time())}"
        self.message_id = 0

    def _request(self, utterance: str, *, new: bool = False) -> tuple[str, float]:
        document = {
            "version": "1.0",
            "request": {
                "type": "SimpleUtterance",
                "original_utterance": utterance,
                "command": utterance,
            },
            "session": {
                "session_id": self.session_id,
                "message_id": self.message_id,
                "new": new,
                "skill_id": self.skill_id,
                "user": {"user_id": self.owner_id},
            },
        }
        request = urllib.request.Request(
            "http://127.0.0.1:8765/alice/" + self.secret,
            data=json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=8.0) as response:
                result = home_assistant_read.strict_json_loads(response.read())
        except (OSError, urllib.error.URLError, home_assistant_read.AdapterError) as error:
            raise EvaluationError("live Alice JSON request failed") from error
        elapsed = time.monotonic() - started
        self.message_id += 1
        body = result.get("response") if isinstance(result, dict) else None
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise EvaluationError("live Alice JSON response is invalid")
        return text.strip(), elapsed

    def ask(self, utterance: str, *, new: bool = False) -> tuple[str, float, bool]:
        started = time.monotonic()
        answer, _initial = self._request(utterance, new=new)
        match = TASK_RE.search(answer)
        if match is None:
            return answer, time.monotonic() - started, False
        task_id = match.group(1)
        for _attempt in range(80):
            time.sleep(0.5)
            answer, _status_latency = self._request(f"Статус задачи {task_id}")
            if "ещё выполняется" not in answer:
                return TASK_RESULT_RE.sub("", answer), time.monotonic() - started, True
        raise EvaluationError("live Alice task did not finish")


def _validate_answer(
    question: str, answer: str, compact: dict[str, Any]
) -> list[str]:
    reasons: list[str] = []
    if len(answer) > 360:
        reasons.append("voice_answer_too_long")
    if ENTITY_ID_RE.search(answer) or OPAQUE_ID_RE.search(answer) or PRIVATE_ADDRESS_RE.search(answer):
        reasons.append("private_identifier_exposed")
    if not _numbers(answer) <= _numbers(compact):
        reasons.append("invented_number")
    reasons.extend(device_learning.validate_compact_answer(compact, question, answer))
    folded = answer.casefold()
    if any(value in folded for value in ("не могу подключиться", "назовите entity", "требуется токен")):
        reasons.append("not_grounded_in_live_ha")
    return sorted(set(reasons))


def _session_fingerprint(value: str) -> str:
    return hashlib.blake2s(value.encode("utf-8"), digest_size=16).hexdigest()


def _read_trace_evidence(session_id: str, started_epoch: int) -> dict[str, Any]:
    database = Path("/home/homebutler/.local/state/home-butler/memory/memory.db")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT route,total_latency_ms,tool_calls,model_calls,action "
            "FROM agent_turn_traces WHERE transport='alice' AND session_key=? "
            "AND created_at>=? ORDER BY created_at",
            (_session_fingerprint(session_id), started_epoch),
        ).fetchall()
    finally:
        connection.close()
    tool_names: list[str] = []
    action_records: list[Any] = []
    model_latencies: list[int] = []
    for row in rows:
        tools = json.loads(str(row["tool_calls"]))
        models = json.loads(str(row["model_calls"]))
        actions = json.loads(str(row["action"]))
        tool_names.extend(
            str(item.get("name")) for item in tools if isinstance(item, dict)
        )
        model_latencies.extend(
            int(item.get("latency_ms"))
            for item in models
            if isinstance(item, dict) and isinstance(item.get("latency_ms"), int)
        )
        if actions:
            action_records.append(actions)
    forbidden_tools = sorted({
        name for name in tool_names
        if name not in {"ha_find_devices", "ha_read.snapshot", "ha_get_device_details"}
    })
    return {
        "trace_count": len(rows),
        "tool_names": sorted(set(tool_names)),
        "forbidden_tools": forbidden_tools,
        "action_record_count": len(action_records),
        "model_call_count": len(model_latencies),
        "model_latency_ms": model_latencies,
    }


def main() -> int:
    started_epoch = int(time.time())
    inventory = _load_inventory_for_qualification()
    snapshot, exit_code = home_assistant_read.execute_safely("snapshot")
    if exit_code != 0 or snapshot.get("status") not in {"healthy", "stale_data"}:
        raise EvaluationError("live Home Assistant snapshot is unavailable")
    found = home_assistant_mcp.find_model_devices(inventory, query="Андрей", limit=2)
    devices = found.get("devices")
    if found.get("matched_device_count") != 1 or not isinstance(devices, list) or len(devices) != 1:
        raise EvaluationError("Андрей did not resolve to one physical device")
    physical_id = devices[0].get("physical_device_id")
    if not isinstance(physical_id, str):
        raise EvaluationError("Андрей physical device is unavailable")
    details = home_assistant_mcp.get_model_device_details(snapshot, inventory, physical_id)
    profile = device_learning.build_profile(details, inventory)
    client = AliceClient()
    checks: list[dict[str, Any]] = []
    for index, question in enumerate(QUESTIONS):
        compact = device_learning.compact_profile(profile, details, question, maximum=3)
        answer, latency, deferred = client.ask(question, new=index == 0)
        reasons = _validate_answer(question, answer, compact)
        checks.append({
            "question": question,
            "answer": answer,
            "pass": not reasons,
            "reasons": reasons,
            "latency_seconds": round(latency, 3),
            "deferred": deferred,
            "relevant_feature_count": compact.get("relevant_feature_count"),
        })
    traces = _read_trace_evidence(client.session_id, started_epoch)
    e2e_client = AliceClient()
    e2e_questions = ("Что с роботом Андреем?", "А батарея?")
    e2e_checks: list[dict[str, Any]] = []
    for index, question in enumerate(e2e_questions):
        compact = device_learning.compact_profile(profile, details, question, maximum=3)
        answer, latency, deferred = e2e_client.ask(question, new=index == 0)
        reasons = _validate_answer(question, answer, compact)
        e2e_checks.append({
            "question": question,
            "answer": answer,
            "pass": not reasons,
            "reasons": reasons,
            "latency_seconds": round(latency, 3),
            "deferred": deferred,
        })
    e2e_traces = _read_trace_evidence(e2e_client.session_id, started_epoch)
    all_pass = (
        all(item["pass"] for item in checks)
        and all(item["pass"] for item in e2e_checks)
        and not traces["forbidden_tools"]
        and not e2e_traces["forbidden_tools"]
        and traces["action_record_count"] == 0
        and e2e_traces["action_record_count"] == 0
        and traces["model_call_count"] >= len(QUESTIONS)
        and e2e_traces["model_call_count"] >= len(e2e_questions)
    )
    result = {
        "schema_version": 1,
        "stage": 68,
        "status": "pass" if all_pass else "fail",
        "read_only": True,
        "service_calls": 0,
        "observed_at": snapshot.get("observed_at"),
        "device_name": details.get("display_name"),
        "question_count": len(checks),
        "passed_count": sum(item["pass"] for item in checks),
        "alice_e2e": {
            "yandex_json": True,
            "initial_question_pass": e2e_checks[0]["pass"],
            "coreference_followup_pass": e2e_checks[1]["pass"],
            "direct_response_count": sum(not item["deferred"] for item in e2e_checks),
            "maximum_latency_seconds": max(item["latency_seconds"] for item in e2e_checks),
            "checks": e2e_checks,
            "trace_evidence": e2e_traces,
        },
        "checks": checks,
        "trace_evidence": traces,
    }
    output = PROJECT_DIR / "reports" / "stage68-evaluation" / "latest.json"
    output.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if all_pass else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvaluationError as error:
        print(json.dumps({"schema_version": 1, "stage": 68, "status": "error", "error": str(error)}))
        raise SystemExit(2)
