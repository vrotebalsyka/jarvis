#!/usr/bin/env python3
"""Contracts for validated diagnostic transition monitoring."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import diagnostic_monitor as monitor  # noqa: E402


def catalog() -> dict[str, object]:
    return {"findings": [
        {
            "entity_id": "sensor.robot_brush_life",
            "friendly_name": "Robot brush life",
            "category": "remaining_life",
            "alert_condition": "at_or_below_10",
        },
        {
            "entity_id": "sensor.dishwasher_error_code",
            "friendly_name": "Dishwasher Код ошибки",
            "category": "error_code",
            "alert_condition": "nonzero",
        },
    ]}


def snapshot(brush: float = 9, error: float = 0) -> dict[str, object]:
    return {"entities": [
        {
            "entity_id": "sensor.robot_brush_life",
            "state_kind": "number",
            "state_value": brush,
        },
        {
            "entity_id": "sensor.dishwasher_error_code",
            "state_kind": "number",
            "state_value": error,
        },
        {
            "entity_id": "media_player.yandex_station_m10vgng0005wxb",
            "state_kind": "enum",
            "state_value": "idle",
        },
    ]}


class DiagnosticMonitorTests(unittest.TestCase):
    def test_new_validated_alert_is_announced_once_without_device_action(self) -> None:
        notifier = mock.Mock()
        first = monitor.run_once(
            live=True,
            catalog_loader=catalog,
            previous_loader=lambda: {"active_alerts": []},
            snapshot_reader=lambda _action: (snapshot(), 0),
            notifier=notifier,
            config_loader=object,
            now=lambda: 100,
        )
        self.assertEqual(first["detected_count"], 1)
        self.assertEqual(first["service_calls"], 1)
        self.assertEqual(first["actions_performed"], 0)
        self.assertIn("9 процентов ресурса", first["message"])
        notifier.assert_called_once()

        notifier.reset_mock()
        second = monitor.run_once(
            live=True,
            catalog_loader=catalog,
            previous_loader=lambda: first,
            snapshot_reader=lambda _action: (snapshot(), 0),
            notifier=notifier,
            config_loader=object,
            now=lambda: 160,
        )
        self.assertEqual(second["detected_count"], 0)
        self.assertEqual(second["service_calls"], 0)
        notifier.assert_not_called()

    def test_recovery_is_detected(self) -> None:
        previous = {
            "active_alerts": [{
                "entity_id": "sensor.robot_brush_life",
                "friendly_name": "Robot brush life",
                "category": "remaining_life",
                "state_value": 9,
            }]
        }
        result = monitor.run_once(
            live=False,
            catalog_loader=catalog,
            previous_loader=lambda: previous,
            snapshot_reader=lambda _action: (snapshot(brush=50), 0),
        )
        self.assertEqual(result["resolved_count"], 1)
        self.assertIn("проблема устранена", result["message"])


if __name__ == "__main__":
    unittest.main()
