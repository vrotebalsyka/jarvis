#!/usr/bin/env python3
"""Contracts for the persistent read-only HA device knowledge catalog."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import ha_device_knowledge as knowledge  # noqa: E402


def device(
    suffix: str,
    name: str,
    *,
    integrations: list[str],
    platforms: list[str],
    entities: list[str],
) -> dict[str, object]:
    return {
        "physical_device_hash": suffix * 64,
        "display_name": name,
        "config_domains": integrations,
        "platforms": platforms,
        "entity_ids": entities,
        "available_entity_count": len(entities),
        "unavailable_entity_count": 0,
        "network_status": "stable",
        "safety_class": "ordinary_relay",
    }


class DeviceKnowledgeTests(unittest.TestCase):
    def test_baseline_groups_entities_and_preserves_multiple_paths(self) -> None:
        inventory = {
            "observed_at": "2026-08-17T12:00:00+00:00",
            "physical_devices": [device(
                "a",
                "Зеркало",
                integrations=["tuya", "localtuya"],
                platforms=["tuya", "localtuya"],
                entities=["switch.mirror", "switch.mirror_local"],
            )],
        }
        result = knowledge.build_catalog(inventory, now=100)
        self.assertEqual(result["active_physical_device_count"], 1)
        self.assertEqual(result["new_device_count"], 0)
        self.assertEqual(result["multiple_connection_device_count"], 1)
        self.assertEqual(result["devices"][0]["lifecycle"], "baseline")
        self.assertEqual(result["devices"][0]["entity_count"], 2)
        self.assertEqual(result["actions_performed"], 0)

    def test_next_refresh_marks_only_new_physical_device(self) -> None:
        initial = knowledge.build_catalog({
            "physical_devices": [device(
                "a", "Зеркало", integrations=["tuya"], platforms=["tuya"],
                entities=["switch.mirror"],
            )],
        }, now=100)
        current = knowledge.build_catalog({
            "physical_devices": [
                device(
                    "a", "Зеркало", integrations=["tuya"], platforms=["tuya"],
                    entities=["switch.mirror"],
                ),
                device(
                    "b", "Новый датчик", integrations=["zha"], platforms=["zha"],
                    entities=["sensor.new_device"],
                ),
            ],
        }, initial, now=200)
        states = {item["display_name"]: item["lifecycle"] for item in current["devices"]}
        self.assertEqual(states, {"Зеркало": "known", "Новый датчик": "new"})
        self.assertEqual(current["new_device_count"], 1)
        mirror = next(item for item in current["devices"] if item["display_name"] == "Зеркало")
        self.assertEqual(mirror["first_seen_epoch"], 100)

    def test_new_device_stays_new_for_a_day_then_becomes_known(self) -> None:
        baseline = knowledge.build_catalog({
            "physical_devices": [device(
                "a", "Зеркало", integrations=["tuya"], platforms=["tuya"],
                entities=["switch.mirror"],
            )],
        }, now=100)
        first_new = knowledge.build_catalog({
            "physical_devices": [
                device(
                    "a", "Зеркало", integrations=["tuya"], platforms=["tuya"],
                    entities=["switch.mirror"],
                ),
                device(
                    "b", "Новый датчик", integrations=["zha"], platforms=["zha"],
                    entities=["sensor.new_device"],
                ),
            ],
        }, baseline, now=200)
        still_new = knowledge.build_catalog({
            "physical_devices": first_new["devices"],
        }, first_new, now=300)
        new_item = next(
            item for item in still_new["devices"]
            if item["display_name"] == "Новый датчик"
        )
        self.assertEqual(new_item["lifecycle"], "new")

        known = knowledge.build_catalog({
            "physical_devices": first_new["devices"],
        }, still_new, now=200 + knowledge.NEW_DEVICE_WINDOW_SECONDS)
        known_item = next(
            item for item in known["devices"]
            if item["display_name"] == "Новый датчик"
        )
        self.assertEqual(known_item["lifecycle"], "known")

    def test_missing_device_is_retained_as_history(self) -> None:
        initial = knowledge.build_catalog({
            "physical_devices": [device(
                "a", "Зеркало", integrations=["tuya"], platforms=["tuya"],
                entities=["switch.mirror"],
            )],
        }, now=100)
        current = knowledge.build_catalog({"physical_devices": []}, initial, now=200)
        self.assertFalse(current["devices"][0]["active"])
        self.assertEqual(
            current["devices"][0]["lifecycle"], "removed_from_current_registry"
        )

    def test_compact_context_explains_ontology_without_private_hash(self) -> None:
        catalog = knowledge.build_catalog({
            "physical_devices": [device(
                "a", "Посудомойка", integrations=["midea_ac_lan"],
                platforms=["midea_ac_lan"], entities=["switch.dishwasher_power"],
            )],
        }, now=100)
        result = knowledge.compact_context(catalog, "изучи посудомойку")
        self.assertEqual(result["active_physical_device_count"], 1)
        self.assertEqual(
            result["matched_or_catalog_devices"][0]["display_name"], "Посудомойка"
        )
        self.assertNotIn("physical_device_hash", str(result))
        self.assertIn("несколь", result["ontology"]["integration_path"])


if __name__ == "__main__":
    unittest.main()
