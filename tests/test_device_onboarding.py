#!/usr/bin/env python3
"""Contracts for owner-confirmed onboarding of new physical devices."""

from __future__ import annotations

import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import device_onboarding as onboarding  # noqa: E402


PHYSICAL_HASH = "a" * 64
DEVICE_ID = "1" * 32


def documents(
    *,
    display_name: str = "Комнатный датчик",
    areas: list[str] | None = None,
    integrations: list[str] | None = None,
    safety_class: str = "sensor",
    local_profile: bool = False,
) -> tuple[dict[str, object], dict[str, object]]:
    paths = integrations or ["tuya"]
    inventory = {
        "physical_devices": [{
            "physical_device_hash": PHYSICAL_HASH,
            "device_ids": [DEVICE_ID],
            "display_name": display_name,
            "area_names": ["Спальня"] if areas is None else areas,
            "area_aliases": ["комната"],
            "manufacturers": ["Example"],
            "models": ["TH-1"],
            "config_domains": paths,
            "platforms": paths,
            "capabilities": ["observe"],
            "safety_class": safety_class,
            "network_status": "stable",
        }],
        "entities": [{
            "entity_id": "sensor.room_temperature",
            "physical_device_hash": PHYSICAL_HASH,
            "friendly_name": "Комнатный датчик Температура",
            "component": "Температура",
            "entity_aliases": ["температура в комнате"],
            "semantic_attributes": {"device_class": "temperature"},
            "semantic_role": "measurement",
            "capability": "observe",
            "diagnostic_relevance": False,
        }, {
            "entity_id": "sensor.room_battery",
            "physical_device_hash": PHYSICAL_HASH,
            "friendly_name": "Комнатный датчик Батарея",
            "component": "Батарея",
            "entity_aliases": [],
            "semantic_attributes": {"device_class": "battery"},
            "semantic_role": "maintenance",
            "capability": "observe",
            "diagnostic_relevance": True,
        }],
        "integration_profiles": ([{
            "domain": "localtuya", "entry_count": 1,
        }] if local_profile else []),
    }
    knowledge = {
        "devices": [{
            "physical_device_hash": PHYSICAL_HASH,
            "active": True,
            "lifecycle": "new",
        }],
    }
    return inventory, knowledge


def prepared_queue(*, local_profile: bool = False) -> dict[str, object]:
    inventory, knowledge = documents(local_profile=local_profile)
    return onboarding.refresh_queue(inventory, knowledge, now=100)


