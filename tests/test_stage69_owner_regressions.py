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


if __name__ == "__main__":
    unittest.main()
