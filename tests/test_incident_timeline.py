#!/usr/bin/env python3
"""Offline contracts for the owner-facing 24-hour incident timeline."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import incident_monitor  # noqa: E402
import incident_timeline  # noqa: E402


class IncidentTimelineTests(unittest.TestCase):
    def _store(self, temporary: str) -> incident_monitor.IncidentStore:
        state = Path(temporary) / "state"
        state.mkdir(mode=0o700)
        return incident_monitor.IncidentStore(
            state / incident_monitor.DATABASE_NAME
        )

    def test_timeline_combines_physical_outage_and_agent_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                store.replace_entity_device_map([{
                    "entity_id": "binary_sensor.motion",
                    "physical_device_hash": "a" * 64,
                    "device_id": "b" * 32,
                    "platform": "tuya_local",
                    "config_entry_ids": [],
                }], 100)
                store.observe(
                    "binary_sensor.motion", "entity", "unavailable", 100,
                    unavailable=True, source="test",
                )
                store.confirm_due(160, 60)
                store.observe(
                    "binary_sensor.motion", "entity", "available", 220,
                    unavailable=False, source="test",
                )
                store.reconcile_device_incidents(220)
                result = store.record_automation_run(
                    run_hash="c" * 64,
                    automation_entity_id="automation.garderob",
                    automation_item_hash="d" * 64,
                    outcome="failed",
                    started_epoch=300,
                    observed_epoch=301,
                    error_code="network_unreachable",
                    cause_code="yandex_cloud_unreachable",
                    cause_confidence="confirmed",
                    action_code="light.turn_on",
                    target_entity_id="light.garderob",
                    display_name="Гардероб",
                )
                incident_id = int(result["incident_id"])
                store.resolve_operational_incident(
                    incident_id, 360, "target_state_confirmed"
                )
                # A verified action is what distinguishes agent recovery.
                store.record_recovery_decision(
                    decision_id="e" * 64,
                    operational_incident_id=incident_id,
                    selected_candidate_id="retry_original_intent_once",
                    decision_source="model",
                    fact_ids=["incident:open"],
                    decided_epoch=320,
                )
                store.record_operational_attempt(
                    operational_incident_id=incident_id,
                    decision_id="e" * 64,
                    candidate_id="retry_original_intent_once",
                    attempted_epoch=320,
                    status="verified",
                    service_calls=1,
                    verification_checks=1,
                    before_state="off",
                    after_state="on",
                    next_allowed_epoch=320,
                    evidence_code="target_state_confirmed",
                )
                store.observe(
                    incident_monitor.RESERVED_SUBJECT,
                    "system",
                    "unreachable",
                    370,
                    unavailable=True,
                    source="test",
                )
                store.confirm_due(380, 10)
                store.observe(
                    incident_monitor.RESERVED_SUBJECT,
                    "system",
                    "reachable",
                    390,
                    unavailable=False,
                    source="test",
                )
                timeline = incident_timeline.collect(
                    store.connection, now=400, window_seconds=300
                )
                summary = timeline["summary"]
                self.assertEqual(summary["total_incidents"], 3)
                self.assertEqual(summary["device_outages"], 1)
                self.assertEqual(summary["automation_failures"], 1)
                self.assertEqual(summary["home_assistant_outages"], 1)
                self.assertEqual(summary["agent_recovered"], 1)
                self.assertEqual(summary["self_recovered"], 2)
                self.assertEqual(summary["recovery_attempts"], 1)
                self.assertEqual(summary["verification_checks"], 1)
                automation = next(
                    item for item in timeline["incidents"]
                    if item["kind"] == "automation_failure"
                )
                self.assertEqual(automation["duration_seconds"], 60)
                self.assertEqual(automation["cause_code"], "yandex_cloud_unreachable")
                self.assertEqual(
                    automation["recovery_action_code"],
                    "retry_original_intent_once",
                )
                self.assertEqual(automation["recovery_attempts"], 1)
                self.assertEqual(automation["verification_checks"], 1)
                core = next(
                    item for item in timeline["incidents"]
                    if item["kind"] == "home_assistant_outage"
                )
                self.assertEqual(core["duration_seconds"], 20)
                self.assertEqual(core["cause_code"], "home_assistant_unreachable")
            finally:
                store.close()

    def test_timeline_merges_old_entity_rows_after_physical_identity_is_learned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                entities = (
                    "binary_sensor.presence_motion",
                    "sensor.presence_battery",
                )
                for entity_id in entities:
                    store.observe(
                        entity_id, "entity", "unavailable", 100,
                        unavailable=True, source="test",
                    )
                store.confirm_due(160, 60)
                for entity_id in entities:
                    store.observe(
                        entity_id, "entity", "available", 220,
                        unavailable=False, source="test",
                    )
                store.reconcile_device_incidents(220)
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM device_incidents"
                    ).fetchone()[0],
                    2,
                )
                store.replace_entity_device_map([
                    {
                        "entity_id": entity_id,
                        "physical_device_hash": "a" * 64,
                        "device_id": "b" * 32,
                        "platform": "tuya_local",
                        "config_entry_ids": [],
                    }
                    for entity_id in entities
                ], 230)
                timeline = incident_timeline.collect(
                    store.connection, now=300, window_seconds=300
                )
                device_events = [
                    item for item in timeline["incidents"]
                    if item["kind"] == "device_outage"
                ]
                self.assertEqual(len(device_events), 1)
                self.assertEqual(device_events[0]["duration_seconds"], 120)
            finally:
                store.close()

    def test_integration_failure_is_not_mislabeled_as_automation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                result = store.record_operational_failure(
                    event_hash="f" * 64,
                    source_type="integration",
                    source_ref="midea_ac_lan",
                    observed_epoch=100,
                    error_code="integration_not_loaded",
                    cause_code="integration_not_loaded",
                    cause_confidence="confirmed",
                    action_code="integration.health",
                    target_entity_id=None,
                    display_name="midea ac lan",
                    evidence_code="config_entry_state",
                )
                store.resolve_operational_incident(
                    int(result["incident_id"]), 160, "integration_healthy"
                )
                timeline = incident_timeline.collect(
                    store.connection, now=200, window_seconds=120
                )
                self.assertEqual(timeline["summary"]["integration_failures"], 1)
                self.assertEqual(timeline["summary"]["automation_failures"], 0)
                self.assertEqual(
                    timeline["incidents"][0]["kind"], "integration_failure"
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
