#!/usr/bin/env python3
"""Offline contracts for universal Home Assistant device health."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import device_health  # noqa: E402
import incident_monitor  # noqa: E402


PHYSICAL_HASH = "a" * 64


def profile(domain: str, loaded: int = 1) -> dict[str, object]:
    return {
        "domain": domain,
        "entry_count": 1,
        "loaded_entry_count": loaded,
        "recovery_mode": "diagnose_only",
    }


def physical_device(
    *,
    platform: str = "midea_ac_lan",
    network_status: str = "stable",
    network_miss_count: int = 0,
) -> dict[str, object]:
    return {
        "physical_device_hash": PHYSICAL_HASH,
        "display_name": "Посудомоечная машина",
        "entity_ids": [
            "sensor.dishwasher_state",
            "sensor.dishwasher_progress",
            "switch.dishwasher_power",
        ],
        "config_domains": [platform],
        "platforms": [platform],
        "safety_class": "restricted",
        "network_status": network_status,
        "network_miss_count": network_miss_count,
    }


def snapshot(kinds: list[str]) -> dict[str, object]:
    entity_ids = physical_device()["entity_ids"]
    return {
        "entities": [
            {"entity_id": entity_id, "state_kind": kind}
            for entity_id, kind in zip(entity_ids, kinds)
        ]
    }


class DeviceHealthTests(unittest.TestCase):
    def _store(self, temporary: str) -> incident_monitor.IncidentStore:
        state = Path(temporary) / "state"
        state.mkdir(mode=0o700)
        return incident_monitor.IncidentStore(
            state / incident_monitor.DATABASE_NAME
        )

    def test_partial_appliance_is_not_reported_as_offline(self) -> None:
        assessment = device_health.assess_device(
            physical_device(),
            device_health._snapshot_index(
                snapshot(["enum", "number", "unavailable"])
            ),
            {"midea_ac_lan": profile("midea_ac_lan")},
        )
        self.assertEqual(assessment["health_status"], "partial")
        self.assertEqual(assessment["cause_code"], "partial_entity_unavailable")
        self.assertEqual(assessment["available_entity_count"], 2)

    def test_lan_absence_plus_all_unavailable_is_confirmed_offline(self) -> None:
        assessment = device_health.assess_device(
            physical_device(
                network_status="not_observed", network_miss_count=3
            ),
            device_health._snapshot_index(
                snapshot(["unavailable", "unavailable", "unavailable"])
            ),
            {"midea_ac_lan": profile("midea_ac_lan")},
        )
        self.assertEqual(assessment["health_status"], "offline")
        self.assertEqual(assessment["cause_code"], "device_not_observed_on_lan")
        self.assertEqual(assessment["cause_confidence"], "confirmed")

    def test_tuya_entities_unavailable_with_lan_presence_blames_integration(self) -> None:
        device = physical_device(platform="tuya")
        assessment = device_health.assess_device(
            device,
            device_health._snapshot_index(
                snapshot(["unavailable", "unavailable", "unavailable"])
            ),
            {"tuya": profile("tuya")},
        )
        self.assertEqual(assessment["health_status"], "degraded")
        self.assertEqual(assessment["cause_code"], "tuya_integration_unavailable")

    def test_integration_transition_opens_and_then_resolves_incident(self) -> None:
        inventory = {
            "physical_devices": [physical_device()],
            "integration_profiles": [profile("midea_ac_lan")],
        }
        healthy_snapshot = snapshot(["enum", "number", "enum"])
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                first = device_health.run_once(
                    store,
                    inventory,
                    observed_epoch=100,
                    snapshot_reader=lambda _action: (healthy_snapshot, 0),
                )
                self.assertEqual(first["integration_incidents"], 0)

                broken = copy.deepcopy(inventory)
                broken["integration_profiles"][0]["loaded_entry_count"] = 0
                second = device_health.run_once(
                    store,
                    broken,
                    observed_epoch=200,
                    snapshot_reader=lambda _action: (healthy_snapshot, 0),
                )
                self.assertEqual(second["integration_incidents"], 0)
                confirmed = device_health.run_once(
                    store,
                    broken,
                    observed_epoch=215,
                    snapshot_reader=lambda _action: (healthy_snapshot, 0),
                )
                self.assertEqual(confirmed["integration_incidents"], 1)
                candidate = store.operational_incident_candidates()[0]
                self.assertEqual(candidate["source_type"], "integration")
                self.assertEqual(candidate["cause_code"], "integration_not_loaded")

                third = device_health.run_once(
                    store,
                    inventory,
                    observed_epoch=300,
                    snapshot_reader=lambda _action: (healthy_snapshot, 0),
                )
                self.assertEqual(third["integration_incidents"], 0)
                self.assertEqual(store.operational_incident_candidates(), [])

                evidence = " ".join(
                    str(row[0])
                    for row in store.connection.execute(
                        "SELECT evidence_json FROM integration_health_events"
                    )
                )
                self.assertNotIn("SECRET", evidence)
                for row in store.connection.execute(
                    "SELECT evidence_json FROM integration_health_events"
                ):
                    json.loads(str(row[0]))
            finally:
                store.close()

    def test_live_integration_counts_override_stale_inventory(self) -> None:
        profiles = {"tuya": profile("tuya", loaded=1)}
        refreshed = device_health._apply_live_integration_counts(
            profiles,
            {"tuya": (1, 0), "yandex_station": (2, 2)},
        )
        self.assertEqual(refreshed["tuya"]["loaded_entry_count"], 0)
        self.assertEqual(refreshed["yandex_station"]["entry_count"], 2)
        self.assertEqual(
            device_health._integration_display_name("homeassistant"),
            "Home Assistant",
        )

    def test_network_scanner_miss_does_not_alert_while_ha_still_answers(self) -> None:
        healthy_snapshot = snapshot(["enum", "number", "enum"])
        healthy_inventory = {
            "physical_devices": [physical_device(network_status="stable")],
            "integration_profiles": [profile("midea_ac_lan")],
        }
        missing_inventory = copy.deepcopy(healthy_inventory)
        missing_inventory["physical_devices"][0]["network_status"] = "not_observed"
        missing_inventory["physical_devices"][0]["network_miss_count"] = 3
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                store.connection.execute(
                    "UPDATE notification_policies SET enabled_epoch=0 WHERE name=?",
                    (incident_monitor.DEVICE_NOTIFICATION_POLICY,),
                )
                device_health.run_once(
                    store, healthy_inventory, observed_epoch=100,
                    snapshot_reader=lambda _action: (healthy_snapshot, 0),
                )
                device_health.run_once(
                    store, missing_inventory, observed_epoch=200,
                    snapshot_reader=lambda _action: (healthy_snapshot, 0),
                )
                self.assertEqual(store.device_notification_candidates(220), [])
                device_health.run_once(
                    store, missing_inventory, observed_epoch=260,
                    snapshot_reader=lambda _action: (healthy_snapshot, 0),
                )
                self.assertEqual(store.device_notification_candidates(260), [])
                device_health.run_once(
                    store, healthy_inventory, observed_epoch=300,
                    snapshot_reader=lambda _action: (healthy_snapshot, 0),
                )
                self.assertIsNone(store.connection.execute(
                    "SELECT status FROM device_incidents"
                ).fetchone())
            finally:
                store.close()

    def test_network_loss_alert_requires_repeated_misses_and_ha_unavailable(self) -> None:
        healthy_inventory = {
            "physical_devices": [physical_device()],
            "integration_profiles": [profile("midea_ac_lan")],
        }
        missing_inventory = copy.deepcopy(healthy_inventory)
        missing_inventory["physical_devices"][0].update({
            "network_status": "not_observed",
            "network_miss_count": 3,
        })
        offline_snapshot = snapshot(["unavailable"] * 3)
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                store.connection.execute(
                    "UPDATE notification_policies SET enabled_epoch=0 WHERE name=?",
                    (incident_monitor.DEVICE_NOTIFICATION_POLICY,),
                )
                device_health.run_once(
                    store, healthy_inventory, observed_epoch=100,
                    snapshot_reader=lambda _action: (snapshot(["enum"] * 3), 0),
                )
                device_health.run_once(
                    store, missing_inventory, observed_epoch=200,
                    snapshot_reader=lambda _action: (offline_snapshot, 0),
                )
                self.assertEqual(store.device_notification_candidates(220), [])
                device_health.run_once(
                    store, missing_inventory, observed_epoch=230,
                    snapshot_reader=lambda _action: (offline_snapshot, 0),
                )
                candidates = store.device_notification_candidates(230)
                self.assertEqual(len(candidates), 1)
                self.assertEqual(
                    candidates[0]["cause_code"], "device_not_observed_on_lan"
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
