#!/usr/bin/env python3
"""Contracts for the exact-13:00 Home Butler voice report."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import daily_voice_report as report  # noqa: E402
import home_assistant_notify as notify  # noqa: E402


DEVICE_A = "a" * 32
DEVICE_B = "b" * 32
PHYSICAL_A = "1" * 64


def inventory() -> dict[str, object]:
    return {
        "schema_version": 1,
        "entities": [
            {
                "entity_id": "switch.one",
                "device_id": DEVICE_A,
                "physical_device_hash": PHYSICAL_A,
            },
            {
                "entity_id": "sensor.one",
                "device_id": DEVICE_A,
                "physical_device_hash": PHYSICAL_A,
            },
            {"entity_id": "switch.two", "device_id": DEVICE_B},
            {"entity_id": "sensor.virtual", "device_id": None},
        ],
    }


def snapshot() -> dict[str, object]:
    return {
        "status": "stale_data",
        "entities": [
            {"entity_id": "switch.one", "state_kind": "unavailable"},
            {"entity_id": "sensor.one", "state_kind": "number"},
            {"entity_id": "switch.two", "state_kind": "unavailable"},
            {"entity_id": notify.FALLBACK_SPEAKER, "state_kind": "enum"},
        ],
    }


class DailyVoiceReportTests(unittest.TestCase):
    def test_counts_actual_registry_devices_from_fresh_entity_states(self) -> None:
        available, unavailable = report.device_availability_counts(
            inventory(), snapshot()
        )
        self.assertEqual((available, unavailable), (1, 1))

    def test_duplicate_registry_devices_count_as_one_physical_device(self) -> None:
        duplicated = inventory()
        duplicated["entities"].append({
            "entity_id": "binary_sensor.one_cloud",
            "device_id": "c" * 32,
            "physical_device_hash": PHYSICAL_A,
        })
        current = snapshot()
        current["entities"].append({
            "entity_id": "binary_sensor.one_cloud",
            "state_kind": "unavailable",
        })
        self.assertEqual(
            report.device_availability_counts(duplicated, current), (1, 1)
        )

    def test_report_contains_all_five_requested_facts(self) -> None:
        message = report.render_message(
            {
                "available_devices": 33,
                "unavailable_devices": 13,
                "uptime_seconds": 2 * 86400 + 4 * 3600,
                "cpu_percent": 12,
                "memory_percent": 38,
            }
        )
        self.assertIn("Доступно 33 устройства", message)
        self.assertIn("недоступно 13 устройств", message)
        self.assertIn("2 дня 4 часа", message)
        self.assertIn("процессора 12 процентов", message)
        self.assertIn("памяти 38 процентов", message)
        self.assertLessEqual(len(message), notify.MAX_MESSAGE_CHARS)

    def test_expanded_report_names_incident_recovery_and_gpu_mode(self) -> None:
        facts = {
            "available_devices": 31,
            "unavailable_devices": 1,
            "uptime_seconds": 3 * 86400,
            "cpu_percent": 14,
            "memory_percent": 42,
            "incidents_total": 2,
            "agent_recovered": 1,
            "self_recovered": 0,
            "unresolved_incidents": 1,
            "model_status": "loaded",
            "model_accelerator": "gpu",
            "incident_details": [{
                "display_name": "Гардероб",
                "cause_code": "yandex_cloud_unreachable",
                "recovery_mode": "agent",
                "duration_seconds": 240,
                "recovery_action_code": "retry_original_intent_once",
            }],
        }
        message = report.render_message(facts)
        self.assertIn("Home Assistant на связи", message)
        self.assertIn("Гардероб: нет связи с облаком Яндекса", message)
        self.assertIn("около 4 минут", message)
        self.assertIn("один раз повторил команду", message)
        self.assertIn("полностью на GPU", message)
        self.assertLessEqual(len(message), notify.MAX_MESSAGE_CHARS)

    def test_oversized_incident_details_are_kept_out_of_voice_message(self) -> None:
        facts = {
            "available_devices": 31,
            "unavailable_devices": 1,
            "uptime_seconds": 3600,
            "cpu_percent": 14,
            "memory_percent": 42,
            "incidents_total": 20,
            "agent_recovered": 0,
            "self_recovered": 0,
            "unresolved_incidents": 20,
            "incident_details": [
                {
                    "display_name": f"Очень длинное имя устройства номер {index}",
                    "cause_code": "device_not_observed_on_lan",
                    "recovery_mode": "unresolved",
                    "duration_seconds": 600,
                    "announced": True,
                }
                for index in range(20)
            ],
        }

        message = report.render_message(facts)

        self.assertIn("Подробности событий сохранены в журнале", message)
        self.assertLessEqual(len(message), notify.MAX_MESSAGE_CHARS)

    def test_report_names_every_significant_incident_and_omits_short_blip(self) -> None:
        facts = {
            "available_devices": 5,
            "unavailable_devices": 1,
            "uptime_seconds": 7200,
            "cpu_percent": 10,
            "memory_percent": 30,
            "incidents_total": 4,
            "agent_recovered": 1,
            "self_recovered": 2,
            "unresolved_incidents": 1,
            "incident_details": [
                {
                    "display_name": "Гардероб",
                    "cause_code": "command_not_confirmed",
                    "recovery_mode": "agent",
                    "duration_seconds": 181,
                    "recovery_action_code": "retry_original_intent_once",
                    "occurrences": 1,
                },
                {
                    "display_name": "Датчик движения",
                    "cause_code": "device_not_observed_on_lan",
                    "recovery_mode": "self",
                    "duration_seconds": 240,
                    "recovery_action_code": "none",
                    "occurrences": 1,
                },
                {
                    "display_name": "Home Assistant",
                    "cause_code": "home_assistant_unreachable",
                    "recovery_mode": "unresolved",
                    "duration_seconds": 60,
                    "recovery_action_code": "none",
                    "occurrences": 1,
                },
                {
                    "display_name": "Короткий шум",
                    "cause_code": "unknown",
                    "recovery_mode": "self",
                    "duration_seconds": 10,
                    "recovery_action_code": "none",
                    "occurrences": 1,
                },
            ],
        }
        message = report.render_message(facts)
        self.assertIn("Гардероб", message)
        self.assertIn("Датчик движения", message)
        self.assertIn("Home Assistant", message)
        self.assertNotIn("Короткий шум", message)
        self.assertLessEqual(len(message), notify.MAX_MESSAGE_CHARS)

    def test_build_report_includes_timeline_and_model_resources(self) -> None:
        facts, _snapshot, message = report.build_report(
            inventory_loader=inventory,
            snapshot_reader=lambda _action: (snapshot(), 0),
            cpu_reader=lambda: 7,
            memory_reader=lambda: 22,
            uptime_reader=lambda: 3600,
            timeline_reader=lambda: {
                "summary": {
                    "total_incidents": 0,
                    "agent_recovered": 0,
                    "self_recovered": 0,
                    "unresolved": 0,
                },
                "incidents": [],
            },
            resource_reader=lambda: {
                "model_status": "loaded",
                "model_accelerator": "gpu",
            },
            diagnostic_reader=lambda: 2,
        )
        self.assertEqual(facts["incidents_total"], 0)
        self.assertEqual(facts["model_accelerator"], "gpu")
        self.assertIn("сбоев не было", message)
        self.assertIn("Активных предупреждений по ошибкам и расходникам: 2", message)

    def test_build_report_voices_only_owner_relevant_timeline_events(self) -> None:
        base = {
            "kind": "device_outage",
            "status": "resolved",
            "action_code": "availability_check",
            "recovery_action_code": "none",
            "recovery_attempts": 0,
            "verification_checks": 0,
            "started_epoch": 100,
            "resolved_epoch": 400,
            "duration_seconds": 300,
            "occurrences": 1,
            "recovery_mode": "self",
        }
        facts, _snapshot, message = report.build_report(
            inventory_loader=inventory,
            snapshot_reader=lambda _action: (snapshot(), 0),
            cpu_reader=lambda: 7,
            memory_reader=lambda: 22,
            uptime_reader=lambda: 3600,
            timeline_reader=lambda: {
                "summary": {},
                "incidents": [
                    {
                        **base,
                        "display_name": "Важный датчик",
                        "cause_code": "device_not_observed_on_lan",
                        "announced": True,
                    },
                    {
                        **base,
                        "display_name": "Фоновый шум",
                        "cause_code": "unknown",
                        "announced": False,
                    },
                ],
            },
            resource_reader=lambda: {
                "model_status": "loaded", "model_accelerator": "gpu"
            },
        )
        self.assertEqual(facts["incidents_total"], 1)
        self.assertIn("Важный датчик", message)
        self.assertNotIn("Фоновый шум", message)

    def test_cpu_is_a_short_current_sample(self) -> None:
        samples = iter(((1000, 800), (1100, 850)))
        sleeper = mock.Mock()
        value = report.current_cpu_percent(
            reader=lambda: next(samples), sleeper=sleeper
        )
        self.assertEqual(value, 50)
        sleeper.assert_called_once_with(0.2)

    def test_dry_run_never_speaks_and_live_uses_one_fixed_tts_call(self) -> None:
        facts = {
            "available_devices": 1,
            "unavailable_devices": 1,
            "uptime_seconds": 3600,
            "cpu_percent": 5,
            "memory_percent": 20,
        }
        message = report.render_message(facts)
        builder = lambda: (facts, snapshot(), message)
        caller = mock.Mock()
        dry = report.execute(
            live=False, report_builder=builder, service_caller=caller
        )
        self.assertEqual(dry["service_calls"], 0)
        caller.assert_not_called()

        config = object()
        baseline = {
            "state": "paused",
            "last_updated": "before",
            "volume_ready": True,
            "muted": False,
        }
        live = report.execute(
            live=True,
            report_builder=builder,
            config_loader=lambda: config,
            service_caller=caller,
            state_reader=lambda _config, _speaker: baseline,
            delivery_verifier=lambda _config, _speaker, _baseline: True,
        )
        caller.assert_called_once_with(config, notify.FALLBACK_SPEAKER, message)
        self.assertEqual(live["service_calls"], 1)
        self.assertEqual(live["status"], "verified")
        self.assertTrue(live["ok"])

    def test_accepted_tts_without_station_transition_is_not_success(self) -> None:
        facts = {
            "available_devices": 1,
            "unavailable_devices": 0,
            "uptime_seconds": 3600,
            "cpu_percent": 5,
            "memory_percent": 20,
        }
        message = report.render_message(facts)
        result = report.execute(
            live=True,
            report_builder=lambda: (facts, snapshot(), message),
            config_loader=object,
            service_caller=mock.Mock(),
            state_reader=lambda _config, _speaker: {
                "state": "paused",
                "last_updated": "before",
                "volume_ready": True,
                "muted": False,
            },
            delivery_verifier=lambda _config, _speaker, _baseline: False,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "accepted_unverified")
        self.assertEqual(
            result["verification"],
            "ha_service_accepted_without_speaker_transition",
        )

    def test_station_transition_is_required_for_delivery_verification(self) -> None:
        states = iter((
            {
                "state": "paused",
                "last_updated": "before",
                "volume_ready": True,
                "muted": False,
            },
            {
                "state": "playing",
                "last_updated": "after",
                "volume_ready": True,
                "muted": False,
            },
        ))
        ticks = iter((0.0, 0.0, 0.1, 0.1))
        self.assertTrue(report.verify_speaker_transition(
            object(),
            notify.FALLBACK_SPEAKER,
            {
                "state": "paused",
                "last_updated": "before",
                "volume_ready": True,
                "muted": False,
            },
            state_reader=lambda _config, _speaker: next(states),
            clock=lambda: next(ticks),
            sleeper=lambda _seconds: None,
            wait_seconds=1.0,
        ))

    def test_daily_status_is_private_and_counts_same_day_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            path = directory / "daily-report-status.json"
            moment = time.mktime((2026, 8, 11, 13, 0, 0, 0, 0, -1))
            result = {
                "status": "accepted_unverified",
                "verification": "ha_service_accepted_without_speaker_transition",
                "service_calls": 1,
                "message": "sensitive spoken report",
            }
            report.write_status(result, path=path, now=lambda: moment)
            report.write_status(result, path=path, now=lambda: moment + 45)
            document = report.ha_read.strict_json_loads(path.read_bytes())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(document["attempts"], 2)
            self.assertFalse(document["verified"])
            self.assertNotIn("message", document)
            self.assertEqual(len(document["message_sha256"]), 64)

    def test_verified_status_prevents_a_duplicate_same_day_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            path = directory / "daily-report-status.json"
            moment = time.mktime((2026, 8, 11, 13, 0, 0, 0, 0, -1))
            report.write_status(
                {
                    "status": "verified",
                    "verification": "ha_speaker_state_transition_observed",
                    "service_calls": 1,
                    "message": "verified report",
                },
                path=path,
                now=lambda: moment,
            )
            self.assertTrue(
                report.already_verified_today(path, now=lambda: moment + 600)
            )
            self.assertFalse(
                report.already_verified_today(path, now=lambda: moment + 86400)
            )

    def test_delayed_report_is_explicitly_marked_after_retry_window(self) -> None:
        at_deadline = time.mktime((2026, 8, 11, 13, 15, 0, 0, 0, -1))
        after_deadline = time.mktime((2026, 8, 11, 13, 16, 0, 0, 0, -1))
        self.assertFalse(report.report_is_delayed(now=lambda: at_deadline))
        self.assertTrue(report.report_is_delayed(now=lambda: after_deadline))

        facts = {
            "available_devices": 1,
            "unavailable_devices": 0,
            "uptime_seconds": 3600,
            "cpu_percent": 5,
            "memory_percent": 20,
        }
        message = report.render_message(facts)
        result = report.execute(
            live=False,
            delayed=True,
            report_builder=lambda: (facts, snapshot(), message),
        )
        self.assertTrue(result["delayed"])
        self.assertTrue(result["message"].startswith("Запоздавший ежедневный отчёт"))

    def test_daily_report_requires_the_fixed_station_max(self) -> None:
        unavailable = snapshot()
        unavailable["entities"][-1]["state_kind"] = "unavailable"
        with self.assertRaises(report.DailyReportError):
            report.choose_daily_report_speaker(unavailable)

    def test_private_inventory_requires_owner_only_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "inventory.json"
            path.write_text('{"schema_version":1,"entities":[]}', encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaises(report.DailyReportError):
                report._load_inventory(path)

    def test_private_inventory_accepts_current_schema_two(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "inventory.json"
            path.write_text(
                '{"schema_version":2,"entities":[]}', encoding="utf-8"
            )
            path.chmod(0o600)
            self.assertEqual(report._load_inventory(path)["schema_version"], 2)


if __name__ == "__main__":
    unittest.main()
