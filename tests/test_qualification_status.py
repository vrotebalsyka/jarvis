#!/usr/bin/env python3
"""Contracts for the real reboot and physical-device proof checklist."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import incident_monitor as monitor  # noqa: E402
import dialogue_qualification as dialogue  # noqa: E402
import qualification_status as status  # noqa: E402


BOOT_ID = "01234567-89ab-cdef-0123-456789abcdef"


class QualificationStatusTests(unittest.TestCase):
    def _history(self, path: Path, boots: int) -> None:
        entries = []
        for index in range(boots):
            entries.append({
                "windows_boot_id": f"2026-08-{11 + index:02d}T04:16:54Z",
                "wsl_boot_id": f"{index:08x}-89ab-cdef-0123-456789abcdef",
                "verified_at": f"2026-08-{11 + index:02d}T04:18:54Z",
                "accelerator": "gpu",
                "startup_self_check_ready": True,
                "alice_public_ready": True,
                "dialogue_qualification_ready": True,
            })
        path.write_text(json.dumps({
            "schema_version": 2,
            "baseline_boot_id": entries[0]["windows_boot_id"],
            "verified_reboot_count": boots - 1,
            "entries": entries,
        }), encoding="utf-8")

    def test_reboot_count_excludes_the_baseline_boot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history.json"
            self._history(path, 4)
            self.assertEqual(status.read_reboot_count(path), 3)

    def test_five_humidifier_entities_are_one_completed_device_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir(mode=0o700)
            database = state / monitor.DATABASE_NAME
            store = monitor.IncidentStore(database)
            try:
                with store.connection:
                    store.connection.execute(
                        "UPDATE notification_policies SET enabled_epoch=? WHERE name=?",
                        (90, monitor.DEVICE_NOTIFICATION_POLICY),
                    )
                physical_hash = "a" * 64
                entities = [
                    f"sensor.uvlazhnitel_{index}" for index in range(5)
                ]
                store.replace_entity_device_map([{
                    "entity_id": entity,
                    "physical_device_hash": physical_hash,
                    "device_id": "1" * 32,
                    "platform": "tuya",
                    "config_entry_ids": ["A" * 26],
                } for entity in entities], 90)
                for entity in entities:
                    store.observe(
                        entity, "entity", "unavailable", 100,
                        unavailable=True, source="websocket",
                    )
                store.confirm_due(120, monitor.CONFIRM_AFTER_SECONDS)
                store.reconcile_device_incidents(120)
                device_id = int(store.connection.execute(
                    "SELECT id FROM device_incidents"
                ).fetchone()[0])
                store.record_device_notification(
                    device_id, "confirmed", 125, status="accepted",
                    speaker_entity_id="media_player.yandex_station_x10x2a000qpm2b",
                )
                for entity in entities:
                    store.observe(
                        entity, "entity", "available", 130,
                        unavailable=False, source="websocket",
                    )
                store.reconcile_device_incidents(130)
                store.record_device_notification(
                    device_id, "resolved", 135, status="accepted",
                    speaker_entity_id="media_player.yandex_station_x10x2a000qpm2b",
                )
            finally:
                store.close()
            history = root / "history.json"
            self._history(history, 4)
            document = status.read_status(
                reboot_path=history,
                database_path=database,
                expected_uid=os.geteuid(),
            )
            humidifier = next(
                item for item in document["devices"]
                if item["key"] == "humidifier"
            )
            self.assertEqual(humidifier["state"], "passed")
            self.assertEqual(humidifier["member_count"], 5)
            self.assertTrue(humidifier["one_outage_notice"])
            self.assertTrue(humidifier["one_recovery_notice"])
            self.assertEqual(humidifier["alert_seconds"], 25)
            self.assertEqual(
                [item["state"] for item in document["devices"]],
                ["pending", "pending", "passed"],
            )

    def test_invalid_windows_history_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history.json"
            path.write_text('{"schema_version":1}', encoding="utf-8")
            with self.assertRaises(status.QualificationError):
                status.read_reboot_count(path)

    def test_dialogue_proof_is_private_and_bound_to_current_boot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "dialogue"
            state.mkdir(mode=0o700)
            dialogue.write_status({
                "schema_version": 1,
                "observed_epoch": 100,
                "boot_id": BOOT_ID,
                "ready": True,
                "local_chat_ready": True,
                "alice_public_ready": True,
                "history_verified": True,
                "free_dialogue_verified": True,
                "fake_tool_claim_absent": True,
                "local_answer_lengths": [60, 40, 100],
                "alice_answer_lengths": [60, 40, 100],
            }, state)
            self.assertEqual(
                status._dialogue_proof(
                    state_dir=state, current_boot_id=BOOT_ID
                )["state"],
                "passed",
            )
            self.assertEqual(
                status._dialogue_proof(
                    state_dir=state,
                    current_boot_id="fedcba98-7654-3210-fedc-ba9876543210",
                )["state"],
                "pending",
            )


if __name__ == "__main__":
    unittest.main()