class DeviceOnboardingTests(unittest.TestCase):
    def test_new_device_is_collected_without_action_or_private_model_ids(self) -> None:
        queue = prepared_queue()
        self.assertEqual(queue["pending_count"], 1)
        self.assertEqual(queue["actions_performed"], 0)
        item = queue["items"][0]
        discovery = item["discovery"]
        self.assertEqual(discovery["manufacturers"], ["Example"])
        self.assertEqual(discovery["models"], ["TH-1"])
        self.assertEqual(discovery["device_classes"], ["battery", "temperature"])
        self.assertEqual(discovery["diagnostic_features"], ["Батарея"])
        self.assertEqual(discovery["network_identity_status"], "stable")
        public = onboarding.model_view(queue)
        rendered = str(public)
        self.assertNotIn(PHYSICAL_HASH, rendered)
        self.assertNotIn(DEVICE_ID, rendered)
        self.assertNotIn("sensor.room_temperature", rendered)

    def test_known_name_area_and_single_path_are_not_asked_again(self) -> None:
        queue = prepared_queue()
        self.assertEqual(queue["items"][0]["questions"], [])

    def test_only_missing_owner_facts_are_asked(self) -> None:
        inventory, knowledge = documents(
            display_name="Без имени", areas=[], integrations=["tuya", "zha"]
        )
        queue = onboarding.refresh_queue(inventory, knowledge, now=100)
        fields = [item["field"] for item in queue["items"][0]["questions"]]
        self.assertEqual(fields, ["human_name", "area", "preferred_integration"])

    def test_proposal_contains_all_owner_policy_fields_but_no_ha_write(self) -> None:
        queue = prepared_queue()
        onboarding_id = queue["items"][0]["onboarding_id"]
        result = onboarding.create_proposal(queue, onboarding_id, {})
        self.assertEqual(set(result["proposal"]), {
            "human_name", "area", "aliases", "criticality",
            "notification_policy", "auto_recovery_policy", "preferred_integration",
        })
        self.assertEqual(queue["actions_performed"], 0)
        self.assertEqual(queue["items"][0]["status"], "proposal_ready")
        self.assertIn("ha_registry_metadata_exact", result["plan_ids"])

    def test_known_facts_can_prepare_proposal_without_invented_owner_answers(self) -> None:
        queue = prepared_queue()
        onboarding_id = queue["items"][0]["onboarding_id"]
        result = onboarding.record_owner_answers(queue, onboarding_id, {})
        self.assertEqual(result["status"], "proposal_ready")
        self.assertEqual(result["proposal"]["human_name"], "Комнатный датчик")
        self.assertEqual(result["proposal"]["area"], "Спальня")
        self.assertEqual(queue["actions_performed"], 0)

    def test_restricted_device_cannot_receive_automatic_r1_policy(self) -> None:
        inventory, knowledge = documents(safety_class="restricted")
        queue = onboarding.refresh_queue(inventory, knowledge, now=100)
        onboarding_id = queue["items"][0]["onboarding_id"]
        with self.assertRaises(onboarding.OnboardingError):
            onboarding.create_proposal(
                queue, onboarding_id, {"auto_recovery_policy": "approved_r1"}
            )

    def test_exact_proposal_hash_and_explicit_owner_confirmation_are_required(self) -> None:
        queue = prepared_queue()
        onboarding_id = queue["items"][0]["onboarding_id"]
        result = onboarding.create_proposal(queue, onboarding_id, {})
        with self.assertRaises(onboarding.OnboardingError):
            onboarding.approve_proposal(
                queue, onboarding_id, result["proposal_hash"],
                explicit_owner_confirmation=False,
            )
        with self.assertRaises(onboarding.OnboardingError):
            onboarding.approve_proposal(
                queue, onboarding_id, "0" * 64,
                explicit_owner_confirmation=True,
            )
        onboarding.approve_proposal(
            queue, onboarding_id, result["proposal_hash"],
            explicit_owner_confirmation=True,
        )
        self.assertEqual(queue["items"][0]["status"], "approved")

    def test_ha_plan_is_staged_then_calls_only_exact_adapter_with_readback(self) -> None:
        queue = prepared_queue()
        onboarding_id = queue["items"][0]["onboarding_id"]
        proposal = onboarding.create_proposal(queue, onboarding_id, {})
        onboarding.approve_proposal(
            queue, onboarding_id, proposal["proposal_hash"],
            explicit_owner_confirmation=True,
        )
        adapter = mock.Mock(return_value={
            "status": "verified", "verified": True, "changed": True,
            "verification": "registry and entities read back",
        })
        staged = onboarding.execute_plan(
            queue, onboarding_id, "ha_registry_metadata_exact",
            explicit_owner_confirmation=True, live_qualified=False,
            adapter_executor=adapter,
        )
        self.assertEqual(staged["status"], "qualification_required")
        adapter.assert_not_called()
        result = onboarding.execute_plan(
            queue, onboarding_id, "ha_registry_metadata_exact",
            explicit_owner_confirmation=True, live_qualified=True,
            adapter_executor=adapter, now=200,
        )
        self.assertEqual(result["status"], "verified")
        self.assertEqual(adapter.call_args.args[0], "ha.registry.metadata_exact")
        self.assertIsNone(adapter.call_args.args[2])
        self.assertEqual(queue["items"][0]["audit"][0]["status"], "verified")

    def test_secure_operator_material_is_not_returned_or_audited(self) -> None:
        queue = prepared_queue(local_profile=True)
        onboarding_id = queue["items"][0]["onboarding_id"]
        proposal = onboarding.create_proposal(
            queue, onboarding_id, {"preferred_integration": "localtuya"}
        )
        # The fixture already links Tuya, while LocalTuya is globally available.
        self.assertIn("local_integration_onboard_exact", proposal["plan_ids"])
        onboarding.approve_proposal(
            queue, onboarding_id, proposal["proposal_hash"],
            explicit_owner_confirmation=True,
        )
        secret = object()
        secure_operator = mock.Mock(return_value=secret)
        adapter = mock.Mock(return_value={
            "status": "delivery_unknown", "verified": False, "changed": False,
            "verification": "delivery unknown",
        })
        result = onboarding.execute_plan(
            queue, onboarding_id, "local_integration_onboard_exact",
            explicit_owner_confirmation=True, live_qualified=True,
            adapter_executor=adapter, secure_operator=secure_operator, now=200,
        )
        self.assertEqual(result["status"], "delivery_unknown")
        self.assertIs(adapter.call_args.args[2], secret)
        self.assertNotIn("secret", str(result).casefold())
        self.assertNotIn(repr(secret), str(queue))
        with self.assertRaises(onboarding.OnboardingError):
            onboarding.execute_plan(
                queue, onboarding_id, "local_integration_onboard_exact",
                explicit_owner_confirmation=True, live_qualified=True,
                adapter_executor=adapter, secure_operator=secure_operator,
            )

    def test_queue_file_is_private_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.chmod(root, 0o700)
            path = root / "queue.json"
            queue = prepared_queue()
            onboarding.write_queue(queue, path)
            self.assertEqual(stat_mode(path), 0o600)
            self.assertEqual(onboarding.read_queue(path), queue)

    def test_schema_one_queue_migrates_without_losing_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.chmod(root, 0o700)
            path = root / "queue.json"
            legacy = prepared_queue()
            legacy["schema_version"] = 1
            legacy["items"][0].pop("owner_answers", None)
            path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
            os.chmod(path, 0o600)
            migrated = onboarding.read_queue(path)
            self.assertEqual(migrated["schema_version"], onboarding.SCHEMA_VERSION)
            self.assertEqual(migrated["items"][0]["owner_answers"], {})
            self.assertEqual(
                migrated["items"][0]["physical_device_hash"], PHYSICAL_HASH
            )

    def test_partial_owner_answers_persist_until_proposal_is_complete(self) -> None:
        inventory, knowledge = documents(
            display_name="Без имени", areas=[], integrations=["tuya", "zha"]
        )
        queue = onboarding.refresh_queue(inventory, knowledge, now=100)
        onboarding_id = queue["items"][0]["onboarding_id"]
        first = onboarding.record_owner_answers(
            queue, onboarding_id, {"area": "Спальня"}
        )
        self.assertEqual(first["status"], "clarification_required")
        self.assertEqual(queue["items"][0]["owner_answers"]["area"], "Спальня")
        self.assertNotIn("area", [item["field"] for item in first["questions"]])
        second = onboarding.record_owner_answers(
            queue, onboarding_id, {"human_name": "Датчик климата"}
        )
        self.assertEqual(second["status"], "clarification_required")
        self.assertEqual(
            [item["field"] for item in second["questions"]],
            ["preferred_integration"],
        )
        final = onboarding.record_owner_answers(
            queue, onboarding_id, {"preferred_integration": "zha"}
        )
        self.assertEqual(final["status"], "proposal_ready")
        self.assertEqual(final["proposal"]["human_name"], "Датчик климата")
        self.assertEqual(final["proposal"]["area"], "Спальня")
        self.assertEqual(queue["actions_performed"], 0)

    def test_invalid_partial_policy_is_not_persisted(self) -> None:
        inventory, knowledge = documents(display_name="Без имени", areas=[])
        queue = onboarding.refresh_queue(inventory, knowledge, now=100)
        onboarding_id = queue["items"][0]["onboarding_id"]
        with self.assertRaises(onboarding.OnboardingError):
            onboarding.record_owner_answers(
                queue,
                onboarding_id,
                {"area": "Спальня", "criticality": "disable_safety"},
            )
        self.assertEqual(queue["items"][0]["owner_answers"], {})


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


if __name__ == "__main__":
    unittest.main()
