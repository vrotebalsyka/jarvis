#!/usr/bin/env python3
"""Acceptance contracts for natural Russian scheduler requests."""

from __future__ import annotations

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
import scheduler_natural as natural  # noqa: E402


ZONE = ZoneInfo(scheduler.DEFAULT_TIMEZONE)
NOW_DT = datetime(2026, 8, 24, 10, 0, tzinfo=ZONE)
NOW = int(NOW_DT.timestamp())


def model_document(
    *,
    operation: str = "create",
    kind: str | None = "local_reminder",
    description: str | None = "Напоминание: заказать таблетки для посудомойки",
    payload: dict[str, object] | None = None,
    next_run: str | None = "2026-08-25T08:00:00+05:00",
    recurrence: dict[str, object] | None = None,
    query: str | None = None,
    clarification: str | None = None,
) -> dict[str, object]:
    return {
        "operation": operation,
        "kind": kind,
        "natural_description": description,
        "canonical_payload": payload if payload is not None else {
            "text": "заказать таблетки для посудомойки"
        },
        "timezone": scheduler.DEFAULT_TIMEZONE if kind is not None else None,
        "next_run_local": next_run,
        "recurrence": recurrence if recurrence is not None else {"type": "none"},
        "query": query,
        "clarification": clarification,
    }


class NaturalSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.directory.chmod(0o700)
        self.store = scheduler.SchedulerStore(
            self.directory / "scheduler.sqlite3", clock=lambda: NOW
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_q_natural_reminder_without_required_word_creates_taskspec(self) -> None:
        phrase = (
            "Завтра утром в восемь напомни заказать таблетки для посудомойки."
        )
        result = natural.handle_natural_task_request(
            phrase,
            store=self.store,
            now=NOW_DT,
            model_parser=lambda _text, _now: model_document(),
        )
        tasks = self.store.list_tasks(query="таблетки")
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task.kind, "local_reminder")
        self.assertEqual(task.canonical_payload["text"], "заказать таблетки для посудомойки")
        self.assertEqual(
            datetime.fromtimestamp(task.next_run, ZONE).strftime("%d.%m.%Y %H:%M"),
            "25.08.2026 08:00",
        )
        self.assertIn("25.08.2026 в 08:00", result)
        self.assertIn(scheduler.DEFAULT_TIMEZONE, result)
        self.assertIn("без повтора", result)

    def test_model_json_is_deterministically_rejected_if_time_is_in_past(self) -> None:
        stale = model_document(next_run="2026-08-24T09:00:00+05:00")
        with self.assertRaises(natural.NaturalScheduleError):
            natural.validate_model_document(stale, now=NOW_DT)

    def test_one_short_clarification_is_returned_without_creating_task(self) -> None:
        document = model_document(
            operation="clarify", kind=None, description=None, payload=None,
            next_run=None, recurrence=None, clarification="Во сколько напомнить",
        )
        result = natural.handle_natural_task_request(
            "Напомни проверить фильтр",
            store=self.store,
            now=NOW_DT,
            model_parser=lambda _text, _now: document,
        )
        self.assertEqual(result, "Во сколько напомнить?")
        self.assertEqual(len(self.store.list_tasks(query="фильтр")), 0)

    def test_fallback_understands_tomorrow_and_relative_hour(self) -> None:
        failing = mock.Mock(side_effect=natural.NaturalScheduleError("offline"))
        tomorrow = natural.parse_natural_request(
            "Завтра утром в восемь напомни заказать таблетки для посудомойки",
            now=NOW_DT,
            model_parser=failing,
        )
        relative = natural.parse_natural_request(
            "Через полтора часа напомни проверить посудомойку",
            now=NOW_DT,
            model_parser=failing,
        )
        self.assertEqual(tomorrow["next_run"], int(datetime(2026, 8, 25, 8, 0, tzinfo=ZONE).timestamp()))
        self.assertEqual(relative["next_run"], NOW + 90 * 60)

    def test_r_daily_report_reschedule_has_one_source_and_runs_at_new_time(self) -> None:
        new_due = datetime(2026, 8, 25, 11, 40, tzinfo=ZONE)
        document = model_document(
            operation="update",
            kind="daily_report",
            description="Ежедневный отчёт о состоянии дома",
            payload={"report": "home_status"},
            next_run=new_due.isoformat(),
            recurrence={"type": "daily", "time": "11:40"},
            query="ежедневный отчёт",
        )
        result = natural.handle_natural_task_request(
            "С завтрашнего дня ежедневный отчёт в 11:40.",
            store=self.store,
            now=NOW_DT,
            model_parser=lambda _text, _now: document,
        )
        task = self.store.get_task(scheduler.SYSTEM_DAILY_REPORT_ID)
        self.assertEqual(task.next_run, int(new_due.timestamp()))
        self.assertEqual(task.recurrence, {"type": "daily", "time": "11:40"})
        self.assertIn("25.08.2026 в 11:40", result)

        old_time = int(datetime(2026, 8, 24, 13, 0, tzinfo=ZONE).timestamp())
        executor = mock.Mock(return_value={
            "status": "verified", "verification": "ha_speaker_state_transition_observed"
        })
        self.assertEqual(
            scheduler.run_due(
                self.store, now=old_time, executor=executor, status_path=None
            ),
            [],
        )
        executor.assert_not_called()

        status = scheduler.scheduler_status(self.store, now=old_time)
        self.assertEqual(status["daily_report"]["next_run_epoch"], int(new_due.timestamp()))
        self.assertEqual(status["wake_epoch"], int(new_due.timestamp()) - 120)
        completed = scheduler.run_due(
            self.store, now=int(new_due.timestamp()), executor=executor,
            status_path=None,
        )
        self.assertEqual(len(completed), 1)
        executor.assert_called_once()
        moved = self.store.get_task(scheduler.SYSTEM_DAILY_REPORT_ID)
        self.assertEqual(
            datetime.fromtimestamp(moved.next_run, ZONE).strftime("%Y-%m-%d %H:%M"),
            "2026-08-26 11:40",
        )

    def test_t_find_update_cancel_by_normal_name_has_no_duplicate_execution(self) -> None:
        create = model_document()
        natural.apply_plan(
            natural.validate_model_document(create, now=NOW_DT), store=self.store
        )
        task = self.store.list_tasks(query="таблетки")[0]
        update = model_document(
            operation="update",
            next_run="2026-08-25T09:30:00+05:00",
            query="таблетки для посудомойки",
        )
        natural.apply_plan(
            natural.validate_model_document(update, now=NOW_DT), store=self.store
        )
        changed = self.store.get_task(task.task_id)
        self.assertEqual(
            datetime.fromtimestamp(changed.next_run, ZONE).strftime("%H:%M"), "09:30"
        )
        cancel_doc = model_document(
            operation="cancel", kind=None, description=None, payload=None,
            next_run=None, recurrence=None, query="таблетки для посудомойки",
        )
        natural.apply_plan(
            natural.validate_model_document(cancel_doc, now=NOW_DT), store=self.store
        )
        cancelled = self.store.get_task(task.task_id)
        self.assertFalse(cancelled.enabled)
        self.assertEqual(self.store.execution_count(task.task_id), 0)

    def test_cancel_phrase_fallback_finds_query_without_special_template(self) -> None:
        plan = natural.parse_natural_request(
            "Отмени напоминание про фильтр",
            now=NOW_DT,
            model_parser=mock.Mock(side_effect=natural.NaturalScheduleError("offline")),
        )
        self.assertEqual(plan, {"operation": "cancel", "query": "фильтр"})


if __name__ == "__main__":
    unittest.main()
