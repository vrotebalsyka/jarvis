#!/usr/bin/env python3
"""Offline contracts for compact HA system-log diagnostics."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import incident_monitor  # noqa: E402
import system_log_diagnostics as diagnostics  # noqa: E402


class SystemLogDiagnosticsTests(unittest.TestCase):
    def _store(self, temporary: str) -> incident_monitor.IncidentStore:
        state = Path(temporary) / "state"
        state.mkdir(mode=0o700)
        return incident_monitor.IncidentStore(
            state / incident_monitor.DATABASE_NAME
        )

    def test_tuya_signature_failure_is_correlated_without_raw_log_storage(self) -> None:
        raw_marker = "network error: (-9999999) sign invalid PRIVATE DETAILS"
        entry = {
            "timestamp": 102.5,
            "count": 1,
            "level": "ERROR",
            "name": "homeassistant.core",
            "message": "Error executing service switch/turn_on",
            "exception": f"tuya_sharing {raw_marker}",
            "source": "components/switch/__init__.py:1",
        }
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                diagnostics.run_once(store, [], observed_epoch=90)
                store.replace_entity_device_map(
                    [{
                        "entity_id": "switch.relay",
                        "physical_device_hash": "f" * 64,
                        "device_id": "a" * 32,
                        "platform": "tuya",
                        "config_entry_ids": ["b" * 32],
                    }],
                    90,
                )
                store.record_service_call(
                    event_hash="c" * 64,
                    context_hash=hashlib.sha256(b"context").hexdigest(),
                    domain="switch",
                    service="turn_on",
                    entity_ids=["switch.relay"],
                    observed_epoch=100,
                )
                result = diagnostics.run_once(
                    store, [entry], observed_epoch=103
                )
                self.assertEqual(result["incidents"], 1)
                candidate = store.operational_incident_candidates()[0]
                self.assertEqual(candidate["source_type"], "system_log")
                self.assertEqual(candidate["target_entity_id"], "switch.relay")
                self.assertEqual(candidate["action_code"], "switch.turn_on")
                self.assertEqual(
                    candidate["cause_code"], "tuya_integration_unavailable"
                )
                self.assertEqual(
                    candidate["error_code"], "cloud_signature_invalid"
                )
                duplicate = diagnostics.run_once(
                    store, [entry], observed_epoch=104
                )
                self.assertEqual(duplicate["incidents"], 0)
                serialized = json.dumps([
                    tuple(row) for row in store.connection.execute(
                        "SELECT * FROM operational_observations"
                    )
                ])
                self.assertNotIn(raw_marker, serialized)
                self.assertNotIn("PRIVATE DETAILS", serialized)
            finally:
                store.close()

    def test_first_poll_seeds_existing_errors_without_opening_incidents(self) -> None:
        entry = {
            "timestamp": 50.0,
            "count": 7,
            "name": "custom_components.midea_ac_lan",
            "message": "network error",
            "exception": "PRIVATE DEVICE ADDRESS",
            "source": "custom_components/midea_ac_lan/client.py:1",
        }
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                result = diagnostics.run_once(
                    store, [entry], observed_epoch=100
                )
                self.assertEqual(result["recorded"], 1)
                self.assertEqual(result["incidents"], 0)
                self.assertEqual(store.operational_incident_candidates(), [])
                self.assertTrue(
                    store.diagnostic_cursor_exists(diagnostics.CURSOR_NAME)
                )
            finally:
                store.close()

    def test_unknown_log_noise_is_ignored(self) -> None:
        self.assertIsNone(diagnostics.classify_entry({
            "timestamp": 10.0,
            "count": 1,
            "name": "homeassistant.core",
            "message": "ordinary informational text",
            "exception": "",
            "source": "core.py:1",
        }))


if __name__ == "__main__":
    unittest.main()
