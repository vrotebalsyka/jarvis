#!/usr/bin/env python3
"""Offline contracts for profile-bound integration recovery."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import home_assistant_read as ha_read  # noqa: E402
import incident_monitor  # noqa: E402
import integration_recovery as recovery  # noqa: E402
import recovery_planner  # noqa: E402


TOKEN = "SECRET_SENTINEL.DO_NOT_LEAK.abcdefghijklmnopqrstuvwxyz"
ENTRY_ID = "01KJF759CXHGZ0YJWQGT60M1R5"


class IntegrationRecoveryTests(unittest.TestCase):
    def _store(
        self, temporary: str, *, domain: str = "midea_ac_lan"
    ) -> incident_monitor.IncidentStore:
        state = Path(temporary) / "state"
        state.mkdir(mode=0o700)
        store = incident_monitor.IncidentStore(
            state / incident_monitor.DATABASE_NAME
        )
        store.record_operational_failure(
            event_hash="a" * 64,
            source_type="integration",
            source_ref=domain,
            observed_epoch=100,
            error_code="integration_not_loaded",
            cause_code="integration_not_loaded",
            cause_confidence="confirmed",
            action_code="integration.health",
            target_entity_id=None,
            display_name=domain,
            evidence_code="config_entry_state",
        )
        return store

    @staticmethod
    def _config() -> ha_read.AdapterConfig:
        return ha_read.AdapterConfig(
            "http", "192.168.1.127", 8123, TOKEN, (), True
        )

    @staticmethod
    def _inventory(
        *,
        domain: str = "midea_ac_lan",
        mode: str = "idle_entry_reload",
        automatic: bool = True,
    ) -> dict[str, object]:
        return {
            "integration_profiles": [{
                "domain": domain,
                "entry_count": 1,
                "loaded_entry_count": 0,
                "unloadable_entry_count": 1,
                "recovery_mode": mode,
                "automatic_recovery_allowed": automatic,
            }],
            "config_entries": [{
                "entry_id": ENTRY_ID,
                "domain": domain,
                "state": "setup_retry",
                "supports_unload": True,
            }],
            "physical_devices": [{
                "physical_device_hash": "b" * 64,
                "display_name": "Посудомоечная машина",
                "entity_ids": [
                    "sensor.dishwasher_status",
                    "switch.dishwasher_power",
                ],
                "config_entry_ids": [ENTRY_ID],
                "config_domains": [domain],
                "platforms": [domain],
                "safety_class": "restricted",
                "network_status": "stable",
            }],
        }

    @staticmethod
    def _plan(candidate_id: str):
        def plan(store, incident, runtime, *, now):
            facts = recovery_planner.build_facts(incident, runtime)
            candidates = recovery_planner.build_candidates(facts)
            candidate = next(
                item for item in candidates if item["id"] == candidate_id
            )
            return recovery_planner.plan_one(
                store,
                incident,
                runtime,
                now=now,
                chooser=lambda _facts, _candidates: {
                    "candidate_id": candidate_id,
                    "fact_ids": candidate["required_fact_ids"],
                    "source": "model",
                },
            )
        return plan

    def test_idle_appliance_reload_is_verified_at_twenty_seconds(self) -> None:
        states = {ENTRY_ID: "setup_retry"}
        calls: list[str] = []
        sleeps: list[float] = []
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                result = recovery.run_once(
                    store,
                    self._inventory(),
                    now=200,
                    live=True,
                    config_loader=self._config,
                    state_reader=lambda _config: dict(states),
                    raw_state_reader=lambda _config, _path: [
                        {"entity_id": "sensor.dishwasher_status", "state": "idle"},
                        {"entity_id": "switch.dishwasher_power", "state": "off"},
                    ],
                    plan_fn=self._plan("reload_integration_entry_once"),
                    entry_reload_caller=lambda _config, entry_id: (
                        calls.append(entry_id),
                        states.__setitem__(entry_id, "loaded"),
                    ),
                    sleeper=sleeps.append,
                )
                self.assertEqual(result["outcome"], "verified")
                self.assertEqual(result["service_calls"], 1)
                self.assertEqual(result["verification_checks"], 1)
                self.assertEqual(calls, [ENTRY_ID])
                self.assertEqual(sleeps, [20])
                self.assertEqual(store.operational_incident_candidates(), [])
            finally:
                store.close()

    def test_active_appliance_cannot_be_reloaded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                result = recovery.run_once(
                    store,
                    self._inventory(),
                    now=200,
                    live=False,
                    config_loader=self._config,
                    state_reader=lambda _config: {ENTRY_ID: "setup_retry"},
                    raw_state_reader=lambda _config, _path: [
                        {"entity_id": "sensor.dishwasher_status", "state": "washing"},
                        {"entity_id": "switch.dishwasher_power", "state": "on"},
                    ],
                )
                self.assertEqual(result["candidate_ids"], ["observe_and_notify"])
                self.assertEqual(result["runtime_facts"]["device_activity"], "active")
                self.assertEqual(result["service_calls"], 0)
            finally:
                store.close()

    def test_unknown_integration_is_diagnose_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary, domain="unknown_vendor")
            try:
                result = recovery.run_once(
                    store,
                    self._inventory(
                        domain="unknown_vendor",
                        mode="diagnose_only",
                        automatic=False,
                    ),
                    now=200,
                    live=False,
                    config_loader=self._config,
                    state_reader=lambda _config: {ENTRY_ID: "setup_retry"},
                )
                self.assertEqual(result["candidate_ids"], ["observe_and_notify"])
                self.assertEqual(result["service_calls"], 0)
            finally:
                store.close()

    def test_tuya_cloud_failure_is_never_treated_as_entry_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary, domain="tuya")
            try:
                result = recovery.run_once(
                    store,
                    self._inventory(
                        domain="tuya", mode="cloud_backoff", automatic=False
                    ),
                    now=200,
                    live=False,
                    config_loader=self._config,
                    state_reader=lambda _config: {ENTRY_ID: "setup_retry"},
                )
                self.assertEqual(result["candidate_ids"], ["observe_and_notify"])
                self.assertEqual(result["service_calls"], 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
