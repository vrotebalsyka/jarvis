#!/usr/bin/env python3
"""Offline contracts for learned Home Assistant telemetry freshness."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import entity_freshness as freshness  # noqa: E402
import incident_monitor  # noqa: E402


ENTITY = "sensor.room_temperature"
PHYSICAL_HASH = "a" * 64


def timestamp(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def snapshot(entity_id: str, source_epoch: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "entities": [{
            "entity_id": entity_id,
            "state_kind": "number",
            "state_value": 21.0,
            "observed_at": timestamp(source_epoch),
            "source_last_updated_at": timestamp(source_epoch),
        }],
    }


class EntityFreshnessTests(unittest.TestCase):
    def _store(self, temporary: str) -> incident_monitor.IncidentStore:
        state = Path(temporary) / "state"
        state.mkdir(mode=0o700)
        store = incident_monitor.IncidentStore(
            state / incident_monitor.DATABASE_NAME
        )
        store.replace_entity_device_map([{
            "entity_id": ENTITY,
            "physical_device_hash": PHYSICAL_HASH,
            "device_id": "b" * 32,
            "platform": "tuya_local",
            "config_entry_ids": ["c" * 32],
        }], 1)
        return store

    @staticmethod
    def _run(
        store: incident_monitor.IncidentStore,
        observed_epoch: int,
        source_epoch: int,
        *,
        entity_id: str = ENTITY,
    ) -> dict[str, int]:
        document = snapshot(entity_id, source_epoch)
        return freshness.run_once(
            store,
            observed_epoch=observed_epoch,
            snapshot_reader=lambda _command: (document, 0),
        )

    def test_only_periodic_telemetry_gets_a_conservative_policy(self) -> None:
        self.assertEqual(freshness.minimum_stale_seconds(ENTITY), 7200)
        self.assertEqual(
            freshness.minimum_stale_seconds("sensor.relay_total_energy"),
            172800,
        )
        self.assertIsNone(
            freshness.minimum_stale_seconds("binary_sensor.room_motion")
        )
        self.assertIsNone(freshness.minimum_stale_seconds("sensor.last_event"))

    def test_stale_requires_three_learned_intervals_and_resolves_on_new_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                for epoch in (100, 400, 700, 1000):
                    result = self._run(store, epoch, epoch)
                    self.assertEqual(result["stale"], 0)
                learned = store.connection.execute(
                    "SELECT interval_samples,stale_threshold_seconds "
                    "FROM entity_freshness_observations WHERE entity_id=?",
                    (ENTITY,),
                ).fetchone()
                self.assertEqual(learned["interval_samples"], 3)
                self.assertEqual(learned["stale_threshold_seconds"], 7200)

                first = self._run(store, 8201, 1000)
                self.assertEqual((first["stale"], first["observed"]), (1, 1))
                self._run(store, 8501, 1000)
                incident = store.connection.execute(
                    "SELECT status,cause_code,cause_confidence "
                    "FROM device_incidents"
                ).fetchone()
                self.assertEqual(
                    tuple(incident),
                    ("confirmed", "stale_entity_data", "probable"),
                )

                recovered = self._run(store, 8502, 8502)
                self.assertEqual(recovered["resolved"], 1)
                final = store.connection.execute(
                    "SELECT status,resolved_epoch FROM device_incidents"
                ).fetchone()
                self.assertEqual(tuple(final), ("resolved", 8502))
            finally:
                store.close()

    def test_old_timestamp_without_learned_cadence_never_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                self._run(store, 100, 100)
                result = self._run(store, 100000, 100)
                self.assertEqual(result["learning"], 1)
                self.assertEqual(result["stale"], 0)
                self.assertIsNone(
                    store.connection.execute("SELECT id FROM incidents").fetchone()
                )
            finally:
                store.close()

    def test_future_or_malformed_source_timestamp_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                future = snapshot(ENTITY, 200)
                with self.assertRaises(incident_monitor.MonitorError):
                    freshness.run_once(
                        store,
                        observed_epoch=100,
                        snapshot_reader=lambda _command: (future, 0),
                    )
                malformed = snapshot(ENTITY, 100)
                malformed["entities"][0]["source_last_updated_at"] = "not-a-date"
                with self.assertRaises(freshness.FreshnessError):
                    freshness.run_once(
                        store,
                        observed_epoch=100,
                        snapshot_reader=lambda _command: (malformed, 0),
                    )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
