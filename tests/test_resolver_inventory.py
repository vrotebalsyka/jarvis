from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import home_assistant_inventory as inventory
import home_assistant_mcp as resolver
from test_stage70_core import graph, snapshot


class ResolverInventoryTests(unittest.TestCase):
    def test_physical_identity_is_stable_and_separates_devices(self) -> None:
        first = inventory._physical_hash("device-a", "sensor.one")
        self.assertEqual(first, inventory._physical_hash("device-a", "sensor.other"))
        self.assertNotEqual(first, inventory._physical_hash("device-b", "sensor.one"))
        self.assertEqual(len(first), 64)

    def test_migration_discards_unrelated_top_level_overlays(self) -> None:
        document = graph()
        document["network"] = {"secret": "must disappear"}
        document["recovery_profiles"] = [1]
        migrated = inventory.migrate_inventory_document(document)
        self.assertNotIn("network", migrated)
        self.assertNotIn("recovery_profiles", migrated)

    def test_resolver_uses_name_type_and_area(self) -> None:
        result = resolver.find_model_devices(graph(), query="посудомойка кухня")
        self.assertEqual(result["matched_device_count"], 1)
        self.assertEqual(result["devices"][0]["display_name"], "посудомойка")

    def test_exact_room_named_device_outranks_area_only_device(self) -> None:
        document = graph()
        result = resolver.find_model_devices(document, query="свет кухни")
        self.assertEqual(
            {item["display_name"] for item in result["devices"]}, {"свет кухни"}
        )

    def test_natural_punctuation_is_accepted(self) -> None:
        self.assertEqual(resolver.normalize_device_query("Проверь, пожалуйста, зеркало?"), "зеркало")

    def test_details_join_fresh_state_to_inventory_identity(self) -> None:
        document = graph()
        device = document["physical_devices"][0]
        details = resolver.get_model_device_details(snapshot(document), document, device["physical_device_hash"])
        self.assertEqual(details["source"], "fresh Home Assistant read via inventory identity")
        self.assertEqual(details["features"][0]["state"]["value"], "off")


if __name__ == "__main__":
    unittest.main()
