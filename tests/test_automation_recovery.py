#!/usr/bin/env python3
"""Offline contracts for bounded, verified automation recovery."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import automation_recovery as recovery  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402
import incident_monitor  # noqa: E402
import recovery_planner  # noqa: E402


TOKEN = "SECRET_SENTINEL.DO_NOT_LEAK.abcdefghijklmnopqrstuvwxyz"
ENTRY_ID = "01KJF759CXHGZ0YJWQGT60M1R5"
TARGET = "light.rele_2_garderob"
MOTION = "binary_sensor.24g_presence_sensor_v3_dvizhenie"
HELPER = "input_boolean.garderob_rele_2_vkliucheno_datchikom"
AUTOMATION = "automation.garderob_rele_po_datchiku_dvizheniia"


class AutomationRecoveryTests(unittest.TestCase):
    def _store(self, temporary: str, *, action: str = "light.turn_on") -> incident_monitor.IncidentStore:
        state_dir = Path(temporary) / "state"
        state_dir.mkdir(mode=0o700)
        store = incident_monitor.IncidentStore(
            state_dir / incident_monitor.DATABASE_NAME
        )
        store.record_automation_run(
            run_hash="a" * 64,
            automation_entity_id=AUTOMATION,
            automation_item_hash="b" * 64,
            outcome="failed",
            started_epoch=100,
            observed_epoch=101,
            error_code="network_unreachable",
            cause_code="yandex_cloud_unreachable",
            cause_confidence="confirmed",
            action_code=action,
            target_entity_id=TARGET,
            display_name="Гардероб",
        )
        return store

    @staticmethod
    def _config() -> ha_read.AdapterConfig:
        return ha_read.AdapterConfig(
            "http", "192.168.1.127", 8123, TOKEN, (), True
        )

    @staticmethod
    def _inventory() -> dict[str, object]:
        return {
            "schema_version": 1,
            "entities": [{
                "entity_id": TARGET,
                "platform": "yandex_station",
                "config_entry_ids": [ENTRY_ID],
            }],
            "config_entries": [{
                "entry_id": ENTRY_ID,
                "domain": "yandex_station",
                "state": "loaded",
            }],
        }

    @staticmethod
    def _reader(states: dict[str, str]):
        return lambda _config, _path: [
            {"entity_id": entity_id, "state": state, "attributes": {}}
            for entity_id, state in states.items()
        ]

    @staticmethod
    def _plan(candidate_id: str):
        def plan(store, incident, runtime, *, now):
            facts = recovery_planner.build_facts(incident, runtime)
            candidates = recovery_planner.build_candidates(facts)
            candidate = next(
                item for item in candidates if item["id"] == candidate_id
            )
            return recovery_planner.plan_one(
                store, incident, runtime, now=now,
                chooser=lambda _facts, _candidates: {
                    "candidate_id": candidate_id,
                    "fact_ids": candidate["required_fact_ids"],
                    "source": "model",
                },
            )
        return plan

    def test_stale_failed_turn_on_repairs_helper_without_touching_relay(self) -> None:
        states = {TARGET: "off", MOTION: "off", HELPER: "on"}
        calls: list[tuple[str, str]] = []
        sleeps: list[float] = []
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                result = recovery.run_once(
                    store, self._inventory(), now=200, live=True,
                    config_loader=self._config,
                    raw_state_reader=self._reader(states),
                    cloud_probe=lambda: True,
                    plan_fn=self._plan("repair_helper_state"),
                    action_caller=lambda *_args: calls.append(("relay", "changed")),
                    helper_caller=lambda _config, entity_id: (
                        calls.append(("helper", entity_id)),
                        states.__setitem__(entity_id, "off"),
                    ),
                    sleeper=sleeps.append,
                )
                self.assertEqual(result["outcome"], "verified")
                self.assertEqual(result["verification_checks"], 1)
                self.assertEqual(calls, [("helper", HELPER)])
                self.assertEqual(sleeps, [20])
                incident = store.connection.execute(
                    "SELECT status FROM operational_incidents"
                ).fetchone()
                self.assertEqual(incident["status"], "resolved")
            finally:
                store.close()

    def test_current_intent_retries_exact_action_once_and_verifies_at_20s(self) -> None:
        states = {TARGET: "off", MOTION: "on", HELPER: "on"}
        calls: list[tuple[str, str]] = []
        sleeps: list[float] = []
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                result = recovery.run_once(
                    store, self._inventory(), now=200, live=True,
                    config_loader=self._config,
                    raw_state_reader=self._reader(states),
                    cloud_probe=lambda: True,
                    plan_fn=self._plan("retry_original_intent_once"),
                    action_caller=lambda _config, entity_id, action: (
                        calls.append((entity_id, action)),
                        states.__setitem__(entity_id, "on"),
                    ),
                    sleeper=sleeps.append,
                )
                self.assertEqual(result["outcome"], "verified")
                self.assertEqual(calls, [(TARGET, "turn_on")])
                self.assertEqual(sleeps, [20])
                self.assertEqual(store.operational_incident_candidates(), [])
            finally:
                store.close()

    def test_cloud_wait_uses_30_then_120_second_backoff(self) -> None:
        states = {TARGET: "off", MOTION: "on", HELPER: "on"}
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                first = recovery.run_once(
                    store, self._inventory(), now=200, live=True,
                    config_loader=self._config,
                    raw_state_reader=self._reader(states),
                    cloud_probe=lambda: False,
                    plan_fn=self._plan("wait_yandex_backoff"),
                )
                self.assertEqual(first["service_calls"], 0)
                self.assertEqual(first["next_allowed_epoch"], 230)
                cooldown = recovery.run_once(
                    store, self._inventory(), now=220, live=True,
                    config_loader=self._config,
                    raw_state_reader=self._reader(states),
                    cloud_probe=lambda: False,
                    plan_fn=self._plan("wait_yandex_backoff"),
                )
                self.assertEqual(cooldown["outcome"], "cooldown")
                second = recovery.run_once(
                    store, self._inventory(), now=230, live=True,
                    config_loader=self._config,
                    raw_state_reader=self._reader(states),
                    cloud_probe=lambda: False,
                    plan_fn=self._plan("wait_yandex_backoff"),
                )
                self.assertEqual(second["next_allowed_epoch"], 350)
            finally:
                store.close()

    def test_delivery_unknown_is_never_retried_for_same_incident(self) -> None:
        states = {TARGET: "off", MOTION: "on", HELPER: "on"}
        service_calls = 0

        def uncertain(*_args):
            nonlocal service_calls
            service_calls += 1
            raise OSError("private transport detail")

        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                first = recovery.run_once(
                    store, self._inventory(), now=200, live=True,
                    config_loader=self._config,
                    raw_state_reader=self._reader(states),
                    cloud_probe=lambda: True,
                    plan_fn=self._plan("retry_original_intent_once"),
                    action_caller=uncertain,
                )
                self.assertEqual(first["outcome"], "delivery_unknown")
                second = recovery.run_once(
                    store, self._inventory(),
                    now=first["next_allowed_epoch"], live=True,
                    config_loader=self._config,
                    raw_state_reader=self._reader(states),
                    cloud_probe=lambda: True,
                    plan_fn=self._plan("observe_and_notify"),
                    action_caller=uncertain,
                )
                self.assertEqual(second["outcome"], "no_action")
                self.assertEqual(second["service_calls"], 0)
                self.assertEqual(service_calls, 1)
            finally:
                store.close()

    def test_unconfirmed_command_is_escalated_and_never_offered_again(self) -> None:
        states = {TARGET: "off", MOTION: "on", HELPER: "on"}
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                result = recovery.run_once(
                    store, self._inventory(), now=200, live=True,
                    config_loader=self._config,
                    raw_state_reader=self._reader(states),
                    cloud_probe=lambda: True,
                    plan_fn=self._plan("retry_original_intent_once"),
                    action_caller=lambda *_args: None,
                    sleeper=lambda _seconds: None,
                )
                self.assertEqual(result["outcome"], "failed")
                incident = store.operational_incident_candidates()[0]
                self.assertEqual(incident["status"], "escalated")
                self.assertEqual(incident["cause_code"], "command_not_confirmed")
                follow_up = recovery.run_once(
                    store, self._inventory(), now=result["next_allowed_epoch"],
                    live=False,
                    config_loader=self._config,
                    raw_state_reader=self._reader(states),
                    cloud_probe=lambda: True,
                )
                self.assertNotIn(
                    "retry_original_intent_once", follow_up["candidate_ids"]
                )
                self.assertEqual(follow_up["service_calls"], 0)
            finally:
                store.close()

    def test_dry_run_exposes_candidates_but_never_calls_model_or_service(self) -> None:
        states = {TARGET: "off", MOTION: "on", HELPER: "on"}
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                result = recovery.run_once(
                    store, self._inventory(), now=200, live=False,
                    config_loader=self._config,
                    raw_state_reader=self._reader(states),
                    cloud_probe=lambda: True,
                    plan_fn=lambda *_args, **_kwargs: self.fail("model called"),
                    action_caller=lambda *_args: self.fail("service called"),
                )
                self.assertIn("retry_original_intent_once", result["candidate_ids"])
                self.assertEqual(result["service_calls"], 0)
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM recovery_decisions"
                    ).fetchone()[0],
                    0,
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
