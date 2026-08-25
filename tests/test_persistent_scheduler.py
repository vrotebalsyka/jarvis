#!/usr/bin/env python3
"""Contracts for the single persistent Home Butler scheduler."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import persistent_scheduler as scheduler  # noqa: E402


ZONE = ZoneInfo(scheduler.DEFAULT_TIMEZONE)
NOW = int(datetime(2026, 8, 24, 10, 0, tzinfo=ZONE).timestamp())


class SchedulerStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.directory.chmod(0o700)
        self.path = self.directory / "scheduler.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def store(self, *, now: int = NOW, seed: bool = True) -> scheduler.SchedulerStore:
        return scheduler.SchedulerStore(
            self.path, clock=lambda: now, seed_defaults=seed
        )

    @staticmethod
    def create_local(
        store: scheduler.SchedulerStore,
        *,
        due: int = NOW + 60,
        description: str = "Напоминание: купить фильтр",
        key: str | None = None,
    ) -> scheduler.TaskSpec:
        return store.create_task(
            owner_id=scheduler.DEFAULT_OWNER_ID,
            kind="local_reminder",
            natural_description=description,
            canonical_payload={"text": "купить фильтр"},
            timezone=scheduler.DEFAULT_TIMEZONE,
            next_run=due,
            recurrence={"type": "none"},
            delivery_target="station_max",
            delivery_mode="tts",
            idempotency_key=key,
            missed_run_policy="run_once",
            verification_policy="station_state_transition",
        )

    def test_schema_is_private_versioned_and_seeds_daily_report_in_database(self) -> None:
        store = self.store()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        task = store.get_task(scheduler.SYSTEM_DAILY_REPORT_ID)
        self.assertEqual(task.kind, "daily_report")
        self.assertEqual(task.recurrence, {"type": "daily", "time": "13:00"})
        self.assertEqual(
            datetime.fromtimestamp(task.next_run, ZONE).strftime("%Y-%m-%d %H:%M"),
            "2026-08-24 13:00",
        )
        with store._connect() as connection:
            version = connection.execute(
                "SELECT value FROM scheduler_meta WHERE key='schema_version'"
            ).fetchone()[0]
        self.assertEqual(version, "1")

    def test_daily_report_time_literal_exists_only_in_scheduler_seed(self) -> None:
        """Prevent systemd, supervisor, chat or wake helpers becoming a second clock."""
        production_roots = (
            PROJECT_DIR / "scripts",
            PROJECT_DIR / "config" / "systemd",
        )
        suffixes = {".py", ".sh", ".ps1", ".service", ".timer", ".path"}
        occurrences: list[tuple[str, str]] = []
        for root in production_roots:
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix not in suffixes:
                    continue
                for line in path.read_text().splitlines():
                    if "13:00" in line:
                        occurrences.append(
                            (str(path.relative_to(PROJECT_DIR)), line.strip())
                        )
        self.assertEqual(
            occurrences,
            [
                (
                    "scripts/persistent_scheduler.py",
                    'recurrence = {"type": "daily", "time": "13:00"}',
                )
            ],
        )

    def test_every_required_task_kind_has_a_closed_canonical_payload(self) -> None:
        store = self.store(seed=False)
        samples = {
            "local_reminder": ({"text": "проверить фильтр"}, "tts", "station_state_transition"),
            "yandex_native_reminder": (
                {"text": "проверить фильтр", "station": "station_max", "backend_status": "completed"},
                "yandex_native", "yandex_success",
            ),
            "daily_report": ({"report": "home_status"}, "tts", "station_state_transition"),
            "recurring_report": ({"report": "home_status"}, "tts", "station_state_transition"),
            "one_shot_report": ({"report": "home_status"}, "tts", "station_state_transition"),
            "scheduled_device_action": (
                {"action_id": "capability-1", "arguments": {"value": 1}, "requires_confirmation": True},
                "ha_control", "ha_state_readback",
            ),
            "follow_up_task": ({"prompt": "проверить результат"}, "workspace", "none"),
            "deferred_diagnostic_result": ({"job_id": "diagnostic-1"}, "workspace", "none"),
        }
        for index, (kind, (payload, mode, verification)) in enumerate(samples.items()):
            with self.subTest(kind=kind):
                task = store.create_task(
                    owner_id="owner",
                    kind=kind,
                    natural_description=f"Задача {kind}",
                    canonical_payload=payload,
                    timezone=scheduler.DEFAULT_TIMEZONE,
                    next_run=NOW + 100 + index,
                    recurrence={"type": "none"},
                    enabled=kind != "yandex_native_reminder",
                    status="external_managed" if kind == "yandex_native_reminder" else "scheduled",
                    delivery_target="station_max" if mode in {"tts", "yandex_native"} else "local",
                    delivery_mode=mode,
                    missed_run_policy="run_once",
                    verification_policy=verification,
                )
                self.assertEqual(task.kind, kind)
        self.assertEqual(len(store.list_tasks(include_disabled=True, limit=20)), len(samples))

    def test_payload_cannot_smuggle_shell_service_or_executable(self) -> None:
        store = self.store(seed=False)
        for arguments in (
            {"command": "bash"},
            {"service_path": "/api/services/homeassistant/restart"},
            {"nested": {"executable": "cmd.exe"}},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(scheduler.SchedulerError):
                store.create_task(
                    owner_id="owner",
                    kind="scheduled_device_action",
                    natural_description="Небезопасная задача",
                    canonical_payload={
                        "action_id": "capability-1",
                        "arguments": arguments,
                        "requires_confirmation": True,
                    },
                    timezone=scheduler.DEFAULT_TIMEZONE,
                    next_run=NOW + 60,
                    recurrence={"type": "none"},
                    delivery_target="home_assistant",
                    delivery_mode="ha_control",
                    missed_run_policy="run_once",
                    verification_policy="ha_state_readback",
                )

    def test_tool_schemas_are_closed_and_crud_uses_only_validated_fields(self) -> None:
        definitions = scheduler.task_tool_definitions()
        self.assertEqual(
            {item["function"]["name"] for item in definitions},
            {"task_create", "task_update", "task_cancel", "task_list", "task_get"},
        )
        encoded = json.dumps(definitions)
        for forbidden in ("shell", "service_path", "executable", "argv"):
            self.assertNotIn(forbidden, encoded)
        for definition in definitions:
            self.assertFalse(
                definition["function"]["parameters"]["additionalProperties"]
            )
        encoded_payload = definitions[0]["function"]["parameters"]["properties"]
        for union_name in ("canonical_payload", "recurrence"):
            for variant in encoded_payload[union_name]["anyOf"]:
                self.assertFalse(variant["additionalProperties"])
        action_variant = next(
            item for item in encoded_payload["canonical_payload"]["anyOf"]
            if "arguments" in item["properties"]
        )
        self.assertFalse(
            action_variant["properties"]["arguments"]["additionalProperties"]
        )

    def test_idempotency_returns_same_task_without_duplicate_row(self) -> None:
        store = self.store(seed=False)
        first = self.create_local(store, key="same-request")
        second = self.create_local(store, key="same-request")
        self.assertEqual(first.task_id, second.task_id)
        self.assertEqual(len(store.list_tasks()), 1)

    def test_scheduler_restart_executes_one_shot_exactly_once(self) -> None:
        first_store = self.store(seed=False)
        task = self.create_local(first_store, due=NOW + 10)
        del first_store

        restarted = self.store(now=NOW + 10, seed=False)
        executor = mock.Mock(return_value={
            "status": "verified",
            "verification": "ha_speaker_state_transition_observed",
        })
        completed = scheduler.run_due(
            restarted, now=NOW + 10, executor=executor, status_path=None
        )
        again = scheduler.run_due(
            restarted, now=NOW + 20, executor=executor, status_path=None
        )

        self.assertEqual(len(completed), 1)
        self.assertEqual(again, [])
        executor.assert_called_once()
        self.assertEqual(restarted.execution_count(task.task_id), 1)
        stored = restarted.get_task(task.task_id)
        self.assertFalse(stored.enabled)
        self.assertEqual(stored.status, "completed")

    def test_expired_worker_lease_never_repeats_uncertain_delivery(self) -> None:
        store = self.store(seed=False)
        task = self.create_local(store, due=NOW)
        claims = store.claim_due(now=NOW)
        self.assertEqual(len(claims), 1)
        recovered = scheduler.SchedulerStore(
            self.path, clock=lambda: NOW + scheduler.LEASE_SECONDS + 1,
            seed_defaults=False,
        )
        self.assertEqual(
            recovered.claim_due(now=NOW + scheduler.LEASE_SECONDS + 1), []
        )
        stored = recovered.get_task(task.task_id)
        self.assertEqual(stored.status, "delivery_unknown")
        self.assertFalse(stored.enabled)
        self.assertEqual(recovered.execution_count(task.task_id), 1)

    def test_missed_skip_policy_records_miss_without_calling_executor(self) -> None:
        store = self.store(seed=False)
        task = store.create_task(
            owner_id="owner",
            kind="local_reminder",
            natural_description="Просроченное напоминание",
            canonical_payload={"text": "проверить фильтр"},
            timezone=scheduler.DEFAULT_TIMEZONE,
            next_run=NOW - scheduler.MISSED_RUN_GRACE_SECONDS - 1,
            recurrence={"type": "none"},
            delivery_target="station_max",
            delivery_mode="tts",
            missed_run_policy="skip",
            verification_policy="station_state_transition",
        )
        executor = mock.Mock()
        self.assertEqual(
            scheduler.run_due(store, now=NOW, executor=executor, status_path=None),
            [],
        )
        executor.assert_not_called()
        stored = store.get_task(task.task_id)
        self.assertFalse(stored.enabled)
        self.assertEqual(stored.status, "missed")
        self.assertEqual(stored.last_result["reason"], "missed_run_policy_skip")

    def test_overdue_run_once_policy_executes_once(self) -> None:
        store = self.store(seed=False)
        task = self.create_local(
            store, due=NOW - scheduler.MISSED_RUN_GRACE_SECONDS - 100
        )
        executor = mock.Mock(
            return_value={
                "status": "verified",
                "verification": "ha_speaker_state_transition_observed",
            }
        )
        completed = scheduler.run_due(
            store, now=NOW, executor=executor, status_path=None
        )
        self.assertEqual(len(completed), 1)
        executor.assert_called_once()
        self.assertEqual(store.get_task(task.task_id).status, "completed")

    def test_failed_daily_report_retries_inside_window_then_advances(self) -> None:
        store = self.store()
        store.update_task(
            scheduler.SYSTEM_DAILY_REPORT_ID,
            next_run=NOW,
            recurrence={"type": "daily", "time": "10:00"},
        )
        first = scheduler.run_due(
            store,
            now=NOW,
            executor=lambda _task: {
                "status": "failed", "verification": "not_sent"
            },
            status_path=None,
        )
        self.assertEqual(len(first), 1)
        retry = store.get_task(scheduler.SYSTEM_DAILY_REPORT_ID)
        self.assertEqual(retry.next_run, NOW + scheduler.REPORT_RETRY_SECONDS)
        self.assertEqual(
            scheduler.scheduler_status(store, now=NOW)["daily_report"]["state"],
            "retrying",
        )
        second = scheduler.run_due(
            store,
            now=retry.next_run,
            executor=lambda _task: {
                "status": "verified",
                "verification": "ha_speaker_state_transition_observed",
            },
            status_path=None,
        )
        self.assertEqual(len(second), 1)
        moved = store.get_task(scheduler.SYSTEM_DAILY_REPORT_ID)
        self.assertEqual(
            datetime.fromtimestamp(moved.next_run, ZONE).strftime("%Y-%m-%d %H:%M"),
            "2026-08-25 10:00",
        )

    def test_daily_report_delivery_unknown_is_visible_and_never_retried(self) -> None:
        store = self.store()
        store.update_task(
            scheduler.SYSTEM_DAILY_REPORT_ID,
            next_run=NOW,
            recurrence={"type": "daily", "time": "10:00"},
        )
        executor = mock.Mock(
            return_value={
                "status": "delivery_unknown",
                "verification": "ha_service_accepted_without_speaker_transition",
            }
        )

        completed = scheduler.run_due(
            store, now=NOW, executor=executor, status_path=None
        )
        duplicate = scheduler.run_due(
            store, now=NOW + 1, executor=executor, status_path=None
        )
        status = scheduler.scheduler_status(store, now=NOW + 1)["daily_report"]

        self.assertEqual(len(completed), 1)
        self.assertEqual(duplicate, [])
        executor.assert_called_once()
        self.assertEqual(status["state"], "not_due")
        self.assertEqual(status["last_run_epoch"], NOW)
        self.assertEqual(status["attempts"], 1)
        self.assertEqual(
            status["verification"],
            "ha_service_accepted_without_speaker_transition",
        )

    def test_status_count_is_not_truncated_by_public_list_limit(self) -> None:
        store = self.store(seed=False)
        for index in range(scheduler.MAX_LIST_TASKS + 3):
            self.create_local(
                store,
                due=NOW + 1000 + index,
                description=f"Напоминание номер {index}",
                key=f"task-{index}",
            )
        status = scheduler.scheduler_status(store, now=NOW)
        self.assertEqual(
            status["enabled_task_count"], scheduler.MAX_LIST_TASKS + 3
        )

    def test_find_update_and_cancel_by_human_description_do_not_execute(self) -> None:
        store = self.store(seed=False)
        task = self.create_local(store)
        found = store.list_tasks(query="фильтр")
        self.assertEqual([item.task_id for item in found], [task.task_id])
        updated = store.update_task(task.task_id, next_run=NOW + 3600)
        self.assertEqual(updated.next_run, NOW + 3600)
        cancelled = store.cancel_task(task.task_id)
        self.assertFalse(cancelled.enabled)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(store.execution_count(task.task_id), 0)

    def test_native_yandex_record_is_not_falsely_changed_or_cancelled(self) -> None:
        store = self.store(seed=False)
        task = store.create_task(
            owner_id="owner", kind="yandex_native_reminder",
            natural_description="Напоминание Станции",
            canonical_payload={
                "text": "проверить фильтр", "station": "station_max",
                "backend_status": "completed",
            },
            timezone=scheduler.DEFAULT_TIMEZONE, next_run=NOW + 60,
            recurrence={"type": "none"}, enabled=False, status="external_managed",
            delivery_target="station_max", delivery_mode="yandex_native",
            missed_run_policy="skip", verification_policy="yandex_success",
        )
        with self.assertRaises(scheduler.SchedulerError):
            store.update_task(task.task_id, next_run=NOW + 120)
        with self.assertRaises(scheduler.SchedulerError):
            store.cancel_task(task.task_id)

    def test_safe_status_export_contains_nearest_wake_without_payload(self) -> None:
        store = self.store(seed=False)
        task = self.create_local(store, due=NOW + 600)
        path = self.directory / "scheduler-status.json"
        document = scheduler.export_status(store, path=path, now=NOW)
        self.assertEqual(document["next_run_epoch"], task.next_run)
        self.assertEqual(document["wake_epoch"], task.next_run - 120)
        raw = path.read_text("ascii")
        self.assertNotIn("купить фильтр", raw)
        self.assertNotIn("canonical_payload", raw)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_legacy_reminder_migration_is_idempotent_and_preserves_record(self) -> None:
        store = self.store(seed=False)
        legacy = {
            "schema_version": 2,
            "status": "completed",
            "due_at": "2026-08-25T08:00+05:00",
            "timezone": scheduler.DEFAULT_TIMEZONE,
            "reminder_text": "проверить фильтр",
            "fingerprint": "f" * 64,
        }
        reader = mock.Mock(return_value={"content": json.dumps(legacy)})
        with mock.patch.object(scheduler.model_workspace, "read_text", reader):
            first = scheduler.migrate_legacy_reminder(store)
            second = scheduler.migrate_legacy_reminder(store)
        self.assertIsNotNone(first)
        self.assertEqual(first.task_id, second.task_id)
        self.assertEqual(first.kind, "yandex_native_reminder")
        self.assertEqual(first.status, "external_managed")
        self.assertFalse(first.enabled)
        self.assertEqual(len(store.list_tasks(include_disabled=True)), 1)


if __name__ == "__main__":
    unittest.main()
