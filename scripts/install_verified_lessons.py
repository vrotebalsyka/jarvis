#!/usr/bin/env python3
"""Install reviewed, secret-free Home Butler lessons into its bounded workspace."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import model_workspace  # noqa: E402


LESSON_PATH = "knowledge/verified-lessons-stage67.json"
TRAINING_PATH = "knowledge/training/stage67-household.jsonl"
REPORT_PATH = "reports/stage67-learning-install.json"
DEFAULT_TRAINING_SOURCE = SCRIPT_DIR.parent / "training" / "stage67_verified_examples.jsonl"
MAX_TRAINING_BYTES = 2 * 1024 * 1024
UNSAFE_TRAINING_RE = re.compile(
    r"(?:Authorization|Bearer\s+|password|passwd|cookie|set-cookie|"
    r"-----BEGIN|\b(?:10|127|169\.254|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b|"
    r"\b(?:[0-9A-F]{2}:){5}[0-9A-F]{2}\b)",
    re.IGNORECASE,
)

LESSONS = {
    "schema_version": 1,
    "lesson_set": "stage67-truthful-home",
    "authority": "reviewed_owner_evidence_and_sanitized_ha_facts",
    "rules": [
        {
            "id": "robot_charging",
            "fact": "vacuum state charging or docked means dock/charging, not movement or cleaning",
        },
        {
            "id": "literal_numbers",
            "fact": "battery and maintenance percentages must be copied exactly from current HA observations",
        },
        {
            "id": "partial_feature",
            "fact": "one unavailable feature is not proof that the physical device is offline",
        },
        {
            "id": "no_invented_cause",
            "fact": "never invent Wi-Fi, reset or frozen-module causes without explicit evidence",
        },
        {
            "id": "accepted_not_verified",
            "fact": "accepted commands are not physical success; success requires a verified receipt",
        },
        {
            "id": "conditional_availability",
            "fact": "a control unavailable while an appliance is off may be expected and is not automatically an incident",
        },
        {
            "id": "owner_facing_privacy",
            "fact": "owner-facing answers use human names and never expose entity IDs, hashes, IP/MAC or secrets",
        },
    ],
}


class LessonInstallError(RuntimeError):
    """A bounded, secret-free lesson installation failure."""


def _training_source() -> Path:
    configured = os.environ.get("HOME_BUTLER_STAGE67_TRAINING_FILE")
    return Path(configured) if configured else DEFAULT_TRAINING_SOURCE


def _read_training(path: Path | None = None) -> tuple[str, list[dict[str, Any]]]:
    source = _training_source() if path is None else path
    try:
        metadata = source.lstat()
    except OSError as error:
        raise LessonInstallError("verified training source is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= MAX_TRAINING_BYTES
        or metadata.st_mode & 0o022
    ):
        raise LessonInstallError("verified training source is unsafe")
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise LessonInstallError("verified training source is unreadable") from error
    if UNSAFE_TRAINING_RE.search(text) or model_workspace.SECRET_RE.search(text):
        raise LessonInstallError("verified training source contains private data")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as error:
            raise LessonInstallError("verified training source is invalid JSONL") from error
        lesson_id = item.get("id") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or not isinstance(lesson_id, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,79}", lesson_id)
            or lesson_id in seen
            or not isinstance(item.get("input"), str)
        ):
            raise LessonInstallError("verified training row is invalid")
        seen.add(lesson_id)
        rows.append(item)
    if len(rows) < 20:
        raise LessonInstallError("verified training corpus is incomplete")
    normalized = "".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for item in rows
    )
    return normalized, rows


def install() -> dict[str, object]:
    status = model_workspace.status()
    training_text, rows = _read_training()
    lesson_text = json.dumps(LESSONS, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    lesson_result = model_workspace.write_text(LESSON_PATH, lesson_text)
    training_result = model_workspace.write_text(TRAINING_PATH, training_text)
    report = {
        "schema_version": 1,
        "status": "installed",
        "lesson_set": LESSONS["lesson_set"],
        "lesson_count": len(LESSONS["rules"]),
        "training_example_count": len(rows),
        "workspace_max_bytes": status["max_bytes"],
        "artifacts": [LESSON_PATH, TRAINING_PATH],
        "weights_modified": False,
        "runtime_grounding_required": True,
        "training_mode": "reviewed_examples_plus_deterministic_grounding",
    }
    model_workspace.write_text(
        REPORT_PATH,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    report["lesson_bytes"] = lesson_result["size_bytes"]
    report["training_bytes"] = training_result["size_bytes"]
    return report


def main() -> int:
    try:
        result = install()
    except (LessonInstallError, model_workspace.WorkspaceError, OSError, ValueError):
        print("VERIFIED_LESSON_INSTALL_FAILED", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
