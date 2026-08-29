#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import bounded_ha_agent
import capability_catalog
import device_learning
import home_assistant_control
import home_assistant_mcp
import model_ha_proof


class Stage69OwnerRegressionTests(unittest.TestCase):
    def test_browser_chat_does_not_prefix_every_turn_with_direct_model(self):
        source = (PROJECT_DIR / "scripts/local_chat_gateway.py").read_text(encoding="utf-8")
        self.assertIn("const routedMessage=message;", source)
        self.assertNotIn("directMode.checked?'/модель '+message:message", source)

    def test_human_device_query_normalization(self):
        self.assertEqual(model_ha_proof.normalize_device_query("посудомойка"), "посудомойка")
        self.assertEqual(model_ha_proof.normalize_device_query("дисвашер"), "дисвашер")
        self.assertEqual(model_ha_proof.normalize_device_query("свет кабинет"), "свет кабинет")
        self.assertEqual(model_ha_proof.normalize_device_query("пылесос Roborock"), "пылесос Roborock")
        self.assertEqual(model_ha_proof.normalize_device_query("роботом Андреем"), "Андрей")

    def test_semantic_resolver_matches_type_plus_room(self):
        physical = "a" * 64
        inventory = {
            "entities": [{
                "entity_id": "light.office", "domain": "light",
                "physical_device_hash": physical, "friendly_name": "Кабинет",
                "area_name": "Кабинет", "area_aliases": [], "entity_aliases": [],
                "integration_domains": ["tuya_local"],
            }],
            "physical_devices": [{
                "physical_device_hash": physical, "display_name": "Кабинет",
                "entity_ids": ["light.office"], "area_names": ["Кабинет"],
                "manufacturers": [], "models": [], "capabilities": ["control"],
            }],
        }
        result = home_assistant_mcp.find_model_devices(inventory, query="свет кабинете")
        self.assertEqual(result["matched_device_count"], 1)

    def test_obvious_read_keeps_compound_query_when_display_name_is_ambiguous(self):
        target = "a" * 64
        other = "b" * 64
        inventory = {
            "entities": [
                {
                    "entity_id": "light.office", "domain": "light",
                    "physical_device_hash": target, "friendly_name": "Свет кабинет",
                    "area_name": "Кабинет", "area_aliases": [],
                    "entity_aliases": [], "integration_domains": ["tuya_local"],
                },
                {
                    "entity_id": "sensor.office_air", "domain": "sensor",
                    "physical_device_hash": other, "friendly_name": "Климат кабинет",
                    "area_name": "Кабинет", "area_aliases": [],
                    "entity_aliases": [], "integration_domains": ["tuya_local"],
                },
            ],
            "physical_devices": [
                {
                    "physical_device_hash": target, "display_name": "Кабинет",
                    "entity_ids": ["light.office"], "area_names": ["Кабинет"],
                    "manufacturers": [], "models": [], "capabilities": [],
                },
                {
                    "physical_device_hash": other, "display_name": "Климат",
                    "entity_ids": ["sensor.office_air"], "area_names": ["Кабинет"],
                    "manufacturers": [], "models": [], "capabilities": [],
                },
            ],
        }
        intent = bounded_ha_agent.resolve_obvious_read_intent(
            "Что со светом в кабинете?", [], inventory
        )
        self.assertIsNotNone(intent)
        self.assertNotEqual(intent.device_query, "Кабинет")
        result = home_assistant_mcp.find_model_devices(
            inventory, query=intent.device_query, limit=2
        )
        self.assertEqual(result["matched_device_count"], 1)
        self.assertEqual(result["devices"][0]["physical_device_id"], target)

    def test_semantic_resolver_maps_russian_dishwasher_to_english_registry_name(self):
        physical = "b" * 64
        inventory = {
            "entities": [{
                "entity_id": "switch.dw_power", "domain": "switch",
                "physical_device_hash": physical, "friendly_name": "Dishwasher Питание",
                "area_name": None, "area_aliases": [], "entity_aliases": [],
                "integration_domains": ["midea_ac_lan"],
            }],
            "physical_devices": [{
                "physical_device_hash": physical, "display_name": "Dishwasher 760EY174",
                "entity_ids": ["switch.dw_power"], "area_names": [],
                "manufacturers": ["Midea"], "models": ["760EY174"],
                "capabilities": ["control"],
            }],
        }
        result = home_assistant_mcp.find_model_devices(inventory, query="посудомойка")
        self.assertEqual(result["matched_device_count"], 1)

    def test_generic_unlearned_light_is_rendered_from_live_facts(self):
        result = {
            "display_name": "Кабинет", "areas": ["Кабинет"],
            "physical_availability": "available",
            "features": [{
                "human_name": "Свет кабинет", "component": "main",
                "semantic_role": "control", "domain": "light",
                "availability": "available",
                "state": {"kind": "enum", "value": "on"},
                "measurement_type": {"unit": None},
            }],
        }
        answer = model_ha_proof.render_device_observation(result, "свет кабинет")
        self.assertIn("Кабинет", answer)
        self.assertIn("включён", answer)

    def test_transient_switch_does_not_verify(self):
        values = iter(["off", "on", "on", "on", "off", "off", "off", "off", "off", "off", "off", "off", "off"])
        def snapshot_reader(mode):
            self.assertEqual(mode, "snapshot")
            return ({"status": "healthy", "entities": [{
                "entity_id": "switch.dw_power", "state_kind": "enum",
                "state_value": next(values),
            }]}, 0)
        with mock.patch.object(home_assistant_control.ha_read, "load_config", return_value=object()):
            result, exit_code = home_assistant_control.execute(
                "switch.dw_power", "turn_on", snapshot_reader=snapshot_reader,
                service_caller=lambda *_a, **_k: None, sleeper=lambda _s: None,
            )
        self.assertEqual(exit_code, 4)
        self.assertEqual(result["status"], "not_verified")

    def test_transport_acceptance_is_not_verified(self):
        capability = capability_catalog.Capability(
            capability_id="cap_" + "a" * 24, physical_device_id="b" * 64,
            device_name="Dishwasher", area_name=None, feature_name="Питание",
            domain="switch", action_id="turn_on", available=True, risk_class="R2",
            owner_confirmation="explicit_request", parameter_schema={
                "type": "object", "properties": {}, "required": [],
                "additionalProperties": False,
            }, verification_method="stable_state_matches_expected",
            entity_id="switch.dw_power",
        )
        catalogue = capability_catalog.CapabilityCatalog([capability])
        result = catalogue.execute(
            capability.capability_id, {}, explicit_owner_request=True,
            executor=lambda *_a: ({
                "status": "accepted", "verification_strength": "transport_only",
                "service_calls": 1, "delivery": "accepted",
            }, 0),
        )
        self.assertEqual(result["adapter_status"], "accepted_unverified")

    def test_plan_source_requires_every_step_verified(self):
        source = (PROJECT_DIR / "scripts/bounded_ha_agent.py").read_text(encoding="utf-8")
        self.assertIn('step_result.get("adapter_status") != "verified"', source)
        self.assertNotIn('item.get("adapter_status") in {"verified", "accepted"}', source)

    def test_unknown_devices_are_not_blocked_by_learning_profile(self):
        source = (PROJECT_DIR / "scripts/bounded_ha_agent.py").read_text(encoding="utf-8")
        self.assertIn("A real HA device must remain readable even before it has a learned profile", source)

    def test_stale_profile_does_not_filter_a_new_live_feature(self):
        physical = "c" * 64
        profile = {
            "schema_version": 1,
            "physical_device_id": physical,
            "display_name": "Климат кабинета",
            "device_type": "home_assistant_device",
            "features": [],
        }
        live = {
            "physical_device_id": physical,
            "display_name": "Климат кабинета",
            "areas": ["Кабинет"],
            "physical_availability": "available",
            "features": [{
                "entity_id": "sensor.office_air_quality",
                "human_name": "Качество воздуха",
                "component": "air_quality", "semantic_role": "measurement",
                "domain": "sensor", "availability": "available",
                "state": {"kind": "number", "value": 42},
                "measurement_type": {"unit": "%"},
            }],
        }
        compact = device_learning.compact_profile(
            profile, live, "Что с качеством воздуха?", maximum=3
        )
        self.assertEqual(compact["relevant_feature_count"], 1)
        self.assertEqual(compact["relevant_features"][0]["human_name"], "Качество воздуха")
        self.assertNotIn("entity_id", str(compact))

    def test_compact_live_read_keeps_area_for_where_question(self):
        physical = "d" * 64
        profile = {
            "schema_version": 1,
            "physical_device_id": physical,
            "display_name": "Климат кабинета",
            "device_type": "home_assistant_device",
            "features": [],
        }
        live = {
            "physical_device_id": physical,
            "display_name": "Климат кабинета",
            "areas": ["Кабинет"],
            "physical_availability": "available",
            "features": [{
                "entity_id": "sensor.office_status", "human_name": "Статус",
                "component": "status", "semantic_role": "status",
                "domain": "sensor", "availability": "available",
                "state": {"kind": "enum", "value": "idle"},
            }],
        }
        compact = device_learning.compact_profile(
            profile, live, "Где климат кабинета?", maximum=3
        )
        self.assertEqual(compact["areas"], ["Кабинет"])
        answer = device_learning.render_compact_observation(
            compact, "Где климат кабинета?"
        )
        self.assertIn("Кабинет", answer)
        self.assertEqual(
            device_learning.validate_compact_answer(
                compact, "Где климат кабинета?", answer
            ),
            [],
        )

    def test_compact_causal_question_keeps_unknown_cause_unknown(self):
        compact = {
            "display_name": "Андрей",
            "unknown_cause_stays_unknown": True,
            "relevant_features": [],
            "unavailable_feature_count": 1,
        }
        answer = device_learning.render_compact_observation(compact, "Это Wi-Fi?")
        self.assertEqual(
            answer,
            "Андрей: причина по текущим данным не подтверждена.",
        )
        self.assertEqual(
            device_learning.validate_compact_answer(compact, "Это Wi-Fi?", answer),
            [],
        )
        self.assertIn(
            "unknown_cause_not_preserved",
            device_learning.validate_compact_answer(
                compact, "Это Wi-Fi?", "Да, это Wi-Fi."
            ),
        )

    def test_compact_multi_fact_question_keeps_status_area_and_battery(self):
        compact = {
            "display_name": "Андрей",
            "areas": ["Кухня"],
            "physical_availability": "available",
            "unavailable_feature_count": 0,
            "relevant_features": [
                {
                    "human_name": "Статус",
                    "component": "main_status",
                    "availability": "available",
                    "state": {"kind": "enum", "value": "charging"},
                },
                {
                    "human_name": "Батарея",
                    "component": "battery",
                    "availability": "available",
                    "state": {"kind": "number", "value": 100},
                },
            ],
        }
        question = "Что сейчас делает Андрей, где он и сколько у него заряда?"
        answer = device_learning.render_compact_observation(compact, question)
        self.assertIn("Кухня", answer)
        self.assertIn("заряжается", answer)
        self.assertIn("100%", answer)
        self.assertEqual(
            device_learning.validate_compact_answer(compact, question, answer), []
        )

    def test_generic_causal_question_does_not_invent_wifi_cause(self):
        answer = model_ha_proof.render_device_observation(
            {
                "display_name": "Roborock S5 Max",
                "features": [],
                "physical_availability": "available",
            },
            "Это Wi-Fi?",
        )
        self.assertEqual(
            answer,
            "Roborock S5 Max: причина по текущим данным не подтверждена.",
        )

    def test_equal_action_capabilities_require_clarification(self):
        capabilities = [
            {"capability_id": "cap_" + "a" * 24, "action_id": "turn_on", "feature_name": "Канал 1"},
            {"capability_id": "cap_" + "b" * 24, "action_id": "turn_on", "feature_name": "Канал 2"},
        ]
        filtered, status, options = bounded_ha_agent._filter_action_capabilities_for_owner(
            capabilities,
            bounded_ha_agent.OwnerIntent("ha_action", "Реле", "включить", None, False),
        )
        self.assertEqual(filtered, [])
        self.assertEqual(status, "clarification_required")
        self.assertEqual(options, ["Канал 1", "Канал 2"])

    def test_unique_primary_power_hides_secondary_switches(self):
        capabilities = [
            {"capability_id": "cap_" + "a" * 24, "action_id": "turn_on", "feature_name": "Питание"},
            {"capability_id": "cap_" + "b" * 24, "action_id": "turn_on", "feature_name": "Storage"},
            {"capability_id": "cap_" + "c" * 24, "action_id": "turn_on", "feature_name": "Половинная загрузка"},
        ]
        filtered, status, options = bounded_ha_agent._filter_action_capabilities_for_owner(
            capabilities,
            bounded_ha_agent.OwnerIntent("ha_action", "Посудомойка", "включить", None, False),
        )
        self.assertEqual([item["feature_name"] for item in filtered], ["Питание"])
        self.assertEqual(status, "unique_primary")
        self.assertEqual(options, [])

    def test_ambiguous_read_is_clarified_before_model_can_choose(self):
        first = "1" * 64
        second = "2" * 64
        inventory = {
            "entities": [
                {
                    "entity_id": "vacuum.andrew",
                    "domain": "vacuum",
                    "physical_device_hash": first,
                    "friendly_name": "Робот Андрей",
                    "area_name": "Кухня",
                    "area_aliases": [],
                    "entity_aliases": [],
                    "integration_domains": ["xiaomi_miot"],
                },
                {
                    "entity_id": "vacuum.roborock",
                    "domain": "vacuum",
                    "physical_device_hash": second,
                    "friendly_name": "Робот Roborock",
                    "area_name": "Кухня",
                    "area_aliases": [],
                    "entity_aliases": [],
                    "integration_domains": ["xiaomi_miot"],
                },
            ],
            "physical_devices": [
                {
                    "physical_device_hash": first,
                    "display_name": "Андрей",
                    "entity_ids": ["vacuum.andrew"],
                    "area_names": ["Кухня"],
                    "manufacturers": [], "models": [], "capabilities": [],
                },
                {
                    "physical_device_hash": second,
                    "display_name": "Roborock S5 Max",
                    "entity_ids": ["vacuum.roborock"],
                    "area_names": ["Кухня"],
                    "manufacturers": [], "models": [], "capabilities": [],
                },
            ],
        }

        def unexpected_model_call(*_args, **_kwargs):
            self.fail("ambiguous physical targets must not be offered to the model")

        answer = bounded_ha_agent.run_tool_loop(
            "Что с роботом?",
            {},
            [],
            bounded_ha_agent.OwnerIntent(
                "ha_read", "робот", None, None, False
            ),
            inventory_loader=lambda: inventory,
            ollama_call=unexpected_model_call,
            snapshot_reader=lambda _mode: ({"status": "healthy", "entities": []}, 0),
            control_catalogue_reader=lambda _mode: ({"status": "healthy", "entities": []}, 0),
            control_executor=lambda *_args, **_kwargs: self.fail("action attempted"),
            onboarding_reader=lambda: {},
        )
        self.assertIn("несколько устройств", answer)
        self.assertIn("Андрей", answer)
        self.assertIn("Roborock S5 Max", answer)


if __name__ == "__main__":
    unittest.main()
