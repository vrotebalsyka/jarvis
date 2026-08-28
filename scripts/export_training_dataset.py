#!/usr/bin/env python3
"""Operator-only export of validated Stage 68 workspace data into the repository."""

from __future__ import annotations

import json
import os
import pwd
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


sys.dont_write_bytecode = True
PROJECT_DIR = Path(__file__).resolve().parents[1]
WORKSPACE = Path("/home/homebutler/.local/share/home-butler/model-workspace")
MAX_SOURCE_BYTES = 16 * 1024 * 1024
STABLE_RE = re.compile(r"[a-f0-9]{24}\.jsonl\Z")
HASH_RE = re.compile(r"[a-f0-9]{64}\Z")
REQUIRED = {
    "id", "source_device", "source_snapshot_hash", "category", "input",
    "context", "expected", "evidence", "validator_version", "teacher_model",
    "created_at",
}
SECRET_RE = re.compile(
    r"(?:\beyJ[A-Za-z0-9_-]{16,}\.|Authorization\s*:\s*Bearer|"
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:[0-9A-F]{2}:){5}[0-9A-F]{2}\b)",
    re.IGNORECASE,
)
ENTITY_ID_RE = re.compile(r"\b[a-z_][a-z0-9_]{0,63}\.[a-z_][a-z0-9_]{1,199}\b")


class ExportError(RuntimeError):
    """The reviewed dataset could not be exported safely."""


def _service_uid() -> int:
    try:
        return pwd.getpwnam("homebutler").pw_uid
    except KeyError as error:
        raise ExportError("service account is unavailable") from error


def _read_private(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ExportError("learning artifact is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != _service_uid() or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or not 0 < metadata.st_size <= MAX_SOURCE_BYTES
    ):
        raise ExportError("learning artifact is unsafe")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    try:
        raw = os.read(descriptor, MAX_SOURCE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ExportError("learning artifact is oversized")
    return raw


def _parse_jsonl(raw: bytes, *, validated: bool) -> list[dict[str, Any]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExportError("learning artifact is not UTF-8") from error
    if SECRET_RE.search(text):
        raise ExportError("learning artifact contains private data")
    result: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError as error:
            raise ExportError("learning artifact is not JSONL") from error
        if not isinstance(item, dict) or not REQUIRED <= set(item):
            raise ExportError("training example schema is invalid")
        if not HASH_RE.fullmatch(str(item.get("source_snapshot_hash", ""))):
            raise ExportError("training evidence hash is invalid")
        if validated and ENTITY_ID_RE.search(str(item.get("expected", ""))):
            raise ExportError("training output exposes an entity id")
        if validated and "rejection_reasons" in item:
            raise ExportError("rejected example entered validated corpus")
        result.append(item)
    if not result:
        raise ExportError("learning artifact is empty")
    return result


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.resolve() not in {
        (PROJECT_DIR / "training/generated").resolve(),
        (PROJECT_DIR / "training/validated").resolve(),
        (PROJECT_DIR / "training/rejected").resolve(),
        (PROJECT_DIR / "reports/learning-rejected").resolve(),
    }:
        raise ExportError("export target is outside reviewed folders")
    descriptor, temporary = tempfile.mkstemp(prefix=".stage68-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o644)
        os.write(descriptor, content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def export() -> dict[str, Any]:
    if os.geteuid() != 0:
        raise ExportError("dataset export requires the operator account")
    sources = {
        "generated": WORKSPACE / "knowledge/training/generated",
        "validated": WORKSPACE / "knowledge/training/validated",
        "rejected": WORKSPACE / "reports/learning-rejected",
    }
    counts: dict[str, int] = {}
    validated_all: list[dict[str, Any]] = []
    devices: set[str] = set()
    for kind, directory in sources.items():
        if not directory.is_dir() or directory.is_symlink():
            raise ExportError("learning source directory is unavailable")
        count = 0
        for source in sorted(directory.iterdir()):
            if not STABLE_RE.fullmatch(source.name):
                continue
            items = _parse_jsonl(_read_private(source), validated=kind == "validated")
            data = "".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                for item in items
            ).encode("utf-8")
            _atomic_write(PROJECT_DIR / "training" / kind / source.name, data)
            if kind == "rejected":
                _atomic_write(PROJECT_DIR / "reports/learning-rejected" / source.name, data)
            if kind == "validated":
                validated_all.extend(items)
                devices.update(str(item["source_device"]) for item in items)
            count += len(items)
        counts[kind] = count
    combined = "".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for item in sorted(validated_all, key=lambda value: str(value["id"]))
    ).encode("utf-8")
    _atomic_write(PROJECT_DIR / "training/validated/stage68-all.jsonl", combined)
    return {
        "schema_version": 1,
        "status": "exported",
        "counts": counts,
        "validated_device_count": len(devices),
        "validated_total": len(validated_all),
        "weights_modified": False,
    }


def main() -> int:
    try:
        result = export()
    except (ExportError, OSError):
        print("TRAINING_EXPORT_FAILED", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
