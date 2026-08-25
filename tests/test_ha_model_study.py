#!/usr/bin/env python3
"""Contracts for the versioned read-only HA semantic entity catalog."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import ha_model_study as study  # noqa: E402


ROBOT = "a" * 64
DISHWASHER = "b" * 64


def tagged(value: str) -> dict[str, str]:
    return {"text": value, "trust": "untrusted_data"}


def snapshot() -> dict[str, object]:
    return {
        "status": "healthy",
        "entities": [
            {
                "entity_id": "sensor.robot_service_medium",
                "state_kind": "number",
                "state_value": 9.0,
                "source_last_updated_at": "2026-08-24T00:00:00+00:00",
            },
            {
                "entity_id": "sensor.dishwasher_register_17",
                "state_kind": "number",
                "state_value": 17.0,
                "source_last_updated_at": "2026-08-24T00:01:00+00:00",
            },
            {
                "entity_id": "sensor.room_temperature",
                "state_kind": "number",
                "state_value": 22.5,
                "source_last_updated_at": "2026-08-24T00:02:00+00:00",
            },
        ],
    }


def inventory() -> dict[str, object]:
    return {
        "schema_version": 3,
        "entities": [
            {
                "entity_id": "sensor.robot_service_medium",
                "friendly_name": "ZQ service medium reserve",
                "original_name": "ZQ service medium reserve",
                "translation_key": "zq_service_medium_remaining",
                "component": "ZQ medium",
                "semantic_role": "measurement",
                "platform": "unknown_vendor",
                "integration_domains": ["unknown_vendor"],
                "physical_device_hash": ROBOT,
                "availability": "available",
                "semantic_attributes": {
                    "unit_of_measurement": tagged("%"),
                    "state_class": tagged("measurement"),
                },
            },
            {
                "entity_id": "sensor.dishwasher_register_17",
                "friendly_name": "Diagnostic register",
                "original_name": "Diagnostic register",
                "translation_key": "diagnostic_register",
                "component": "Diagnostic register",
                "semantic_role": "diagnostic",
                "diagnostic_relevance": True,
                "platform": "new_appliance",
                "integration_domains": ["new_appliance"],
                "physical_device_hash": DISHWASHER,
                "availability": "available",
                "semantic_attributes": {},
            },
            {
                "entity_id": "sensor.room_temperature",
                "friendly_name": "Room temperature",
                "component": "temperature",
                "semantic_role": "measurement",
                "platform": "demo",
                "integration_domains": ["demo"],
                "physical_device_hash": None,
                "availability": "available",
                "semantic_attributes": {
                    "device_class": tagged("temperature"),
                    "unit_of_measurement": tagged("°C"),
                },
            },
        ],
        "physical_devices": [
            {"physical_device_hash": ROBOT, "display_name": "Андрей"},
            {"physical_device_hash": DISHWASHER, "display_name": "Посудомойка"},
        ],
    }


def model_profile(
    entity_id: str,
    *,
    component: str,
    role: str,
    issue: str,
    operator: str,
    threshold: float | None,
    evidence: list[str],
) -> dict[str, object]:
    abnormal = {
        "less_or_equal": "numeric_low_is_attention",
        "nonzero": "nonzero_is_error_code",
        "none": "none_known",
    }[operator]
    return {
        "entity_id": entity_id,
        "component": component,
        "semantic_role": role,
        "issue_class": issue,
        "normal_state_semantics": "unknown",
        "abnormal_state_semantics": abnormal,
        "severity_policy": "warning" if issue != "none" else "info",
        "confidence": 0.86,
        "evidence_fields": evidence,
        "monitor_operator": operator,
        "monitor_threshold": threshold,
        "explanation_ru": "Классификация основана только на переданных метаданных",
    }


def model_response(candidates: list[dict[str, object]]) -> dict[str, object]:
    profiles = []
    for candidate in candidates:
        entity_id = str(candidate["entity_id"])
        if entity_id == "sensor.robot_service_medium":
            profiles.append(model_profile(
                entity_id,
                component="ZQ medium",
                role="consumable",
                issue="consumable_level",
                operator="less_or_equal",
                threshold=10.0,
                evidence=["translation_key", "unit", "current_state"],
            ))
        elif entity_id == "sensor.dishwasher_register_17":
            profiles.append(model_profile(
                entity_id,
                component="Diagnostic register",
                role="diagnostic",
                issue="error_code",
                operator="nonzero",
                threshold=None,
                evidence=["translation_key", "current_state", "integration"],
            ))
        else:
            profiles.append(model_profile(
                entity_id,
                component="temperature",
                role="measurement",
                issue="none",
                operator="none",
                threshold=None,
                evidence=["device_class", "unit", "current_state"],
            ))
    return {"message": {"content": json.dumps({"profiles": profiles}, ensure_ascii=False)}}


class ModelStudyTests(unittest.TestCase):
    def test_every_entity_is_classified_without_keyword_candidate_filter(self) -> None:
        candidates = study.collect_candidates(snapshot(), inventory())
        self.assertEqual(len(candidates), 3)
        self.assertEqual(
            {item["entity_id"] for item in candidates},
            {
                "sensor.robot_service_medium",
                "sensor.dishwasher_register_17",
                "sensor.room_temperature",
            },
        )
        self.assertFalse(hasattr(study, "CANDIDATE_RE"))
        self.assertFalse(hasattr(study, "expected_rule"))

    def test_model_cannot_change_scope_state_or_evidence(self) -> None:
        candidates = study.collect_candidates(snapshot(), inventory())
        profiles = study.validate_profiles(model_response(candidates), candidates)
        by_id = {item["entity_id"]: item for item in profiles}
        diagnostic = by_id["sensor.dishwasher_register_17"]
        self.assertEqual(diagnostic["issue_class"], "error_code")
        self.assertEqual(diagnostic["observed_state"]["value"], 17.0)
        self.assertEqual(diagnostic["physical_device_id"], DISHWASHER)
        self.assertNotIn("state_value", json.loads(
            model_response(candidates)["message"]["content"]
        )["profiles"][0])

        hostile = model_response(candidates)
        document = json.loads(hostile["message"]["content"])
        document["profiles"][0]["evidence_fields"].append("options")
        hostile["message"]["content"] = json.dumps(document)
        with self.assertRaises(study.StudyError):
            study.validate_profiles(hostile, candidates)

    def test_catalog_is_read_only_complete_and_private(self) -> None:
        document = study.build_catalog(
            snapshot_reader=lambda _action: (snapshot(), 0),
            inventory_loader=inventory,
            model_reader=model_response,
            now=lambda: 1_787_529_600,
        )
        self.assertEqual(document["schema_version"], 2)
        self.assertEqual(document["entity_count"], 3)
        self.assertEqual(document["classification_count"], 3)
        self.assertEqual(document["learning_scope"], "read_only")
        self.assertEqual(document["actions_performed"], 0)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            path = directory / "study.json"
            study.write_catalog(document, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_schema_one_migrates_idempotently_and_is_backed_up_before_write(self) -> None:
        old = {
            "schema_version": 1,
            "findings": [{
                "entity_id": "sensor.legacy_resource",
                "friendly_name": "Legacy resource",
                "physical_device_hash": ROBOT,
                "state_kind": "number",
                "category": "remaining_life",
                "alert_condition": "at_or_below_10",
            }],
        }
        migrated = study.migrate_catalog_document(old)
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["migrated_from_schema"], 1)
        self.assertEqual(
            migrated["profiles"][0]["recommended_monitoring_condition"],
            {"operator": "less_or_equal", "threshold": 10.0},
        )
        self.assertIs(study.migrate_catalog_document(migrated), migrated)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            path = directory / "study.json"
            path.write_text(json.dumps(old), encoding="utf-8")
            path.chmod(0o600)
            study.write_catalog(migrated, path)
            backup = directory / "study.json.schema1.backup"
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(backup.read_text())["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
