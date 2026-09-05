"""Frozen generic read/area provenance expectations, defined before the fix."""

from __future__ import annotations

import copy
import hashlib
import sys
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]

import bounded_ha_agent as agent
import home_assistant_inventory as inventory
import home_assistant_mcp as resolver
import stage71_oracle as oracle


class GenericReadCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        areas = [
            {"area_id": "workshop", "name": "Мастерская", "aliases": ["Творческая"]},
            {"area_id": "pantry", "name": "Кладовая", "aliases": []},
            {"area_id": "attic", "name": "Мансарда", "aliases": []},
        ]
        specs = [
            ("named1", "мастерская", None, "switch"),
            ("named2", "мастерская", "workshop", "switch"),
            ("named3", "мастерская", None, "switch"),
            ("foreign", "очиститель", "workshop", "humidifier"),
            ("duct", "вентилятор в кладовой", "pantry", "fan"),
            ("otherfan", "вентилятор мансарды", "attic", "fan"),
            ("atticlight", "бра", "attic", "light"),
            ("temperature", "микроклимат", "workshop", "sensor"),
            ("unbound", "светильник творческой", None, "light"),
            ("conflict", "лампа кладовой", "workshop", "light"),
            ("multi", "лампа мастерской и кладовой", None, "light"),
        ]
        devices = [{"id": key, "name": label, "area_id": area} for key, label, area, _domain in specs]
        entities, states, snapshot_entities = [], [], []
        for key, label, area, domain in [*specs, ("logical", "уборка мастерской", "workshop", "input_boolean")]:
            entity_id = f"{domain}.fixture_{key}"
            entities.append({
                "id": key, "entity_id": entity_id, "original_name": label,
                "device_id": key if key != "logical" else None,
                "area_id": area, "platform": "fixture",
                "translation_key": "temperature" if key == "temperature" else None,
            })
            attributes = {"friendly_name": label}
            if key == "temperature":
                attributes.update(device_class="temperature", unit_of_measurement="°C")
            states.append({"entity_id": entity_id, "state": "20" if key == "temperature" else "off", "attributes": attributes})
            snapshot_entities.append({
                "entity_id": entity_id, "state_kind": "number" if key == "temperature" else "on_off",
                "state_value": 20 if key == "temperature" else "off",
                "source_last_updated_at": "2026-09-01T00:00:00Z",
            })
        self.graph = inventory.build_inventory(entities, devices, areas, states)
        self.refs = {
            key: next(row["target_ref"] for row in self.graph["entities"] if row["entity_id"] == f"{domain}.fixture_{key}")
            for key, _label, _area, domain in [*specs, ("logical", "", None, "input_boolean")]
        }
        self.snapshot = {"observed_at": "2026-09-05T00:00:00Z", "entities": snapshot_entities, "service_calls": 0}

    def turn(self, text: str) -> agent.TurnResult:
        return agent.process_turn(
            text, {"session_focus": agent.SessionFocus()}, [],
            inventory_loader=lambda: self.graph,
            snapshot_reader=lambda _command: (self.snapshot, 0),
            ollama_call=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("unexpected model")),
            trace_sink=None,
        )

    def test_room_only_status_or_power_does_not_override_named_devices(self) -> None:
        expected = {self.refs[key] for key in ("named1", "named2", "named3")}
        for query in (
            "Какой статус у устройств мастерской?", "Покажи питание в мастерской",
            "Какое состояние мастерской?",
        ):
            with self.subTest(query=query):
                result = self.turn(query)
                self.assertEqual(result.frame.kind, "clarification")
                self.assertEqual(set(result.frame.clarification_target_refs), expected)
                self.assertEqual(result.receipts, ())

    def test_inflected_name_with_room_keeps_requested_device_and_feature(self) -> None:
        for feature, word in (("status", "статус"), ("power", "питание")):
            result = self.turn(f"Покажи {word} вентилятора в кладовой")
            report = oracle.evaluate_turn(result, self.graph, self.snapshot, [self.refs["duct"]], [feature])
            self.assertEqual(report, oracle.OracleResult())
            self.assertTrue(result.receipts)

    def test_quantitative_feature_plus_area_still_reads(self) -> None:
        result = self.turn("Покажи температуру в мастерской")
        self.assertEqual({s.target_ref for s in result.frame.selections}, {self.refs["temperature"]})
        self.assertIn("20°C", result.answer)

    def test_shadow_action_with_implicit_type_preserves_existing_unique_area_plan(self) -> None:
        result = self.turn("Можешь погасить в мансарде")
        self.assertIsNotNone(result.action_plan)
        self.assertEqual(result.action_plan.target_label, "бра")
        self.assertEqual(result.action_plan.areas, ("Мансарда",))
        self.assertEqual(result.action_plan.action, "turn_off")
        self.assertEqual(result.action_plan.service_calls, 0)

    def test_registry_and_inferred_areas_are_distinct_and_ephemeral(self) -> None:
        before = copy.deepcopy(self.graph)
        context = resolver.target_context(self.graph, self.refs["unbound"])
        self.assertEqual(context["registry_areas"], [])
        self.assertEqual(context["inferred_areas"], ["Мастерская"])
        self.assertEqual(self.graph, before)
        result = self.turn("Покажи статус светильник творческой")
        self.assertTrue(result.receipts)
        self.assertEqual(result.receipts[0].areas, ())
        self.assertEqual(oracle.evaluate_turn(result, self.graph, self.snapshot, [self.refs["unbound"]], ["status"]), oracle.OracleResult())

    def test_registry_binding_wins_and_multi_room_name_does_not_infer(self) -> None:
        conflict = resolver.target_context(self.graph, self.refs["conflict"])
        self.assertEqual(conflict["registry_areas"], ["Мастерская"])
        self.assertEqual(conflict["inferred_areas"], [])
        multi = resolver.target_context(self.graph, self.refs["multi"])
        self.assertEqual(multi["registry_areas"], [])
        self.assertEqual(multi["inferred_areas"], [])

    def test_independent_oracle_rejects_promoting_inference_or_losing_registry(self) -> None:
        for key, query, forged_areas in (
            ("unbound", "Покажи статус светильник творческой", ("Мастерская",)),
            ("conflict", "Покажи статус лампа кладовой", ()),
        ):
            result = self.turn(query)
            self.assertTrue(result.receipts)
            forged = replace(result, receipts=tuple(replace(r, areas=forged_areas) for r in result.receipts))
            report = oracle.evaluate_turn(forged, self.graph, self.snapshot, [self.refs[key]], ["status"])
            self.assertGreater(report.invented_facts, 0)
            self.assertGreater(report.lost_requested_values, 0)

    def test_original_blind_expectations_are_frozen(self) -> None:
        path = ROOT / "tests/data/stage71_blind_owner.jsonl"
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), "4ff8119abb39dd0e33b17c239699f8df15f372f414aa743d256c234eab602376")


if __name__ == "__main__":
    unittest.main()
