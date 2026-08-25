#!/usr/bin/env python3
"""Contracts for the DeviceGraph-derived bounded capability catalogue."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import capability_catalog as catalog  # noqa: E402


PHYSICAL = "a" * 64


def inventory() -> dict:
    return {
        "schema_version": 3,
        "entities": [
            {"entity_id": "vacuum.andrei", "physical_device_hash": PHYSICAL},
            {"entity_id": "select.andrei_mode", "physical_device_hash": PHYSICAL},
            {"entity_id": "number.andrei_volume", "physical_device_hash": PHYSICAL},
            {"entity_id": "siren.andrei_alarm", "physical_device_hash": PHYSICAL},
        ],
        "physical_devices": [{
            "physical_device_hash": PHYSICAL,
            "display_name": "Андрей",
            "area_names": ["Кухня"],
            "entity_ids": [
                "vacuum.andrei", "select.andrei_mode", "number.andrei_volume",
                "siren.andrei_alarm",
            ],
        }],
    }


def control_document() -> dict:
    return {
        "control_entities": [
            {"entity_id": "vacuum.andrei", "friendly_name": "Андрей", "available": True},
            {
                "entity_id": "select.andrei_mode", "friendly_name": "Андрей режим",
                "available": True, "options": ["Тихий", "Турбо"],
            },
            {
                "entity_id": "number.andrei_volume", "friendly_name": "Андрей громкость",
                "available": True, "min": 0, "max": 10, "step": 1,
            },
            {
                "entity_id": "siren.andrei_alarm", "friendly_name": "Андрей аларм",
                "available": True,
            },
            {
                "entity_id": "switch.orphan", "friendly_name": "Чужой switch",
                "available": True,
            },
        ]
    }


class CapabilityCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalogue = catalog.CapabilityCatalog.from_documents(
            control_document(), inventory()
        )

    def capability(self, *, domain: str, action: str) -> dict:
        return next(
            item for item in self.catalogue.model_view(PHYSICAL)["capabilities"]
            if item["domain"] == domain and item["action_id"] == action
        )

    def test_model_view_uses_opaque_ids_and_closed_parameter_schemas(self) -> None:
        document = self.catalogue.model_view(PHYSICAL)
        self.assertGreaterEqual(document["capability_count"], 8)
        self.assertNotIn("entity_id", str(document))
        self.assertNotIn("/api/services", str(document))
        for item in document["capabilities"]:
            self.assertRegex(item["capability_id"], r"^cap_[a-f0-9]{24}$")
            self.assertFalse(item["parameters"]["additionalProperties"])
        self.assertFalse(any("orphan" in str(item) for item in document["capabilities"]))

    def test_select_and_number_constraints_come_from_live_catalogue(self) -> None:
        select = self.capability(domain="select", action="set_option")
        self.assertEqual(select["parameters"]["properties"]["value"]["enum"], ["Тихий", "Турбо"])
        number = self.capability(domain="number", action="set_value")
        value = number["parameters"]["properties"]["value"]
        self.assertEqual((value["minimum"], value["maximum"], value["multipleOf"]), (0.0, 10.0, 1.0))

    def test_r2_execution_uses_private_entity_and_returns_readback_only(self) -> None:
        capability = self.capability(domain="vacuum", action="return_home")
        executor = mock.Mock(return_value=({
            "status": "accepted", "verification": "get_readback_completed",
            "before_state": "cleaning", "after_state": "returning",
            "service_calls": 1,
        }, 0))
        result = self.catalogue.execute(
            capability["capability_id"], {}, explicit_owner_request=True,
            executor=executor,
        )
        executor.assert_called_once_with("vacuum.andrei", "return_home")
        self.assertEqual(result["verification"], "get_readback_completed")
        self.assertNotIn("entity_id", result)

    def test_action_requires_explicit_owner_request_and_one_exact_parameter(self) -> None:
        capability = self.capability(domain="select", action="set_option")
        executor = mock.Mock()
        with self.assertRaises(catalog.CapabilityCatalogError):
            self.catalogue.execute(
                capability["capability_id"], {"value": "Турбо"},
                explicit_owner_request=False, executor=executor,
            )
        with self.assertRaises(catalog.CapabilityCatalogError):
            self.catalogue.execute(
                capability["capability_id"], {"value": "Несуществующий"},
                explicit_owner_request=True, executor=executor,
            )
        executor.assert_not_called()

    def test_siren_is_r3_and_needs_separate_confirmation(self) -> None:
        capability = self.capability(domain="siren", action="turn_on")
        self.assertEqual(capability["risk_class"], "R3")
        executor = mock.Mock(return_value=({"status": "verified"}, 0))
        with self.assertRaises(catalog.CapabilityCatalogError):
            self.catalogue.execute(
                capability["capability_id"], {}, explicit_owner_request=True,
                executor=executor,
            )
        executor.assert_not_called()

    def test_numeric_step_is_enforced_before_adapter(self) -> None:
        capability = self.capability(domain="number", action="set_value")
        executor = mock.Mock()
        with self.assertRaises(catalog.CapabilityCatalogError):
            self.catalogue.execute(
                capability["capability_id"], {"value": 2.5},
                explicit_owner_request=True, executor=executor,
            )
        executor.assert_not_called()


if __name__ == "__main__":
    unittest.main()

