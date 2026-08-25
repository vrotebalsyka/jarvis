#!/usr/bin/env python3
"""One durable, fail-closed source of truth for every Home Butler schedule."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import daily_voice_report  # noqa: E402
import heartbeat  # noqa: E402
import home_assistant_notify as ha_notify  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402
import model_workspace  # noqa: E402


SCHEMA_VERSION = 1
DEFAULT_OWNER_ID = "owner"
DEFAULT_TIMEZONE = "Asia/Yekaterinburg"
DEFAULT_DB_PATH = Path(
    os.environ.get(
        "HOME_BUTLER_SCHEDULER_DB",
        "/home/homebutler/.local/state/home-butler/scheduler/scheduler.sqlite3",
    )
)
DEFAULT_STATUS_PATH = Path(
    os.environ.get(
        "HOME_BUTLER_SCHEDULER_STATUS",
        "/home/homebutler/.local/state/home-butler/scheduler/scheduler-status.json",
    )
)
SYSTEM_DAILY_REPORT_ID = "system-daily-report"
SYSTEM_DAILY_REPORT_KEY = "system:daily-report:v1"
LEASE_SECONDS = 90
MISSED_RUN_GRACE_SECONDS = 15 * 60
REPORT_RETRY_SECONDS = 5 * 60
REPORT_RETRY_WINDOW_SECONDS = 15 * 60
MAX_TEXT = 500
MAX_RESULT_BYTES = 32 * 1024
MAX_LIST_TASKS = 100

TASK_KINDS = frozenset({
    "local_reminder",
    "yandex_native_reminder",
    "daily_report",
    "recurring_report",
    "one_shot_report",
    "scheduled_device_action",
    "follow_up_task",
    "deferred_diagnostic_result",
})
DELIVERY_MODES = frozenset({"none", "tts", "yandex_native", "ha_control", "workspace"})
MISSED_RUN_POLICIES = frozenset({"skip", "run_once", "catch_up_once"})
VERIFICATION_POLICIES = frozenset({
    "none",
    "station_state_transition",
    "yandex_success",
    "ha_state_readback",
})
TASK_STATUSES = frozenset({
    "scheduled", "running", "completed", "failed", "blocked",
    "delivery_unknown", "cancelled", "external_managed", "missed",
})
FORBIDDEN_PAYLOAD_KEYS = frozenset({
    "shell", "command", "service", "service_path", "executable", "exec",
    "script", "argv", "url", "token", "secret", "password",
})
PAYLOAD_FIELDS = {
    "local_reminder": frozenset({"text"}),
    "yandex_native_reminder": frozenset({"text", "station", "backend_status"}),
    "daily_report": frozenset({"report"}),
    "recurring_report": frozenset({"report"}),
    "one_shot_report": frozenset({"report"}),
    "scheduled_device_action": frozenset({"action_id", "arguments", "requires_confirmation"}),
    "follow_up_task": frozenset({"prompt"}),
    "deferred_diagnostic_result": frozenset({"job_id"}),
}


class SchedulerError(RuntimeError):
    """A bounded scheduler failure that never contains payloads or secrets."""


@dataclass(frozen=True, slots=True)
class TaskSpec:
    task_id: str
    owner_id: str
    kind: str
    natural_description: str
    canonical_payload: dict[str, Any]
    timezone: str
    next_run: int
    recurrence: dict[str, Any]
    enabled: bool
    created_at: int
    updated_at: int
    status: str
    attempts: int
    last_result: dict[str, Any] | None
    delivery_target: str
    delivery_mode: str
    idempotency_key: str
    missed_run_policy: str
    verification_policy: str
    last_run: int | None = None


def _safe_text(value: object, *, field: str, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise SchedulerError(f"{field} is invalid")
    normalized = " ".join(value.split())
    if (
        not 1 <= len(normalized) <= maximum
        or any(ord(character) < 32 for character in normalized)
    ):
        raise SchedulerError(f"{field} is invalid")
    return normalized


def _safe_timezone(value: object) -> str:
    name = _safe_text(value, field="timezone", maximum=64)
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise SchedulerError("timezone is invalid") from error
    return name


def _json_value(value: object, *, depth: int = 0) -> Any:
    if depth > 4:
        raise SchedulerError("canonical payload is too deep")
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str):
            return _safe_text(value, field="canonical payload", maximum=MAX_TEXT)
        return value
    if isinstance(value, list) and len(value) <= 32:
        return [_json_value(item, depth=depth + 1) for item in value]
    if isinstance(value, dict) and len(value) <= 32:
        result: dict[str, Any] = {}
        for key, item in value.items():
            safe_key = _safe_text(key, field="canonical payload key", maximum=64)
            if safe_key.casefold() in FORBIDDEN_PAYLOAD_KEYS:
                raise SchedulerError("canonical payload contains a forbidden field")
            result[safe_key] = _json_value(item, depth=depth + 1)
        return result
    raise SchedulerError("canonical payload is invalid")


def validate_payload(kind: str, value: object) -> dict[str, Any]:
    if kind not in TASK_KINDS or not isinstance(value, dict):
        raise SchedulerError("task payload is invalid")
    payload = _json_value(value)
    assert isinstance(payload, dict)
    if set(payload) != PAYLOAD_FIELDS[kind]:
        raise SchedulerError("task payload fields are invalid")
    if kind == "local_reminder":
        _safe_text(payload["text"], field="reminder text", maximum=300)
    elif kind == "yandex_native_reminder":
        _safe_text(payload["text"], field="reminder text", maximum=300)
        if payload["station"] != "station_max" or payload["backend_status"] not in {
            "completed", "delivery_unknown", "blocked",
        }:
            raise SchedulerError("native reminder payload is invalid")
    elif kind in {"daily_report", "recurring_report", "one_shot_report"}:
        if payload["report"] != "home_status":
            raise SchedulerError("report payload is invalid")
    elif kind == "scheduled_device_action":
        _safe_text(payload["action_id"], field="action id", maximum=128)
        if not isinstance(payload["arguments"], dict) or not isinstance(
            payload["requires_confirmation"], bool
        ):
            raise SchedulerError("device action payload is invalid")
    elif kind == "follow_up_task":
        _safe_text(payload["prompt"], field="follow-up prompt", maximum=300)
    elif kind == "deferred_diagnostic_result":
        _safe_text(payload["job_id"], field="diagnostic job id", maximum=128)
    return payload


def validate_recurrence(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchedulerError("recurrence is invalid")
    recurrence = _json_value(value)
    assert isinstance(recurrence, dict)
    kind = recurrence.get("type")
    if kind == "none" and set(recurrence) == {"type"}:
        return recurrence
    if kind in {"daily", "weekdays"} and set(recurrence) == {"type", "time"}:
        _parse_clock(recurrence["time"])
        return recurrence
    if kind == "weekly" and set(recurrence) == {
        "type", "time", "weekday", "interval_weeks",
    }:
        _parse_clock(recurrence["time"])
        if (
            isinstance(recurrence["weekday"], bool)
            or not isinstance(recurrence["weekday"], int)
            or not 0 <= recurrence["weekday"] <= 6
            or isinstance(recurrence["interval_weeks"], bool)
            or not isinstance(recurrence["interval_weeks"], int)
            or not 1 <= recurrence["interval_weeks"] <= 52
        ):
            raise SchedulerError("weekly recurrence is invalid")
        return recurrence
    if kind == "interval" and set(recurrence) == {"type", "seconds"}:
        seconds = recurrence["seconds"]
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, int)
            or not 60 <= seconds <= 366 * 86400
        ):
            raise SchedulerError("interval recurrence is invalid")
        return recurrence
    raise SchedulerError("recurrence is invalid")


def _parse_clock(value: object) -> tuple[int, int]:
    text = _safe_text(value, field="recurrence time", maximum=5)
    parts = text.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise SchedulerError("recurrence time is invalid")
    hour, minute = map(int, parts)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise SchedulerError("recurrence time is invalid")
    return hour, minute


def next_recurrence(
    recurrence: Mapping[str, Any],
    *,
    timezone: str,
    after_epoch: int,
    previous_epoch: int | None = None,
) -> int | None:
    rule = validate_recurrence(dict(recurrence))
    if rule["type"] == "none":
        return None
    zone = ZoneInfo(_safe_timezone(timezone))
    after = datetime.fromtimestamp(after_epoch, zone)
    if rule["type"] == "interval":
        base = previous_epoch if previous_epoch is not None else after_epoch
        candidate = base + int(rule["seconds"])
        while candidate <= after_epoch:
            candidate += int(rule["seconds"])
        return candidate
    hour, minute = _parse_clock(rule["time"])
    candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if rule["type"] == "daily":
        if candidate <= after:
            candidate += timedelta(days=1)
    elif rule["type"] == "weekdays":
        if candidate <= after:
            candidate += timedelta(days=1)
        while candidate.weekday() > 4:
            candidate += timedelta(days=1)
    else:
        target = int(rule["weekday"])
        days = (target - candidate.weekday()) % 7
        candidate += timedelta(days=days)
        if candidate <= after:
            candidate += timedelta(weeks=int(rule["interval_weeks"]))
    return int(candidate.timestamp())


def _prepare_private_file(path: Path, expected_uid: int) -> None:
    try:
        parent = path.parent.lstat()
    except OSError as error:
        raise SchedulerError("scheduler directory is unavailable") from error
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != expected_uid
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise SchedulerError("scheduler directory is unsafe")
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise SchedulerError("scheduler database is unsafe")
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        os.close(descriptor)
    except OSError as error:
        raise SchedulerError("scheduler database cannot be created") from error


class SchedulerStore:
    def __init__(
        self,
        path: Path = DEFAULT_DB_PATH,
        *,
        expected_uid: int | None = None,
        clock: Callable[[], float] = time.time,
        seed_defaults: bool = True,
    ) -> None:
        self.path = path
        self.expected_uid = os.geteuid() if expected_uid is None else expected_uid
        self.clock = clock
        _prepare_private_file(path, self.expected_uid)
        self._migrate(seed_defaults=seed_defaults)

    def _connect(self) -> sqlite3.Connection:
        _prepare_private_file(self.path, self.expected_uid)
        try:
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=5000")
            return connection
        except sqlite3.Error as error:
            raise SchedulerError("scheduler database connection failed") from error

    def _migrate(self, *, seed_defaults: bool) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS scheduler_meta(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks(
            task_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            natural_description TEXT NOT NULL,
            canonical_payload TEXT NOT NULL,
            timezone TEXT NOT NULL,
            next_run_epoch INTEGER NOT NULL,
            recurrence TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            created_at_epoch INTEGER NOT NULL,
            updated_at_epoch INTEGER NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL,
            last_result TEXT,
            delivery_target TEXT NOT NULL,
            delivery_mode TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            missed_run_policy TEXT NOT NULL,
            verification_policy TEXT NOT NULL,
            last_run_epoch INTEGER,
            lease_until_epoch INTEGER
        );
        CREATE INDEX IF NOT EXISTS tasks_due_idx
            ON tasks(enabled,status,next_run_epoch);
        CREATE INDEX IF NOT EXISTS tasks_owner_idx
            ON tasks(owner_id,updated_at_epoch DESC);
        CREATE TABLE IF NOT EXISTS task_executions(
            execution_key TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id),
            scheduled_epoch INTEGER NOT NULL,
            started_epoch INTEGER NOT NULL,
            finished_epoch INTEGER,
            status TEXT NOT NULL,
            result TEXT,
            UNIQUE(task_id,scheduled_epoch)
        );
        """
        try:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(schema)
                row = connection.execute(
                    "SELECT value FROM scheduler_meta WHERE key='schema_version'"
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO scheduler_meta(key,value) VALUES('schema_version',?)",
                        (str(SCHEMA_VERSION),),
                    )
                elif int(row["value"]) != SCHEMA_VERSION:
                    raise SchedulerError("unsupported scheduler schema version")
                if seed_defaults:
                    self._seed_daily_report(connection)
        except (sqlite3.Error, ValueError) as error:
            if isinstance(error, SchedulerError):
                raise
            raise SchedulerError("scheduler schema migration failed") from error

    def _seed_daily_report(self, connection: sqlite3.Connection) -> None:
        now = int(self.clock())
        recurrence = {"type": "daily", "time": "13:00"}
        next_run = next_recurrence(
            recurrence, timezone=DEFAULT_TIMEZONE, after_epoch=now
        )
        assert next_run is not None
        connection.execute(
            """
            INSERT OR IGNORE INTO tasks(
                task_id,owner_id,kind,natural_description,canonical_payload,
                timezone,next_run_epoch,recurrence,enabled,created_at_epoch,
                updated_at_epoch,status,attempts,last_result,delivery_target,
                delivery_mode,idempotency_key,missed_run_policy,
                verification_policy,last_run_epoch,lease_until_epoch
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                SYSTEM_DAILY_REPORT_ID, DEFAULT_OWNER_ID, "daily_report",
                "Ежедневный отчёт о состоянии дома",
                _dump({"report": "home_status"}), DEFAULT_TIMEZONE, next_run,
                _dump(recurrence), 1, now, now, "scheduled", 0, None,
                "station_max", "tts", SYSTEM_DAILY_REPORT_KEY, "run_once",
                "station_state_transition", None, None,
            ),
        )

    def create_task(
        self,
        *,
        owner_id: str,
        kind: str,
        natural_description: str,
        canonical_payload: Mapping[str, Any],
        timezone: str,
        next_run: int,
        recurrence: Mapping[str, Any],
        delivery_target: str,
        delivery_mode: str,
        missed_run_policy: str,
        verification_policy: str,
        idempotency_key: str | None = None,
        enabled: bool = True,
        status: str = "scheduled",
    ) -> TaskSpec:
        owner = _safe_text(owner_id, field="owner id", maximum=64)
        if kind not in TASK_KINDS:
            raise SchedulerError("task kind is invalid")
        description = _safe_text(
            natural_description, field="natural description", maximum=MAX_TEXT
        )
        payload = validate_payload(kind, dict(canonical_payload))
        zone = _safe_timezone(timezone)
        if isinstance(next_run, bool) or not isinstance(next_run, int) or next_run < 0:
            raise SchedulerError("next run is invalid")
        rule = validate_recurrence(dict(recurrence))
        target = _safe_text(delivery_target, field="delivery target", maximum=128)
        if delivery_mode not in DELIVERY_MODES:
            raise SchedulerError("delivery mode is invalid")
        if missed_run_policy not in MISSED_RUN_POLICIES:
            raise SchedulerError("missed-run policy is invalid")
        if verification_policy not in VERIFICATION_POLICIES:
            raise SchedulerError("verification policy is invalid")
        if status not in TASK_STATUSES or not isinstance(enabled, bool):
            raise SchedulerError("task status is invalid")
        now = int(self.clock())
        key = (
            _safe_text(idempotency_key, field="idempotency key", maximum=128)
            if idempotency_key is not None
            else _idempotency_key(owner, kind, description, payload, next_run, rule)
        )
        task_id = secrets.token_hex(16)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM tasks WHERE idempotency_key=?", (key,)
                ).fetchone()
                if existing is not None:
                    return _row_task(existing)
                connection.execute(
                    """
                    INSERT INTO tasks(
                        task_id,owner_id,kind,natural_description,canonical_payload,
                        timezone,next_run_epoch,recurrence,enabled,created_at_epoch,
                        updated_at_epoch,status,attempts,last_result,delivery_target,
                        delivery_mode,idempotency_key,missed_run_policy,
                        verification_policy,last_run_epoch,lease_until_epoch
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        task_id, owner, kind, description, _dump(payload), zone,
                        next_run, _dump(rule), int(enabled), now, now, status, 0,
                        None, target, delivery_mode, key, missed_run_policy,
                        verification_policy, None, None,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM tasks WHERE task_id=?", (task_id,)
                ).fetchone()
        except sqlite3.Error as error:
            raise SchedulerError("task could not be created") from error
        assert row is not None
        return _row_task(row)

    def get_task(self, task_id: str, *, owner_id: str = DEFAULT_OWNER_ID) -> TaskSpec:
        task = _safe_text(task_id, field="task id", maximum=64)
        owner = _safe_text(owner_id, field="owner id", maximum=64)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id=? AND owner_id=?", (task, owner)
            ).fetchone()
        if row is None:
            raise SchedulerError("task was not found")
        return _row_task(row)

    def list_tasks(
        self,
        *,
        owner_id: str = DEFAULT_OWNER_ID,
        query: str | None = None,
        include_disabled: bool = False,
        limit: int = 20,
    ) -> list[TaskSpec]:
        owner = _safe_text(owner_id, field="owner id", maximum=64)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST_TASKS:
            raise SchedulerError("task list limit is invalid")
        arguments: list[Any] = [owner]
        clauses = ["owner_id=?"]
        if not include_disabled:
            clauses.append("enabled=1")
        if query is not None:
            term = _safe_text(query, field="task query", maximum=128)
            clauses.append("LOWER(natural_description) LIKE ?")
            arguments.append(f"%{term.casefold()}%")
        arguments.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE " + " AND ".join(clauses)
                + " ORDER BY next_run_epoch,created_at_epoch LIMIT ?",
                arguments,
            ).fetchall()
        return [_row_task(row) for row in rows]

    def schedule_overview(
        self, *, owner_id: str = DEFAULT_OWNER_ID
    ) -> tuple[int, TaskSpec | None]:
        """Return an exact enabled count and the nearest task without payload export."""

        owner = _safe_text(owner_id, field="owner id", maximum=64)
        try:
            with self._connect() as connection:
                count_row = connection.execute(
                    "SELECT COUNT(*) AS amount FROM tasks WHERE owner_id=? AND enabled=1",
                    (owner,),
                ).fetchone()
                nearest_row = connection.execute(
                    """
                    SELECT * FROM tasks WHERE owner_id=? AND enabled=1
                    ORDER BY next_run_epoch,created_at_epoch LIMIT 1
                    """,
                    (owner,),
                ).fetchone()
        except sqlite3.Error as error:
            raise SchedulerError("scheduler overview is unavailable") from error
        assert count_row is not None
        return int(count_row["amount"]), (
            _row_task(nearest_row) if nearest_row is not None else None
        )

    def update_task(
        self,
        task_id: str,
        *,
        owner_id: str = DEFAULT_OWNER_ID,
        natural_description: str | None = None,
        canonical_payload: Mapping[str, Any] | None = None,
        timezone: str | None = None,
        next_run: int | None = None,
        recurrence: Mapping[str, Any] | None = None,
        enabled: bool | None = None,
    ) -> TaskSpec:
        current = self.get_task(task_id, owner_id=owner_id)
        if current.kind == "yandex_native_reminder":
            raise SchedulerError(
                "native Yandex reminders cannot be changed without a confirmed API"
            )
        description = (
            current.natural_description if natural_description is None
            else _safe_text(natural_description, field="natural description")
        )
        payload = (
            current.canonical_payload if canonical_payload is None
            else validate_payload(current.kind, dict(canonical_payload))
        )
        zone = current.timezone if timezone is None else _safe_timezone(timezone)
        due = current.next_run if next_run is None else next_run
        if isinstance(due, bool) or not isinstance(due, int) or due < 0:
            raise SchedulerError("next run is invalid")
        rule = current.recurrence if recurrence is None else validate_recurrence(dict(recurrence))
        active = current.enabled if enabled is None else enabled
        if not isinstance(active, bool):
            raise SchedulerError("enabled state is invalid")
        now = int(self.clock())
        status = "scheduled" if active else "cancelled"
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT status FROM tasks WHERE task_id=? AND owner_id=?",
                    (current.task_id, current.owner_id),
                ).fetchone()
                if row is None or row["status"] == "running":
                    raise SchedulerError("task cannot be changed while running")
                connection.execute(
                    """
                    UPDATE tasks SET natural_description=?,canonical_payload=?,
                        timezone=?,next_run_epoch=?,recurrence=?,enabled=?,
                        updated_at_epoch=?,status=?,lease_until_epoch=NULL
                    WHERE task_id=? AND owner_id=?
                    """,
                    (
                        description, _dump(payload), zone, due, _dump(rule),
                        int(active), now, status, current.task_id, current.owner_id,
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM tasks WHERE task_id=?", (current.task_id,)
                ).fetchone()
        except sqlite3.Error as error:
            raise SchedulerError("task could not be updated") from error
        assert updated is not None
        return _row_task(updated)

    def cancel_task(self, task_id: str, *, owner_id: str = DEFAULT_OWNER_ID) -> TaskSpec:
        current = self.get_task(task_id, owner_id=owner_id)
        if current.kind == "yandex_native_reminder":
            raise SchedulerError(
                "native Yandex reminder remains in the Station; cancellation API is unconfirmed"
            )
        return self.update_task(task_id, owner_id=owner_id, enabled=False)

    def claim_due(self, *, now: int | None = None, limit: int = 16) -> list[tuple[TaskSpec, str, int]]:
        observed = int(self.clock()) if now is None else now
        if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
            raise SchedulerError("scheduler clock is invalid")
        claimed: list[tuple[TaskSpec, str, int]] = []
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._expire_leases(connection, observed)
                self._skip_missed(connection, observed)
                rows = connection.execute(
                    """
                    SELECT * FROM tasks
                    WHERE enabled=1 AND status IN ('scheduled','failed')
                      AND next_run_epoch<=?
                    ORDER BY next_run_epoch,created_at_epoch LIMIT ?
                    """,
                    (observed, limit),
                ).fetchall()
                for row in rows:
                    task = _row_task(row)
                    scheduled = task.next_run
                    execution_key = hashlib.sha256(
                        f"{task.task_id}:{scheduled}".encode("ascii")
                    ).hexdigest()
                    try:
                        connection.execute(
                            """
                            INSERT INTO task_executions(
                                execution_key,task_id,scheduled_epoch,started_epoch,status
                            ) VALUES(?,?,?,?,?)
                            """,
                            (execution_key, task.task_id, scheduled, observed, "running"),
                        )
                    except sqlite3.IntegrityError:
                        continue
                    connection.execute(
                        """
                        UPDATE tasks SET status='running',attempts=attempts+1,
                            updated_at_epoch=?,lease_until_epoch=? WHERE task_id=?
                        """,
                        (observed, observed + LEASE_SECONDS, task.task_id),
                    )
                    updated = connection.execute(
                        "SELECT * FROM tasks WHERE task_id=?", (task.task_id,)
                    ).fetchone()
                    assert updated is not None
                    claimed.append((_row_task(updated), execution_key, scheduled))
        except sqlite3.Error as error:
            raise SchedulerError("due tasks could not be claimed") from error
        return claimed

    def _skip_missed(self, connection: sqlite3.Connection, now: int) -> None:
        """Apply the persisted skip policy without calling an action executor."""

        rows = connection.execute(
            """
            SELECT * FROM tasks
            WHERE enabled=1 AND status IN ('scheduled','failed')
              AND missed_run_policy='skip' AND next_run_epoch<?
            ORDER BY next_run_epoch,created_at_epoch
            """,
            (now - MISSED_RUN_GRACE_SECONDS,),
        ).fetchall()
        for row in rows:
            task = _row_task(row)
            scheduled = task.next_run
            following = next_recurrence(
                task.recurrence,
                timezone=task.timezone,
                after_epoch=now,
                previous_epoch=scheduled,
            )
            result = {
                "status": "missed",
                "reason": "missed_run_policy_skip",
                "scheduled_epoch": scheduled,
                "observed_epoch": now,
            }
            execution_key = hashlib.sha256(
                f"{task.task_id}:{scheduled}".encode("ascii")
            ).hexdigest()
            connection.execute(
                """
                INSERT OR IGNORE INTO task_executions(
                    execution_key,task_id,scheduled_epoch,started_epoch,
                    finished_epoch,status,result
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    execution_key, task.task_id, scheduled, now, now,
                    "missed", _dump(result),
                ),
            )
            connection.execute(
                """
                UPDATE tasks SET enabled=?,next_run_epoch=?,updated_at_epoch=?,
                    status=?,last_result=?,lease_until_epoch=NULL
                WHERE task_id=?
                """,
                (
                    int(following is not None),
                    following if following is not None else scheduled,
                    now,
                    "scheduled" if following is not None else "missed",
                    _dump(result),
                    task.task_id,
                ),
            )

    def _expire_leases(self, connection: sqlite3.Connection, now: int) -> None:
        rows = connection.execute(
            "SELECT * FROM tasks WHERE status='running' AND lease_until_epoch<?", (now,)
        ).fetchall()
        for row in rows:
            task = _row_task(row)
            result = {"status": "delivery_unknown", "reason": "worker_lease_expired"}
            next_run = next_recurrence(
                task.recurrence,
                timezone=task.timezone,
                after_epoch=now,
                previous_epoch=task.next_run,
            )
            enabled = next_run is not None
            connection.execute(
                """
                UPDATE tasks SET status=?,enabled=?,next_run_epoch=?,last_result=?,
                    updated_at_epoch=?,last_run_epoch=?,lease_until_epoch=NULL
                WHERE task_id=?
                """,
                (
                    "scheduled" if enabled else "delivery_unknown", int(enabled),
                    next_run if next_run is not None else task.next_run,
                    _dump(result), now, task.next_run, task.task_id,
                ),
            )
            connection.execute(
                """
                UPDATE task_executions SET status='delivery_unknown',finished_epoch=?,
                    result=? WHERE task_id=? AND status='running'
                """,
                (now, _dump(result), task.task_id),
            )

    def finish_execution(
        self,
        task: TaskSpec,
        execution_key: str,
        scheduled_epoch: int,
        result: Mapping[str, Any],
        *,
        now: int | None = None,
    ) -> TaskSpec:
        observed = int(self.clock()) if now is None else now
        safe_result = _json_value(dict(result))
        assert isinstance(safe_result, dict)
        outcome = safe_result.get("status")
        if outcome not in {"verified", "completed", "failed", "blocked", "delivery_unknown"}:
            raise SchedulerError("task result status is invalid")
        following = next_recurrence(
            task.recurrence,
            timezone=task.timezone,
            after_epoch=observed,
            previous_epoch=scheduled_epoch,
        )
        if (
            task.kind in {"daily_report", "recurring_report", "one_shot_report"}
            and outcome == "failed"
        ):
            previous_origin = (
                task.last_result.get("retry_origin_epoch")
                if isinstance(task.last_result, dict) else None
            )
            origin = (
                int(previous_origin)
                if isinstance(previous_origin, int)
                and not isinstance(previous_origin, bool)
                else scheduled_epoch
            )
            retry_deadline = origin + REPORT_RETRY_WINDOW_SECONDS
            if observed < retry_deadline:
                following = min(observed + REPORT_RETRY_SECONDS, retry_deadline)
                safe_result["retry_origin_epoch"] = origin
                safe_result["retry_due_epoch"] = following
        raw = _dump(safe_result)
        if len(raw.encode("utf-8")) > MAX_RESULT_BYTES:
            raise SchedulerError("task result is too large")
        enabled = following is not None
        if enabled:
            final_status = "scheduled"
        elif outcome == "verified":
            final_status = "completed"
        elif outcome == "completed" and task.verification_policy == "none":
            final_status = "completed"
        elif outcome == "completed":
            final_status = "failed"
        else:
            final_status = str(outcome)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                execution = connection.execute(
                    "SELECT status FROM task_executions WHERE execution_key=? AND task_id=?",
                    (execution_key, task.task_id),
                ).fetchone()
                if execution is None or execution["status"] != "running":
                    raise SchedulerError("task execution is no longer claimable")
                connection.execute(
                    """
                    UPDATE task_executions SET finished_epoch=?,status=?,result=?
                    WHERE execution_key=?
                    """,
                    (observed, str(outcome), raw, execution_key),
                )
                connection.execute(
                    """
                    UPDATE tasks SET enabled=?,next_run_epoch=?,updated_at_epoch=?,
                        status=?,last_result=?,last_run_epoch=?,lease_until_epoch=NULL
                    WHERE task_id=?
                    """,
                    (
                        int(enabled), following if following is not None else scheduled_epoch,
                        observed, final_status, raw, scheduled_epoch, task.task_id,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM tasks WHERE task_id=?", (task.task_id,)
                ).fetchone()
        except sqlite3.Error as error:
            raise SchedulerError("task execution could not be finalized") from error
        assert row is not None
        return _row_task(row)

    def execution_count(self, task_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS amount FROM task_executions WHERE task_id=?",
                (task_id,),
            ).fetchone()
        return int(row["amount"])


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _idempotency_key(
    owner: str,
    kind: str,
    description: str,
    payload: Mapping[str, Any],
    next_run: int,
    recurrence: Mapping[str, Any],
) -> str:
    seed = _dump({
        "owner": owner, "kind": kind, "description": description.casefold(),
        "payload": payload, "next_run": next_run, "recurrence": recurrence,
    })
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _row_task(row: sqlite3.Row) -> TaskSpec:
    try:
        payload = json.loads(row["canonical_payload"])
        recurrence = json.loads(row["recurrence"])
        last_result = json.loads(row["last_result"]) if row["last_result"] else None
        task = TaskSpec(
            task_id=str(row["task_id"]), owner_id=str(row["owner_id"]),
            kind=str(row["kind"]), natural_description=str(row["natural_description"]),
            canonical_payload=validate_payload(str(row["kind"]), payload),
            timezone=_safe_timezone(row["timezone"]), next_run=int(row["next_run_epoch"]),
            recurrence=validate_recurrence(recurrence), enabled=bool(row["enabled"]),
            created_at=int(row["created_at_epoch"]), updated_at=int(row["updated_at_epoch"]),
            status=str(row["status"]), attempts=int(row["attempts"]),
            last_result=last_result, delivery_target=str(row["delivery_target"]),
            delivery_mode=str(row["delivery_mode"]), idempotency_key=str(row["idempotency_key"]),
            missed_run_policy=str(row["missed_run_policy"]),
            verification_policy=str(row["verification_policy"]),
            last_run=int(row["last_run_epoch"]) if row["last_run_epoch"] is not None else None,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise SchedulerError("stored task is invalid") from error
    if (
        task.kind not in TASK_KINDS or task.status not in TASK_STATUSES
        or task.delivery_mode not in DELIVERY_MODES
        or task.missed_run_policy not in MISSED_RUN_POLICIES
        or task.verification_policy not in VERIFICATION_POLICIES
    ):
        raise SchedulerError("stored task is invalid")
    return task


def task_tool_definitions() -> list[dict[str, Any]]:
    no_extra = {"additionalProperties": False}
    payload_schema = canonical_payload_tool_schema()
    recurrence_schema = recurrence_tool_schema()
    return [
        {
            "type": "function",
            "function": {
                "name": "task_create",
                "description": "Create one validated persistent task.",
                "parameters": {
                    "type": "object", **no_extra,
                    "required": [
                        "kind", "natural_description", "canonical_payload", "timezone",
                        "next_run", "recurrence", "delivery_target", "delivery_mode",
                        "missed_run_policy", "verification_policy",
                    ],
                    "properties": {
                        "kind": {"type": "string", "enum": sorted(TASK_KINDS)},
                        "natural_description": {"type": "string", "maxLength": MAX_TEXT},
                        "canonical_payload": payload_schema,
                        "timezone": {"type": "string", "maxLength": 64},
                        "next_run": {"type": "integer", "minimum": 0},
                        "recurrence": recurrence_schema,
                        "delivery_target": {"type": "string", "maxLength": 128},
                        "delivery_mode": {"type": "string", "enum": sorted(DELIVERY_MODES)},
                        "missed_run_policy": {"type": "string", "enum": sorted(MISSED_RUN_POLICIES)},
                        "verification_policy": {"type": "string", "enum": sorted(VERIFICATION_POLICIES)},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "task_update", "description": "Update one local task.",
                "parameters": {
                    "type": "object", **no_extra, "required": ["task_id", "changes"],
                    "properties": {
                        "task_id": {"type": "string", "maxLength": 64},
                        "changes": {
                            "type": "object", "additionalProperties": False,
                            "properties": {
                                "natural_description": {"type": "string", "maxLength": MAX_TEXT},
                                "canonical_payload": payload_schema,
                                "timezone": {"type": "string", "maxLength": 64},
                                "next_run": {"type": "integer", "minimum": 0},
                                "recurrence": recurrence_schema,
                                "enabled": {"type": "boolean"},
                            },
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "task_cancel", "description": "Cancel one local task.",
                "parameters": {
                    "type": "object", **no_extra, "required": ["task_id"],
                    "properties": {"task_id": {"type": "string", "maxLength": 64}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "task_list", "description": "List persistent tasks.",
                "parameters": {
                    "type": "object", **no_extra, "properties": {
                        "query": {"type": "string", "maxLength": 128},
                        "include_disabled": {"type": "boolean"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_TASKS},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "task_get", "description": "Get one persistent task.",
                "parameters": {
                    "type": "object", **no_extra, "required": ["task_id"],
                    "properties": {"task_id": {"type": "string", "maxLength": 64}},
                },
            },
        },
    ]


def canonical_payload_tool_schema() -> dict[str, Any]:
    """Closed model-facing payload union; capability args arrive in Phase 7."""

    def closed(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": required,
            "properties": properties,
        }

    return {
        "anyOf": [
            closed(
                {"text": {"type": "string", "minLength": 1, "maxLength": 300}},
                ["text"],
            ),
            closed(
                {
                    "text": {"type": "string", "minLength": 1, "maxLength": 300},
                    "station": {"const": "station_max"},
                    "backend_status": {
                        "type": "string",
                        "enum": ["completed", "delivery_unknown", "blocked"],
                    },
                },
                ["text", "station", "backend_status"],
            ),
            closed({"report": {"const": "home_status"}}, ["report"]),
            closed(
                {
                    "action_id": {
                        "type": "string", "minLength": 1, "maxLength": 128
                    },
                    "arguments": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {},
                    },
                    "requires_confirmation": {"type": "boolean"},
                },
                ["action_id", "arguments", "requires_confirmation"],
            ),
            closed(
                {"prompt": {"type": "string", "minLength": 1, "maxLength": 300}},
                ["prompt"],
            ),
            closed(
                {"job_id": {"type": "string", "minLength": 1, "maxLength": 128}},
                ["job_id"],
            ),
        ]
    }


def recurrence_tool_schema() -> dict[str, Any]:
    """Closed recurrence union shared by tools and structured parsing."""

    def closed(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": required,
            "properties": properties,
        }

    clock = {
        "type": "string",
        "pattern": "^(?:[01][0-9]|2[0-3]):[0-5][0-9]$",
    }
    return {
        "anyOf": [
            closed({"type": {"const": "none"}}, ["type"]),
            closed(
                {"type": {"const": "daily"}, "time": clock},
                ["type", "time"],
            ),
            closed(
                {"type": {"const": "weekdays"}, "time": clock},
                ["type", "time"],
            ),
            closed(
                {
                    "type": {"const": "weekly"},
                    "time": clock,
                    "weekday": {"type": "integer", "minimum": 0, "maximum": 6},
                    "interval_weeks": {
                        "type": "integer", "minimum": 1, "maximum": 52
                    },
                },
                ["type", "time", "weekday", "interval_weeks"],
            ),
            closed(
                {
                    "type": {"const": "interval"},
                    "seconds": {
                        "type": "integer", "minimum": 60, "maximum": 31622400
                    },
                },
                ["type", "seconds"],
            ),
        ]
    }


def execute_task_tool(
    name: str,
    arguments: Mapping[str, Any],
    *,
    store: SchedulerStore,
    owner_id: str = DEFAULT_OWNER_ID,
) -> dict[str, Any]:
    if name == "task_create":
        task = store.create_task(owner_id=owner_id, **dict(arguments))
        return {"status": "created", "task": task_public(task)}
    if name == "task_update":
        if set(arguments) != {"task_id", "changes"} or not isinstance(arguments["changes"], dict):
            raise SchedulerError("task_update arguments are invalid")
        task = store.update_task(
            str(arguments["task_id"]), owner_id=owner_id, **dict(arguments["changes"])
        )
        return {"status": "updated", "task": task_public(task)}
    if name == "task_cancel":
        if set(arguments) != {"task_id"}:
            raise SchedulerError("task_cancel arguments are invalid")
        task = store.cancel_task(str(arguments["task_id"]), owner_id=owner_id)
        return {"status": "cancelled", "task": task_public(task)}
    if name == "task_list":
        if not set(arguments).issubset({"query", "include_disabled", "limit"}):
            raise SchedulerError("task_list arguments are invalid")
        tasks = store.list_tasks(owner_id=owner_id, **dict(arguments))
        return {"status": "listed", "tasks": [task_public(task) for task in tasks]}
    if name == "task_get":
        if set(arguments) != {"task_id"}:
            raise SchedulerError("task_get arguments are invalid")
        task = store.get_task(str(arguments["task_id"]), owner_id=owner_id)
        return {"status": "found", "task": task_public(task)}
    raise SchedulerError("scheduler tool is not allow-listed")


def task_public(task: TaskSpec) -> dict[str, Any]:
    document = asdict(task)
    document.pop("idempotency_key", None)
    return document


def _local_reminder(task: TaskSpec) -> dict[str, Any]:
    snapshot, exit_code = ha_read.execute_safely("snapshot")
    if exit_code != 0:
        return {"status": "failed", "verification": "ha_unavailable"}
    try:
        speaker = ha_notify.choose_speaker(
            snapshot, required_speaker=ha_notify.FALLBACK_SPEAKER
        )
        config = ha_read.load_config()
        baseline = daily_voice_report.read_speaker_state(config, speaker)
        if baseline.get("muted") is True or baseline.get("volume_ready") is not True:
            return {"status": "blocked", "verification": "speaker_not_audible"}
        ha_notify.post_tts(config, speaker, str(task.canonical_payload["text"]))
        verified = daily_voice_report.verify_speaker_transition(config, speaker, baseline)
        return {
            "status": "verified" if verified else "delivery_unknown",
            "verification": (
                "ha_speaker_state_transition_observed"
                if verified else "ha_service_accepted_without_speaker_transition"
            ),
        }
    except ha_notify.NotifyDeliveryUnknown:
        return {"status": "delivery_unknown", "verification": "transport_unknown"}
    except (ha_notify.NotifyError, ha_read.AdapterError, daily_voice_report.DailyReportError):
        return {"status": "failed", "verification": "not_sent"}


def execute_task(task: TaskSpec) -> dict[str, Any]:
    if task.kind == "local_reminder":
        return _local_reminder(task)
    if task.kind in {"daily_report", "recurring_report", "one_shot_report"}:
        delayed = int(time.time()) > task.next_run + 15 * 60
        try:
            result = daily_voice_report.execute(live=True, delayed=delayed)
            return {
                "status": "verified" if result.get("status") == "verified" else "delivery_unknown",
                "verification": result.get("verification", "not_sent"),
            }
        except ha_notify.NotifyDeliveryUnknown:
            return {"status": "delivery_unknown", "verification": "transport_unknown"}
        except (daily_voice_report.DailyReportError, ha_notify.NotifyError, ha_read.AdapterError):
            return {"status": "failed", "verification": "not_sent"}
    return {"status": "blocked", "verification": "executor_not_available"}


def run_due(
    store: SchedulerStore,
    *,
    now: int | None = None,
    executor: Callable[[TaskSpec], Mapping[str, Any]] = execute_task,
    status_path: Path | None = DEFAULT_STATUS_PATH,
) -> list[TaskSpec]:
    observed = int(store.clock()) if now is None else now
    completed: list[TaskSpec] = []
    for task, execution_key, scheduled in store.claim_due(now=observed):
        try:
            result = executor(task)
        except Exception:  # fail closed at the task boundary; never expose payloads
            result = {"status": "failed", "verification": "worker_error"}
        completed.append(
            store.finish_execution(task, execution_key, scheduled, result, now=observed)
        )
    if status_path is not None:
        export_status(store, path=status_path, now=observed)
    return completed


def scheduler_status(store: SchedulerStore, *, now: int | None = None) -> dict[str, Any]:
    observed = int(store.clock()) if now is None else now
    enabled_count, nearest = store.schedule_overview()
    try:
        daily = store.get_task(SYSTEM_DAILY_REPORT_ID)
    except SchedulerError:
        daily = None
    if daily is None:
        daily_document = {
            "task_id": SYSTEM_DAILY_REPORT_ID,
            "state": "unavailable",
            "next_run_epoch": None,
            "last_run_epoch": None,
            "attempts": 0,
            "verification": "not_run",
        }
    else:
        if daily.status == "running":
            daily_state = "running"
        elif daily.enabled and observed >= daily.next_run:
            daily_state = (
                "retrying"
                if observed <= daily.next_run + REPORT_RETRY_WINDOW_SECONDS
                else "missed"
            )
        elif daily.last_result and daily.last_result.get("status") == "failed":
            daily_state = "retrying"
        elif daily.last_result and daily.last_result.get("status") == "verified":
            daily_state = "verified"
        else:
            daily_state = "not_due"
        daily_document = {
            "task_id": daily.task_id,
            "state": daily_state,
            "next_run_epoch": daily.next_run,
            "last_run_epoch": daily.last_run,
            "attempts": daily.attempts,
            "verification": (
                daily.last_result.get("verification")
                if isinstance(daily.last_result, dict) else "not_run"
            ),
        }
    return {
        "schema_version": 1,
        "observed_epoch": observed,
        "database_schema": SCHEMA_VERSION,
        "enabled_task_count": enabled_count,
        "next_run_epoch": nearest.next_run if nearest is not None else None,
        "next_task_kind": nearest.kind if nearest is not None else None,
        "wake_epoch": max(observed, nearest.next_run - 120) if nearest is not None else None,
        "daily_report": daily_document,
    }


def export_status(
    store: SchedulerStore,
    *,
    path: Path = DEFAULT_STATUS_PATH,
    now: int | None = None,
) -> dict[str, Any]:
    document = scheduler_status(store, now=now)
    heartbeat._validate_state_dir(path.parent)
    heartbeat._atomic_write(
        path, json.dumps(document, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    )
    return document


def read_daily_report_status(
    *, store: SchedulerStore | None = None, now: int | None = None
) -> dict[str, Any]:
    database = SchedulerStore() if store is None else store
    return scheduler_status(database, now=now)["daily_report"]


def migrate_legacy_reminder_document(
    store: SchedulerStore, document: object
) -> TaskSpec | None:
    """Import one validated legacy record without modifying the source artifact."""

    if not isinstance(document, dict) or document.get("schema_version") != 2:
        return None
    due = document.get("due_at")
    fingerprint = document.get("fingerprint")
    text = document.get("reminder_text")
    status = document.get("status")
    if (
        not isinstance(due, str) or not isinstance(fingerprint, str)
        or not isinstance(text, str) or status not in {
            "completed", "delivery_unknown", "blocked", "command_acknowledged",
        }
    ):
        return None
    try:
        due_epoch = int(datetime.fromisoformat(due).timestamp())
    except ValueError:
        return None
    backend_status = "completed" if status in {"completed", "command_acknowledged"} else status
    return store.create_task(
        owner_id=DEFAULT_OWNER_ID,
        kind="yandex_native_reminder",
        natural_description=f"Напоминание: {text}",
        canonical_payload={
            "text": text, "station": "station_max", "backend_status": backend_status,
        },
        timezone=str(document.get("timezone", DEFAULT_TIMEZONE)),
        next_run=due_epoch,
        recurrence={"type": "none"},
        enabled=False,
        status="external_managed" if backend_status == "completed" else backend_status,
        delivery_target="station_max",
        delivery_mode="yandex_native",
        idempotency_key=f"legacy:yandex:{fingerprint}",
        missed_run_policy="skip",
        verification_policy="yandex_success",
    )


def migrate_legacy_reminder(store: SchedulerStore) -> TaskSpec | None:
    try:
        result = model_workspace.read_text("notes/LAST-REMINDER.json")
        document = json.loads(str(result.get("content", "")))
    except (model_workspace.WorkspaceError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return migrate_legacy_reminder_document(store, document)


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tick", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--wake-json", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.tick and not arguments.live:
        print('{"schema_version":1,"status":"live_flag_required"}')
        return 2
    try:
        store = SchedulerStore()
        migrate_legacy_reminder(store)
        if arguments.tick:
            completed = run_due(store)
            result = {"schema_version": 1, "status": "ok", "executed": len(completed)}
        else:
            status = export_status(store)
            if arguments.wake_json:
                result = {
                    "schema_version": 1,
                    "next_run_epoch": status["next_run_epoch"],
                    "wake_epoch": status["wake_epoch"],
                }
            else:
                result = status
    except (SchedulerError, heartbeat.HeartbeatError, OSError):
        print('{"schema_version":1,"status":"unavailable"}')
        return 3
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
