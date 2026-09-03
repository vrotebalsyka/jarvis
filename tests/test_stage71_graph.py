from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]

import home_assistant_inventory as inventory
import home_assistant_mcp as resolver
import stage71_fixtures as fixtures
import stage71_oracle as oracle


class HomeGraphContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = fixtures.graph()

    def test_schema_is_metadata_only_and_covers_enabled_current(self) -> None:
        self.assertEqual(self.graph["schema_version"], 5)
        self.assertEqual(oracle.persistent_current_fields(self.graph), [])
        coverage = oracle.coverage(fixtures.ENTITIES, fixtures.raw_states(), self.graph)
        self.assertEqual(coverage["missing_enabled_current"], 0)
        self.assertEqual(coverage["enabled_current"], coverage["represented_enabled_current"])

    def test_one_graph_has_all_node_kinds_and_bindings(self) -> None:
        self.assertGreaterEqual(len(self.graph["physical_nodes"]), 10)
        self.assertGreaterEqual(len(self.graph["logical_nodes"]), 3)
        self.assertEqual(len(self.graph["area_nodes"]), 4)
        self.assertGreaterEqual(len(self.graph["integration_nodes"]), 2)
        self.assertTrue(all("target_ref" in item and "entity_ref" in item for item in self.graph["entities"]))
        self.assertTrue(any(item["integration_refs"] for item in self.graph["entities"]))
        self.assertTrue(any(item["area_ref"] for item in self.graph["entities"]))

    def test_safe_metadata_types_survive(self) -> None:
        battery = next(item for item in self.graph["entities"] if item["translation_key"] == "battery" and item["state_class"] == "measurement")
        self.assertEqual(battery["entity_category"], "diagnostic")
        self.assertEqual(battery["device_class"], "battery")
        self.assertEqual(battery["state_class"], "measurement")
        self.assertEqual(battery["unit"], "%")
        self.assertEqual(battery["platform"], "fixture")
        self.assertTrue(battery["integration_refs"])
        self.assertTrue(battery["area_ref"])
        andrew = next(item for item in self.graph["physical_nodes"] if item["display_name"] == "Андрей")
        self.assertIn("Робот Андрей", andrew["aliases"])
        self.assertTrue(andrew["names"])
        camera = next(item for item in self.graph["entities"] if item["translation_key"] == "recording_mode")
        self.assertEqual(camera["options"], ["continuous", "motion"])
        self.assertIsInstance(camera["supported_features"], int)
        interval = next(item for item in self.graph["entities"] if item["translation_key"] == "alarm_interval")
        self.assertEqual((interval["min"], interval["max"], interval["step"]), (5, 120, 5))
        disabled = next(item for item in self.graph["entities"] if item["translation_key"] is None and item["disabled"])
        self.assertTrue(disabled["disabled"])
        self.assertFalse(disabled["hidden"])
        self.assertTrue(any(item["hidden"] for item in self.graph["entities"]))

    def test_same_name_model_and_room_do_not_merge_physical_identity(self) -> None:
        mirrors = [item for item in self.graph["physical_nodes"] if item["display_name"] == "зеркало"]
        self.assertEqual(len(mirrors), 2)
        self.assertNotEqual(mirrors[0]["target_ref"], mirrors[1]["target_ref"])
        self.assertTrue(all(item["strong_identity"] == "device_registry_id_hash" for item in mirrors))

    def test_registry_and_state_only_logical_nodes_are_one_to_one(self) -> None:
        logical = self.graph["logical_nodes"]
        self.assertTrue(any(item["strong_identity"] == "entity_registry_id_hash" for item in logical))
        self.assertTrue(any(item["strong_identity"] == "exact_entity_id_hash" for item in logical))
        self.assertTrue(all(len(item["entity_refs"]) == 1 for item in logical))

    def test_state_bearing_old_inventory_is_rejected(self) -> None:
        poisoned = copy.deepcopy(self.graph)
        poisoned["entities"][0]["state_value"] = "on"
        with self.assertRaises(inventory.InventoryError):
            inventory.validate_inventory_document(poisoned)


class OrderedResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = fixtures.graph()

    def labels(self, utterance: str, feature: str) -> tuple[str, list[str]]:
        result = resolver.resolve_targets(self.graph, utterance, feature)
        return result.tier, [str(item["display_name"]) for item in result.candidates]

    def test_required_precedence_alias_before_exact_name(self) -> None:
        document = copy.deepcopy(self.graph)
        andrew = next(item for item in document["physical_nodes"] if item["display_name"] == "Андрей")
        andrew["aliases"].append("зеркало")
        result = resolver.resolve_targets(document, "покажи зеркало", "status")
        self.assertEqual(result.tier, "exact_alias")
        self.assertEqual(result.target_refs, (andrew["target_ref"],))

    def test_name_area_type_entity_domain_model_and_typo(self) -> None:
        self.assertEqual(self.labels("заряд у Андрей", "battery")[1], ["Андрей"])
        self.assertEqual(self.labels("температура в кабинете", "temperature")[1], ["климат кабинета"])
        self.assertEqual(self.labels("режим камеры CW700S", "mode")[1], ["камера CW700S"])
        self.assertEqual(self.labels("посудамойка работает", "status")[1], ["посудомойка"])
        self.assertEqual(self.labels("какой зарят у Roborok S5 Max", "battery")[1], ["Roborock S5 Max"])

    def test_exact_area_type_and_manufacturer_model_are_real_tiers(self) -> None:
        tier, labels = self.labels("покажи свет кабинет", "status")
        self.assertEqual((tier, labels), ("exact_area_type", ["свет кабинета"]))
        tier, labels = self.labels("покажи статус модели S5 Max", "status")
        self.assertEqual((tier, labels), ("manufacturer_model", ["Roborock S5 Max"]))

    def test_every_ordered_tier_is_observable(self) -> None:
        document = copy.deepcopy(self.graph)
        andrew = next(item for item in document["physical_nodes"] if item["display_name"] == "Андрей")
        andrew["aliases"].append("домашний робот")
        cases = (
            (document, "домашний робот", "status", "exact_alias"),
            (self.graph, "Андрей", "status", "exact_name"),
            (self.graph, "свет кабинет", "status", "exact_area_type"),
            (self.graph, "Андрюша", "status", "entity_name_alias"),
            (self.graph, "robot", "status", "domain_device_class"),
            (self.graph, "S5 Max", "status", "manufacturer_model"),
            (self.graph, "посудамойка", "status", "morphology_typo"),
        )
        for graph, text, feature, tier in cases:
            self.assertEqual(resolver.resolve_targets(graph, text, feature).tier, tier)

    def test_ambiguity_is_preserved(self) -> None:
        tier, labels = self.labels("покажи зеркало", "status")
        self.assertEqual(tier, "exact_name")
        self.assertEqual(labels, ["зеркало", "зеркало"])

    def test_anti_regressions_target_identity(self) -> None:
        brush_labels = self.labels("ресурс основной щетки Андрея", "main_brush")[1]
        self.assertEqual(brush_labels, ["Андрей"])
        self.assertNotIn("Компьютер", brush_labels)
        andrew = self.labels("заряд Андрей", "battery")[1]
        roborock = self.labels("заряд Roborock S5 Max", "battery")[1]
        self.assertEqual((andrew, roborock), (["Андрей"], ["Roborock S5 Max"]))
        self.assertNotEqual(andrew, roborock)
        self.assertNotEqual(
            self.labels("температура в ванной", "temperature")[1],
            self.labels("температура в кабинете", "temperature")[1],
        )

    def test_feature_resolver_closed_vocabulary(self) -> None:
        examples = {
            "питание": "power", "заряд": "battery", "фильтр": "filter",
            "основная щетка": "main_brush", "боковая щётка": "side_brush",
            "влажность": "humidity", "температура": "temperature",
            "защита от детей": "child_lock", "режим": "mode", "ошибка": "error",
            "расходники": "consumables", "состояние": "status",
        }
        for text, expected in examples.items():
            self.assertEqual(resolver.resolve_feature(text), expected)
        self.assertEqual(resolver.resolve_feature("неизвестный параметр"), "unknown")


if __name__ == "__main__":
    unittest.main()
