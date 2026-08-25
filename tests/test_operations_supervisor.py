#!/usr/bin/env python3
"""Contracts for the continuous Home Butler duty supervisor."""

from __future__ import annotations

import sys
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import operations_supervisor as supervisor  # noqa: E402


def healthy_devices() -> dict[str, object]:
    return {
        "fresh": True,
        "age_seconds": 3,
        "device_count": 38,
        "healthy": 38,
        "partial": 0,
        "degraded": 0,
        "offline": 0,
        "unknown": 0,
        "integration_degraded": 0,
    }


class OperationsSupervisorTests(unittest.TestCase):
    def test_healthy_requires_every_fixed_operational_proof(self) -> None:
        status = supervisor.build_status(
            now=1_786_435_000,
            ha_reader=lambda: {
                "connected": True,
                "status": "healthy",
                "entity_count": 198,
                "available_entity_count": 190,
                "unavailable_entity_count": 8,
            },
            device_reader=healthy_devices,
            heartbeat_reader=lambda: {
                "fresh": True, "age_seconds": 30, "status": "ok"
            },
            daily_reader=lambda: {
                "state": "not_due", "verified": False, "attempts": 0
            },
            model_reader=lambda: {
                "reachable": True, "loaded": True, "accelerator": "gpu"
            },
            unit_checker=lambda unit: unit in supervisor.REQUIRED_UNITS,
        )
        self.assertEqual(status["overall_status"], "healthy")
        self.assertTrue(status["computer"]["connected"])
        self.assertEqual(status["device_monitor"]["device_count"], 38)
        self.assertEqual(set(status["services"]), set(supervisor.REQUIRED_UNITS))

    def test_stale_monitor_missed_report_and_offline_device_require_attention(self) -> None:
        devices = healthy_devices()
        devices.update({"fresh": False, "offline": 1, "healthy": 37})
        status = supervisor.build_status(
            now=1_786_435_000,
            ha_reader=lambda: {"connected": True, "status": "healthy"},
            device_reader=lambda: devices,
            heartbeat_reader=lambda: {
                "fresh": True, "age_seconds": 1, "status": "ok"
            },
            daily_reader=lambda: {
                "state": "missed", "verified": False, "attempts": 3
            },
            model_reader=lambda: {
                "reachable": True, "loaded": True, "accelerator": "gpu"
            },
            unit_checker=lambda _unit: True,
        )
        self.assertEqual(status["overall_status"], "attention")
        self.assertEqual(status["daily_report"]["state"], "missed")
        self.assertEqual(status["device_monitor"]["offline"], 1)

    def test_daily_report_status_comes_from_persistent_scheduler(self) -> None:
        scheduler_state = {
            "task_id": "system-daily-report",
            "state": "retrying",
            "next_run_epoch": 1_786_435_300,
            "last_run_epoch": 1_786_435_000,
            "attempts": 2,
            "verification": "not_sent",
        }
        with mock.patch.object(
            supervisor.persistent_scheduler,
            "read_daily_report_status",
            return_value=scheduler_state,
        ) as reader:
            result = supervisor.read_daily_report(now=1_786_435_100)
        reader.assert_called_once_with(now=1_786_435_100)
        self.assertEqual(result["state"], "retrying")
        self.assertEqual(result["next_run_epoch"], 1_786_435_300)
        self.assertEqual(result["last_run_epoch"], 1_786_435_000)
        self.assertFalse(result["verified"])

    def test_operations_status_is_private_and_check_accepts_attention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            path = directory / "operations-status.json"
            document = {
                "schema_version": 1,
                "observed_epoch": 1000,
                "overall_status": "attention",
            }
            supervisor.write_status(document, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(supervisor.check_status(path, now=1050))
            self.assertFalse(supervisor.check_status(path, now=1100))

    def test_unit_probe_cannot_escape_fixed_allowlist(self) -> None:
        with self.assertRaises(supervisor.SupervisorError):
            supervisor.unit_is_active("home-assistant.service")

    def test_transition_journal_records_only_real_state_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            path = directory / "operations-events.jsonl"
            first = {
                "observed_epoch": 100,
                "overall_status": "healthy",
                "services": {"home-butler.service": True},
                "home_assistant": {"connected": True, "entity_count": 20},
                "model": {"reachable": True, "loaded": True, "accelerator": "gpu"},
                "device_monitor": {"fresh": True, "device_count": 3, "healthy": 3},
                "heartbeat": {"fresh": True, "age_seconds": 2, "status": "ok"},
                "daily_report": {"state": "not_due", "attempts": 0},
            }
            same = json.loads(json.dumps(first))
            same["observed_epoch"] = 130
            same["heartbeat"]["age_seconds"] = 32
            changed = json.loads(json.dumps(same))
            changed["observed_epoch"] = 160
            changed["home_assistant"]["connected"] = False
            self.assertTrue(supervisor.append_transition(first, previous=None, path=path))
            self.assertFalse(supervisor.append_transition(same, previous=first, path=path))
            self.assertTrue(supervisor.append_transition(changed, previous=same, path=path))
            events = [json.loads(line) for line in path.read_text("ascii").splitlines()]
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["event"], "operational_baseline")
            self.assertEqual(events[1]["event"], "operational_transition")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
