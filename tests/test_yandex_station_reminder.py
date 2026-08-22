#!/usr/bin/env python3
"""Contracts for the single-purpose Yandex Station Max reminder adapter."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import home_assistant_read as ha_read  # noqa: E402
import model_workspace  # noqa: E402
import yandex_station_reminder as reminder  # noqa: E402


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=reminder.TIMEZONE)
REQUEST = (
    "потавь напоминание на четверг на 7.10 чтобы я поменял тариф мтс "
    "на рил + 1000 мб через яндекс алису, как напоминание поставишь "
    "через яндекс алису скажи напоминание установлено"
)


class FakeResponse:
    def __init__(self, document: object, status: int = 200) -> None:
        self.status = status
        self._raw = json.dumps(document).encode("utf-8")

    def read(self, _limit: int) -> bytes:
        return self._raw


class FakeConnection:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests: list[tuple[object, ...]] = []

    def request(self, *args: object, **kwargs: object) -> None:
        self.requests.append((*args, kwargs))

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        return None


class ReminderParsingTests(unittest.TestCase):
    def test_upcoming_weekday_is_resolved_in_local_timezone(self) -> None:
        parsed = reminder.parse_request(REQUEST, now=NOW)
        self.assertEqual(parsed.due_at.isoformat(timespec="minutes"), "2026-08-20T07:10+05:00")
        self.assertEqual(
            parsed.text,
            "я поменял тариф мтс на рил + 1000 мб",
        )
        self.assertEqual(
            parsed.command,
            "Поставь напоминание на 20 августа 2026 года в 7 часов 10 минут: "
            "я поменял тариф мтс на рил + 1000 мб.",
        )

    def test_elapsed_time_on_same_weekday_moves_to_next_week(self) -> None:
        now = datetime(2026, 8, 20, 8, 0, tzinfo=reminder.TIMEZONE)
        parsed = reminder.parse_request(
            "напомни в четверг в 7:10 чтобы проверить тариф", now=now
        )
        self.assertEqual(parsed.due_at.day, 27)

    def test_missing_time_or_body_is_rejected(self) -> None:
        for value in (
            "напомни в четверг чтобы проверить тариф",
            "напомни в четверг в 7:10",
            "напомни в четверг в 7:10 и 8:10 чтобы проверить тариф",
        ):
            with self.subTest(value=value), self.assertRaises(reminder.ReminderError):
                reminder.parse_request(value, now=NOW)

    def test_urls_and_shell_text_are_rejected(self) -> None:
        for text in ("зайти на https://example.com", "запустить bash -c test"):
            with self.subTest(text=text), self.assertRaises(reminder.ReminderError):
                reminder.parse_request(
                    f"напомни в четверг в 7:10 чтобы {text}", now=NOW
                )


class ReminderTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ha_read.AdapterConfig(
            "http", "192.168.1.127", 8123, "x" * 40, ("*",), True
        )

    def test_transport_uses_only_fixed_service_station_and_send_text(self) -> None:
        connection = FakeConnection(FakeResponse({
            "changed_states": [],
            "service_response": {"status": "SUCCESS", "text": "Напоминание создано"},
        }))
        result = reminder.post_reminder_command(
            self.config,
            "Поставь напоминание на 20 августа 2026 года в 7 часов 10 минут: тест.",
            connection_factory=lambda _config: connection,
        )
        self.assertTrue(result["acknowledged"])
        method, path, details = connection.requests[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, reminder.SERVICE_PATH)
        body = json.loads(details["body"])
        self.assertEqual(body["entity_id"], reminder.STATION_MAX)
        self.assertEqual(body["command"], "sendText")
        self.assertNotIn("target", body)

    def test_negative_alice_response_is_not_success(self) -> None:
        connection = FakeConnection(FakeResponse({
            "service_response": {"status": "SUCCESS", "text": "Не поняла, уточните"},
        }))
        with self.assertRaises(reminder.ReminderRejected):
            reminder.post_reminder_command(
                self.config,
                "Поставь напоминание на 20 августа 2026 года в 7 часов 10 минут: тест.",
                connection_factory=lambda _config: connection,
            )

    def test_missing_station_response_becomes_delivery_unknown(self) -> None:
        connection = FakeConnection(FakeResponse({"changed_states": []}))
        with self.assertRaises(reminder.ReminderDeliveryUnknown):
            reminder.post_reminder_command(
                self.config,
                "Поставь напоминание на 20 августа 2026 года в 7 часов 10 минут: тест.",
                connection_factory=lambda _config: connection,
            )


class ReminderWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.files: dict[str, str] = {}
        self.config = ha_read.AdapterConfig(
            "http", "192.168.1.127", 8123, "x" * 40, ("*",), True
        )

    def reader(self, path: object) -> dict[str, object]:
        key = str(path)
        if key not in self.files:
            raise model_workspace.WorkspaceError("missing")
        return {"content": self.files[key]}

    def writer(self, path: object, content: object) -> dict[str, object]:
        self.files[str(path)] = str(content)
        return {"path": str(path)}

    @staticmethod
    def snapshot(_action: str) -> tuple[dict[str, object], int]:
        return ({
            "status": "healthy",
            "entities": [{
                "entity_id": reminder.STATION_MAX,
                "state_kind": "enum",
            }],
        }, 0)

    def test_success_is_recorded_and_fixed_confirmation_is_sent(self) -> None:
        calls: list[tuple[str, str]] = []

        def command(_config: ha_read.AdapterConfig, text: str) -> dict[str, object]:
            calls.append(("command", text))
            return {"acknowledged": True, "station_status": "SUCCESS"}

        def tts(_config: ha_read.AdapterConfig, entity: str, text: str) -> None:
            calls.append((entity, text))

        rendered = reminder.create_reminder(
            REQUEST,
            now=NOW,
            observed_epoch=100,
            config_loader=lambda: self.config,
            snapshot_reader=self.snapshot,
            command_caller=command,
            tts_caller=tts,
            workspace_reader=self.reader,
            workspace_writer=self.writer,
        )
        self.assertIn("Напоминание установлено", rendered)
        self.assertEqual(calls[-1], (reminder.STATION_MAX, reminder.CONFIRMATION))
        record = json.loads(self.files[reminder.LAST_REMINDER_PATH])
        self.assertEqual(record["status"], "completed")
        self.assertTrue(record["reminder_created"])
        self.assertTrue(record["voice_confirmation_sent"])
        self.assertEqual(record["actions_performed"], 2)
        self.assertFalse(record["automatic_retry_allowed"])

    def test_delivery_unknown_is_not_retried_or_confirmed(self) -> None:
        tts = mock.Mock()

        def unknown(_config: ha_read.AdapterConfig, _text: str) -> dict[str, object]:
            raise reminder.ReminderDeliveryUnknown("unknown")

        rendered = reminder.create_reminder(
            REQUEST,
            now=NOW,
            observed_epoch=100,
            config_loader=lambda: self.config,
            snapshot_reader=self.snapshot,
            command_caller=unknown,
            tts_caller=tts,
            workspace_reader=self.reader,
            workspace_writer=self.writer,
        )
        self.assertIn("не повторяю", rendered)
        tts.assert_not_called()
        record = json.loads(self.files[reminder.LAST_REMINDER_PATH])
        self.assertEqual(record["status"], "delivery_unknown")
        self.assertFalse(record["reminder_created"])

        caller = mock.Mock()
        second = reminder.create_reminder(
            REQUEST,
            now=NOW,
            observed_epoch=101,
            config_loader=lambda: self.config,
            snapshot_reader=self.snapshot,
            command_caller=caller,
            tts_caller=tts,
            workspace_reader=self.reader,
            workspace_writer=self.writer,
        )
        self.assertIn("Повторно не отправляю", second)
        caller.assert_not_called()

    def test_completed_fingerprint_prevents_a_duplicate(self) -> None:
        parsed = reminder.parse_request(REQUEST, now=NOW)
        self.files[reminder.LAST_REMINDER_PATH] = json.dumps({
            "fingerprint": parsed.fingerprint,
            "status": "completed",
        })
        caller = mock.Mock()
        rendered = reminder.create_reminder(
            REQUEST,
            now=NOW,
            observed_epoch=101,
            config_loader=lambda: self.config,
            snapshot_reader=self.snapshot,
            command_caller=caller,
            workspace_reader=self.reader,
            workspace_writer=self.writer,
        )
        self.assertIn("уже установлено", rendered)
        caller.assert_not_called()


if __name__ == "__main__":
    unittest.main()
