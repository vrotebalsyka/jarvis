#!/usr/bin/env python3
"""Offline contracts for sanitized Home Assistant automation diagnostics."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import automation_diagnostics as diagnostics  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402
import incident_monitor  # noqa: E402


TOKEN = "SECRET_SENTINEL.DO_NOT_LEAK.abcdefghijklmnopqrstuvwxyz"
ITEM_ID = "1780401983084"
RUN_ID = "c50cd77d9dd84a4fac12a70e1a302911"
DEVICE_ID = "c91bf12b42f25fac48fee74e5596a5b8"
PHYSICAL_HASH = "a" * 64


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.replies = [
            {"type": "auth_required"},
            {"type": "auth_ok"},
            {
                "id": 20,
                "type": "result",
                "success": True,
                "result": [
                    {
                        "run_id": RUN_ID,
                        "script_execution": "error",
                        "timestamp": {"start": "2026-08-05T08:59:00+00:00"},
                    },
                    {
                        "run_id": "finished000000000000000000000000",
                        "script_execution": "finished",
                        "timestamp": {"start": "2026-08-05T08:58:00+00:00"},
                    },
                ],
            },
            {
                "id": 21,
                "type": "result",
                "success": True,
                "result": {
                    "config": {
                        "actions": [{
                            "action": "light.turn_on",
                            "target": {"entity_id": "light.rele_2_garderob"},
                        }]
                    },
                    "trace": {
                        "action/0": [{
                            "error": "ClientConnectorError(ConnectionKey(host='iot.quasar.yandex.ru', port=443), OSError(101, 'Network is unreachable'))",
                            "private": TOKEN,
                        }]
                    },
                },
            },
        ]

    def recv(self) -> str:
        return json.dumps(self.replies.pop(0))

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def close(self) -> None:
        pass


class AutomationDiagnosticsTests(unittest.TestCase):
    def _store(self, temporary: str) -> incident_monitor.IncidentStore:
        state = Path(temporary) / "state"
        state.mkdir(mode=0o700)
        return incident_monitor.IncidentStore(
            state / incident_monitor.DATABASE_NAME
        )

    def test_yandex_network_failure_is_sanitized_and_idempotent(self) -> None:
        config = ha_read.AdapterConfig(
            "http", "192.168.1.127", 8123, TOKEN, (), True
        )
        socket = FakeSocket()
        raw_states = [{
            "entity_id": "automation.garderob_rele_po_datchiku_dvizheniia",
            "state": "on",
            "attributes": {
                "id": ITEM_ID,
                "friendly_name": "Гардероб - реле 2 логика движения",
                "last_triggered": "2026-08-05T08:59:00+00:00",
            },
        }]
        records = diagnostics.collect(
            config,
            connector=lambda _config: socket,
            raw_state_reader=lambda _config, path: raw_states,
            observed_epoch=1_785_920_400,
        )
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["cause_code"], "yandex_cloud_unreachable")
        self.assertEqual(record["cause_confidence"], "confirmed")
        self.assertEqual(record["error_code"], "network_unreachable")
        self.assertEqual(record["action_code"], "light.turn_on")
        self.assertEqual(record["target_entity_id"], "light.rele_2_garderob")
        self.assertNotIn(TOKEN, json.dumps(record, ensure_ascii=False))
        self.assertEqual(socket.sent[1], {
            "id": 20,
            "type": "trace/list",
            "domain": "automation",
            "item_id": ITEM_ID,
        })
        self.assertEqual(socket.sent[2], {
            "id": 21,
            "type": "trace/get",
            "domain": "automation",
            "item_id": ITEM_ID,
            "run_id": RUN_ID,
        })

        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                store.replace_entity_device_map([{
                    "entity_id": "light.rele_2_garderob",
                    "physical_device_hash": PHYSICAL_HASH,
                    "device_id": DEVICE_ID,
                    "platform": "yandex_station",
                    "config_entry_ids": [],
                }], 1_785_920_400)
                first = store.record_automation_run(**record)
                second = store.record_automation_run(**record)
                self.assertTrue(first["recorded"])
                self.assertIsNotNone(first["incident_id"])
                self.assertFalse(second["recorded"])
                self.assertIsNone(second["incident_id"])
                incident = store.connection.execute(
                    "SELECT * FROM operational_incidents"
                ).fetchone()
                self.assertEqual(incident["physical_device_hash"], PHYSICAL_HASH)
                self.assertEqual(incident["cause_code"], "yandex_cloud_unreachable")
                self.assertEqual(incident["safety_class"], "light")
                database_dump = " ".join(
                    str(value)
                    for row in store.connection.iterdump()
                    for value in [row]
                )
                self.assertNotIn(TOKEN, database_dump)
                self.assertTrue(store.resolve_operational_incident(
                    int(first["incident_id"]), 1_785_920_430,
                    "target_state_confirmed",
                ))
                self.assertFalse(store.resolve_operational_incident(
                    int(first["incident_id"]), 1_785_920_431,
                    "target_state_confirmed",
                ))
            finally:
                store.close()

    def test_classifier_distinguishes_dns_timeout_and_unknown(self) -> None:
        self.assertEqual(
            diagnostics.classify_failure({"error": "ClientConnectorDNSError: gaierror"}),
            ("dns_resolution_failed", "dns_resolution_failed", "confirmed"),
        )
        self.assertEqual(
            diagnostics.classify_failure({
                "error": "Server Timeout connecting to iot.quasar.yandex.ru"
            }),
            ("upstream_timeout", "yandex_cloud_unreachable", "probable"),
        )
        self.assertEqual(
            diagnostics.classify_failure({"error": "HOSTILE " + TOKEN}),
            ("automation_action_failed", "automation_action_failed", "probable"),
        )

    def test_multiple_targets_are_not_guessed(self) -> None:
        targets, action = diagnostics._targets_and_action({
            "action": "light.turn_off",
            "target": {
                "entity_id": ["light.first", "switch.second", TOKEN]
            },
        })
        self.assertEqual(targets, ["light.first", "switch.second"])
        self.assertEqual(action, "light.turn_off")

    def test_stopped_trace_without_explicit_error_is_not_a_failure(self) -> None:
        self.assertFalse(diagnostics._has_explicit_error({
            "trace": {"action/0": [{"result": {"params": {}}}]}
        }))
        self.assertTrue(diagnostics._has_explicit_error({
            "trace": {"action/0": [{"error": "TimeoutError"}]}
        }))


if __name__ == "__main__":
    unittest.main()
