#!/usr/bin/env python3
"""Offline contracts for the sanitized owner incident summary."""

from __future__ import annotations

import os
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import incident_monitor as monitor  # noqa: E402
import incident_status  # noqa: E402


class IncidentStatusTests(unittest.TestCase):
    def test_current_inventory_schema_two_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "state"
            directory.mkdir(mode=0o700)
            store = monitor.IncidentStore(directory / monitor.DATABASE_NAME)
            store.close()
            inventory = directory / "inventory.json"
            inventory.write_text(json.dumps({
                "schema_version": 2,
                "entities": [],
                "config_entries": [],
                "integration_capabilities": {},
                "identity_bindings": [],
            }), encoding="ascii")
            inventory.chmod(0o600)
            summary = incident_status.read_summary(
                directory / monitor.DATABASE_NAME,
                expected_uid=os.geteuid(),
            )
            self.assertEqual(summary["actionable_platforms"], [])

    def test_summary_exposes_only_fixed_incident_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "state"
            directory.mkdir(mode=0o700)
            store = monitor.IncidentStore(directory / monitor.DATABASE_NAME)
            try:
                store.observe(
                    "switch.relay", "entity", "unavailable", 100,
                    unavailable=True, source="websocket",
                )
                store.confirm_due(160, 60)
            finally:
                store.close()
            inventory = directory / "inventory.json"
            inventory.write_text(json.dumps({
                "schema_version": 1,
                "entities": [{
                    "entity_id": "switch.relay",
                    "platform": "tuya_local",
                    "device_id": "a" * 32,
                }],
            }), encoding="ascii")
            inventory.chmod(0o600)
            summary = incident_status.read_summary(
                directory / monitor.DATABASE_NAME,
                expected_uid=os.geteuid(),
            )
            self.assertEqual(summary["open_count"], 1)
            self.assertEqual(summary["confirmed_count"], 1)
            self.assertEqual(summary["actionable_count"], 1)
            self.assertEqual(summary["incidents"][0]["subject"], "switch.relay")
            self.assertEqual(summary["completed_actions"]["active_ip_changes"], 0)
            self.assertEqual(summary["completed_actions"]["out_of_band_recovery"], 0)
            self.assertEqual(summary["actionable_platforms"], [{
                "platform": "tuya_local",
                "entity_count": 1,
                "device_count": 1,
                "unmapped_entity_count": 0,
            }])
            serialized = str(summary)
            self.assertNotIn("attributes", serialized)
            self.assertNotIn("token", serialized.casefold())
            self.assertNotIn("mac", serialized.casefold())

    def test_missing_inventory_keeps_incident_summary_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "state"
            directory.mkdir(mode=0o700)
            store = monitor.IncidentStore(directory / monitor.DATABASE_NAME)
            try:
                store.observe(
                    "sensor.test", "entity", "unavailable", 100,
                    unavailable=True, source="websocket",
                )
                store.confirm_due(160, 60)
            finally:
                store.close()
            summary = incident_status.read_summary(
                directory / monitor.DATABASE_NAME,
                expected_uid=os.geteuid(),
            )
            self.assertEqual(summary["actionable_platforms"], [])

    def test_stale_entity_incident_keeps_summary_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "state"
            directory.mkdir(mode=0o700)
            store = monitor.IncidentStore(directory / monitor.DATABASE_NAME)
            try:
                store.observe(
                    "sensor.voltage", "entity", "stale", 100,
                    unavailable=True, source="freshness",
                )
                store.confirm_due(160, 60)
            finally:
                store.close()
            summary = incident_status.read_summary(
                directory / monitor.DATABASE_NAME,
                expected_uid=os.geteuid(),
            )
            self.assertEqual(summary["incidents"][0]["last_state"], "stale")

    def test_device_summary_exposes_only_its_sanitized_member_entities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "state"
            directory.mkdir(mode=0o700)
            store = monitor.IncidentStore(directory / monitor.DATABASE_NAME)
            try:
                with store.connection:
                    store.connection.execute(
                        "UPDATE notification_policies SET enabled_epoch=? WHERE name=?",
                        (90, monitor.DEVICE_NOTIFICATION_POLICY),
                    )
                store.replace_entity_device_map([{
                    "entity_id": "switch.humidifier",
                    "physical_device_hash": "b" * 64,
                    "device_id": "c" * 32,
                    "platform": "tuya",
                    "config_entry_ids": ["D" * 26],
                }], 90)
                store.observe(
                    "switch.humidifier", "entity", "unavailable", 100,
                    unavailable=True, source="test",
                )
                store.confirm_due(120, 20)
                store.reconcile_device_incidents(120)
            finally:
                store.close()
            summary = incident_status.read_summary(
                directory / monitor.DATABASE_NAME,
                expected_uid=os.geteuid(),
            )
            self.assertEqual(
                summary["device_incidents"][0]["member_subjects"],
                ["switch.humidifier"],
            )

    def test_xiaomi_group_exposes_only_one_permission_gated_recovery_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "state"
            directory.mkdir(mode=0o700)
            store = monitor.IncidentStore(directory / monitor.DATABASE_NAME)
            try:
                store.observe(
                    "sensor.xiaomi_problem",
                    "entity",
                    "unavailable",
                    100,
                    unavailable=True,
                    source="websocket",
                )
                store.confirm_due(160, 60)
            finally:
                store.close()
            inventory = directory / "inventory.json"
            inventory.write_text(json.dumps({
                "schema_version": 1,
                "config_entries": [{
                    "entry_id": "A" * 26,
                    "domain": "xiaomi_miot",
                }],
                "integration_capabilities": {
                    "xiaomi_miot": {
                        "bounded_config_entry_reload": True,
                        "automatic_recovery_enabled": False,
                    },
                },
                "entities": [{
                    "entity_id": "sensor.xiaomi_problem",
                    "platform": "xiaomi_miot",
                    "device_id": "a" * 32,
                    "config_entry_ids": ["A" * 26],
                }],
            }), encoding="ascii")
            inventory.chmod(0o600)
            summary = incident_status.read_summary(
                directory / monitor.DATABASE_NAME,
                expected_uid=os.geteuid(),
            )
            self.assertEqual(summary["actionable_platforms"], [{
                "platform": "xiaomi_miot",
                "entity_count": 1,
                "device_count": 1,
                "unmapped_entity_count": 0,
                "recovery_status": "permission_required",
                "recovery_config_entry_count": 1,
                "lan_observed_device_count": 0,
            }])

    def test_unsafe_permissions_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "state"
            directory.mkdir(mode=0o700)
            store = monitor.IncidentStore(directory / monitor.DATABASE_NAME)
            store.close()
            path = directory / monitor.DATABASE_NAME
            path.chmod(0o644)
            with self.assertRaises(incident_status.IncidentStatusError):
                incident_status.read_summary(path, expected_uid=os.geteuid())


if __name__ == "__main__":
    unittest.main()
