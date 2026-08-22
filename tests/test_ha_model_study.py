#!/usr/bin/env python3
"""Contracts for bounded read-only Home Assistant model study."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import ha_model_study as study  # noqa: E402


PHYSICAL = "a" * 64


def snapshot() -> dict[str, object]:
    return {
        "entities": [
            {
                "entity_id": "sensor.robot_main_brush_life",
                "state_kind": "number",
                "state_value": 9.0,
                "source_last_updated_at": "2026-08-16T00:00:00+00:00",
            },
            {
                "entity_id": "sensor.dishwasher_error_code",
                "state_kind": "number",
                "state_value": 0.0,
                "source_last_updated_at": "2026-08-16T00:00:00+00:00",
            },
        ]
    }


def inventory() -> dict[str, object]:
    return {
        "entities": [
            {
                "entity_id": "sensor.robot_main_brush_life",
                "friendly_name": "Robot main brush life",
                "platform": "xiaomi_miot",
                "physical_device_hash": PHYSICAL,
            },
            {
                "entity_id": "sensor.dishwasher_error_code",
                "friendly_name": "Dishwasher Код ошибки",
                "platform": "midea_ac_lan",
                "physical_device_hash": PHYSICAL,
            },
        ]
    }


class ModelStudyTests(unittest.TestCase):
    def test_model_mistake_is_recorded_but_cannot_change_validated_rule(self) -> None:
        candidates = study.collect_candidates(snapshot(), inventory())
        response = {
            "message": {
                "content": json.dumps({
                    "findings": [
                        {
                            "entity_id": "sensor.dishwasher_error_code",
                            "category": "error_code",
                            "alert_condition": "nonzero",
                            "reason_ru": "Код ошибки",
                        },
                        {
                            "entity_id": "sensor.robot_main_brush_life",
                            "category": "consumable_shortage",
                            "alert_condition": "on",
                            "reason_ru": "Ошибочное предложение модели",
                        },
                    ]
                }, ensure_ascii=False)
            }
        }
        findings = study.validate_findings(response, candidates)
        by_id = {item["entity_id"]: item for item in findings}
        brush = by_id["sensor.robot_main_brush_life"]
        self.assertEqual(brush["category"], "remaining_life")
        self.assertEqual(brush["alert_condition"], "at_or_below_10")
        self.assertFalse(brush["model_agreed"])
        self.assertTrue(by_id["sensor.dishwasher_error_code"]["model_agreed"])

    def test_catalog_is_read_only_and_private(self) -> None:
        response = {
            "message": {
                "content": json.dumps({
                    "findings": [
                        {
                            "entity_id": "sensor.dishwasher_error_code",
                            "category": "error_code",
                            "alert_condition": "nonzero",
                            "reason_ru": "Код ошибки",
                        },
                        {
                            "entity_id": "sensor.robot_main_brush_life",
                            "category": "remaining_life",
                            "alert_condition": "at_or_below_10",
                            "reason_ru": "Остаток ресурса",
                        },
                    ]
                }, ensure_ascii=False)
            }
        }
        document = study.build_catalog(
            snapshot_reader=lambda _action: (snapshot(), 0),
            inventory_loader=inventory,
            model_reader=lambda _candidates: response,
            now=lambda: 1_786_435_000,
        )
        self.assertEqual(document["learning_scope"], "read_only")
        self.assertEqual(document["actions_performed"], 0)
        self.assertEqual(document["model_rejected_count"], 0)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            path = directory / "study.json"
            study.write_catalog(document, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
