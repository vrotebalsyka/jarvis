#!/usr/bin/env python3
"""Contracts for semantic diagnostic findings and transition monitoring."""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import diagnostic_monitor as monitor  # noqa: E402


ROBOT = "a" * 64
DISHWASHER = "b" * 64


def profile(
    entity_id: str,
    physical: str,
    device_name: str,
    component: str,
    issue: str,
    operator: str,
    threshold: float | None = None,
) -> dict[str, object]:
    seed = hashlib.sha256(entity_id.encode()).hexdigest()
    return {
        "profile_id": seed,
        "entity_id": entity_id,
        "physical_device_id": physical,
        "physical_display_name": device_name,
        "component": component,
        "semantic_role": "diagnostic" if issue == "error_code" else "consumable",
        "issue_class": issue,
        "normal_state_semantics": "unknown",
        "abnormal_state_semantics": "none_known",
        "severity_policy": "warning",
        "classification_confidence": 0.9,
        "evidence_fields": ["current_state"],
        "recommended_monitoring_condition": {
            "operator": operator,
            "threshold": threshold,
        },
        "model_explanation_ru": "Только semantic evidence",
        "model_text_trust": "untrusted_data",
        "metadata_hash": "c" * 64,
        "model_version": "fixture",
        "observed_state": {},
    }


def vacuum_catalog() -> dict[str, object]:
    profiles = [
        profile(
            "sensor.robot_component_00", ROBOT, "Андрей", "Сервисный модуль",
            "consumable_level", "less_or_equal", 10.0,
        )
    ]
    for index in range(1, 26):
        profiles.append(profile(
            f"sensor.robot_component_{index:02d}", ROBOT, "Андрей",
            f"Компонент {index}", "none", "none",
        ))
    return {"schema_version": 2, "profiles": profiles}


def vacuum_snapshot(value: float = 9.0) -> dict[str, object]:
    entities = [{
        "entity_id": "sensor.robot_component_00",
        "state_kind": "number",
        "state_value": value,
        "source_last_updated_at": "2026-08-24T08:00:00+00:00",
    }]
    entities.extend({
        "entity_id": f"sensor.robot_component_{index:02d}",
        "state_kind": "number",
        "state_value": float(index),
        "source_last_updated_at": "2026-08-24T08:00:00+00:00",
    } for index in range(1, 26))
    entities.append({
        "entity_id": "media_player.yandex_station_m10vgng0005wxb",
        "state_kind": "enum",
        "state_value": "idle",
        "source_last_updated_at": "2026-08-24T08:00:00+00:00",
    })
    return {"entities": entities}


class DiagnosticMonitorTests(unittest.TestCase):
    def test_vacuum_with_26_features_reports_only_component_not_device_outage(self) -> None:
        findings = monitor.evaluate(vacuum_snapshot(), vacuum_catalog())
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["component"], "Сервисный модуль")
        self.assertTrue(finding["physical_device_available"])
        self.assertEqual(finding["available_feature_count"], 26)
        self.assertEqual(finding["total_feature_count"], 26)
        self.assertEqual(finding["actionability"], "observe_only")
        message = monitor.render_message(findings)
        self.assertIn("сам прибор доступен", message)
        self.assertIn("Сервисный модуль", message)
        self.assertNotIn("недоступен", message)

    def test_unknown_dishwasher_consumable_is_specific_and_read_only(self) -> None:
        catalog = {"schema_version": 2, "profiles": [
            profile(
                "binary_sensor.dishwasher_zq_medium", DISHWASHER,
                "Посудомойка", "ZQ medium", "consumable_shortage", "on",
            ),
            profile(
                "switch.dishwasher_power", DISHWASHER,
                "Посудомойка", "Питание", "none", "none",
            ),
        ]}
        snapshot = {"entities": [
            {
                "entity_id": "binary_sensor.dishwasher_zq_medium",
                "state_kind": "enum", "state_value": "on",
                "source_last_updated_at": "2026-08-24T08:00:00+00:00",
            },
            {
                "entity_id": "switch.dishwasher_power",
                "state_kind": "enum", "state_value": "on",
                "source_last_updated_at": "2026-08-24T08:00:00+00:00",
            },
        ]}
        findings = monitor.evaluate(snapshot, catalog)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["issue_class"], "consumable_shortage")
        self.assertEqual(findings[0]["suggested_playbook_ids"], [])
        message = monitor.render_message(findings)
        self.assertIn("Посудомойка: сам прибор доступен", message)
        self.assertIn("ZQ medium: требуется пополнить расходник", message)

    def test_unknown_numeric_error_code_is_reported_without_invented_meaning(self) -> None:
        catalog = {"schema_version": 2, "profiles": [
            profile(
                "sensor.appliance_register", DISHWASHER, "Посудомойка",
                "Диагностический регистр", "error_code", "nonzero",
            ),
            profile(
                "switch.dishwasher_power", DISHWASHER, "Посудомойка",
                "Питание", "none", "none",
            ),
        ]}
        snapshot = {"entities": [
            {
                "entity_id": "sensor.appliance_register",
                "state_kind": "number", "state_value": 17.0,
                "source_last_updated_at": "2026-08-24T08:00:00+00:00",
            },
            {
                "entity_id": "switch.dishwasher_power",
                "state_kind": "enum", "state_value": "on",
                "source_last_updated_at": "2026-08-24T08:00:00+00:00",
            },
        ]}
        message = monitor.render_message(monitor.evaluate(snapshot, catalog))
        self.assertIn("код ошибки 17", message)
        self.assertIn("точное значение интеграция не передала", message)

    def test_new_semantic_finding_is_announced_once_without_device_action(self) -> None:
        notifier = mock.Mock()
        first = monitor.run_once(
            live=True,
            catalog_loader=vacuum_catalog,
            previous_loader=lambda: {"active_alerts": []},
            snapshot_reader=lambda _action: (vacuum_snapshot(), 0),
            notifier=notifier,
            config_loader=object,
            now=lambda: 100,
        )
        self.assertEqual(first["schema_version"], 2)
        self.assertEqual(first["detected_count"], 1)
        self.assertEqual(first["service_calls"], 1)
        self.assertEqual(first["actions_performed"], 0)
        notifier.assert_called_once()

        notifier.reset_mock()
        second = monitor.run_once(
            live=True,
            catalog_loader=vacuum_catalog,
            previous_loader=lambda: first,
            snapshot_reader=lambda _action: (vacuum_snapshot(), 0),
            notifier=notifier,
            config_loader=object,
            now=lambda: 160,
        )
        self.assertEqual(second["detected_count"], 0)
        self.assertEqual(second["service_calls"], 0)
        notifier.assert_not_called()

    def test_recovery_is_detected(self) -> None:
        first = monitor.run_once(
            live=False,
            catalog_loader=vacuum_catalog,
            previous_loader=lambda: {"active_alerts": []},
            snapshot_reader=lambda _action: (vacuum_snapshot(), 0),
        )
        result = monitor.run_once(
            live=False,
            catalog_loader=vacuum_catalog,
            previous_loader=lambda: first,
            snapshot_reader=lambda _action: (vacuum_snapshot(value=50), 0),
        )
        self.assertEqual(result["resolved_count"], 1)
        self.assertIn("проблема устранена", result["message"])


if __name__ == "__main__":
    unittest.main()
