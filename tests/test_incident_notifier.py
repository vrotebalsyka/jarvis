#!/usr/bin/env python3
"""Offline deduplication tests for critical Alice notifications."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import incident_monitor as monitor  # noqa: E402
import incident_notifier as notifier  # noqa: E402


class IncidentNotifierTests(unittest.TestCase):
    def _confirmed_store(self, temporary: str) -> monitor.IncidentStore:
        state = Path(temporary) / "state"
        state.mkdir(mode=0o700)
        store = monitor.IncidentStore(state / monitor.DATABASE_NAME)
        store.observe(
            monitor.RESERVED_SUBJECT, "system", "unreachable", 100,
            unavailable=True, source="test",
        )
        store.confirm_due(120, monitor.CONFIRM_AFTER_SECONDS)
        return store

    def _confirmed_sensor_store(self, temporary: str) -> monitor.IncidentStore:
        state = Path(temporary) / "state"
        state.mkdir(mode=0o700)
        store = monitor.IncidentStore(state / monitor.DATABASE_NAME)
        with store.connection:
            store.connection.executemany(
                "UPDATE notification_policies SET enabled_epoch=? WHERE name=?",
                (
                    (90, monitor.SENSOR_NOTIFICATION_POLICY),
                    (90, monitor.DEVICE_NOTIFICATION_POLICY),
                ),
            )
        store.observe(
            "sensor.temperature", "entity", "unavailable", 100,
            unavailable=True, source="test",
        )
        store.confirm_due(120, monitor.CONFIRM_AFTER_SECONDS)
        return store

    def test_dry_run_does_not_mark_or_claim_service_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._confirmed_store(temporary)
            try:
                with mock.patch.dict(os.environ, {"HOME_BUTLER_ALICE_NOTIFY": "dry-run"}), mock.patch(
                    "incident_notifier.ha_notify.send_incident",
                    return_value={"ok": True, "speaker_entity_id": "media_player.yandex_station_m10vgng0005wxb"},
                ):
                    result = notifier.run_once(store, now=200)
                self.assertEqual(result["candidates"], 1)
                self.assertEqual(result["service_calls"], 0)
                amount = store.connection.execute("SELECT COUNT(*) FROM incident_notifications").fetchone()[0]
                self.assertEqual(amount, 0)
            finally:
                store.close()

    def test_live_notification_is_sent_once_per_phase(self) -> None:
        response = {"ok": True, "speaker_entity_id": "media_player.yandex_station_m10vgng0005wxb"}
        with tempfile.TemporaryDirectory() as temporary:
            store = self._confirmed_store(temporary)
            try:
                with mock.patch.dict(os.environ, {"HOME_BUTLER_ALICE_NOTIFY": "live"}), mock.patch(
                    "incident_notifier.ha_notify.send_incident", return_value=response
                ) as send:
                    first = notifier.run_once(store, now=200)
                    second = notifier.run_once(store, now=201)
                    store.observe(
                        monitor.RESERVED_SUBJECT, "system", "reachable", 220,
                        unavailable=False, source="test",
                    )
                    third = notifier.run_once(store, now=221)
                self.assertEqual(first["service_calls"], 1)
                self.assertEqual(second["candidates"], 0)
                self.assertEqual(third["service_calls"], 1)
                self.assertEqual(send.call_count, 2)
            finally:
                store.close()

    def test_device_notice_uses_device_sender_after_twenty_seconds(self) -> None:
        response = {
            "ok": True,
            "speaker_entity_id": "media_player.yandex_station_x10x2a000qpm2b",
        }
        with tempfile.TemporaryDirectory() as temporary:
            store = self._confirmed_sensor_store(temporary)
            try:
                with mock.patch.dict(
                    os.environ, {"HOME_BUTLER_ALICE_NOTIFY": "live"}
                ), mock.patch(
                    "incident_notifier.ha_notify.send_device_incident",
                    return_value=response,
                ) as send_device, mock.patch(
                    "incident_notifier.ha_notify.send_incident"
                ) as send_critical:
                    early = notifier.run_once(store, now=119)
                    due = notifier.run_once(store, now=120)
                self.assertEqual(early["candidates"], 0)
                self.assertEqual(due["service_calls"], 1)
                send_device.assert_called_once_with(
                    "temperature",
                    "confirmed",
                    cause_code="unknown",
                    duration_seconds=None,
                    live=True,
                )
                send_critical.assert_not_called()
            finally:
                store.close()

    def test_switch_device_is_not_excluded_from_outage_notifications(self) -> None:
        response = {
            "ok": True,
            "speaker_entity_id": "media_player.yandex_station_x10x2a000qpm2b",
        }
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir(mode=0o700)
            store = monitor.IncidentStore(state / monitor.DATABASE_NAME)
            try:
                with store.connection:
                    store.connection.execute(
                        "UPDATE notification_policies SET enabled_epoch=? WHERE name=?",
                        (90, monitor.DEVICE_NOTIFICATION_POLICY),
                    )
                store.replace_entity_device_map([{
                    "entity_id": "switch.humidifier",
                    "physical_device_hash": "d" * 64,
                    "device_id": "3" * 32,
                    "platform": "tuya",
                    "config_entry_ids": ["C" * 26],
                }], 90)
                store.observe(
                    "switch.humidifier", "entity", "unavailable", 100,
                    unavailable=True, source="test",
                )
                store.confirm_due(120, monitor.CONFIRM_AFTER_SECONDS)
                with mock.patch.dict(
                    os.environ, {"HOME_BUTLER_ALICE_NOTIFY": "live"}
                ), mock.patch(
                    "incident_notifier.ha_notify.send_device_incident",
                    return_value=response,
                ) as send_device:
                    result = notifier.run_once(store, now=120)
                self.assertEqual(result["service_calls"], 1)
                send_device.assert_called_once()
                self.assertEqual(send_device.call_args.args[0], "humidifier")
            finally:
                store.close()

    def test_three_entities_of_one_physical_sensor_send_one_notice(self) -> None:
        response = {
            "ok": True,
            "speaker_entity_id": "media_player.yandex_station_x10x2a000qpm2b",
        }
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir(mode=0o700)
            store = monitor.IncidentStore(state / monitor.DATABASE_NAME)
            try:
                with store.connection:
                    store.connection.execute(
                        "UPDATE notification_policies SET enabled_epoch=? WHERE name=?",
                        (90, monitor.DEVICE_NOTIFICATION_POLICY),
                    )
                physical_hash = "a" * 64
                store.replace_entity_device_map([
                    {
                        "entity_id": "binary_sensor.presence_motion",
                        "physical_device_hash": physical_hash,
                        "device_id": "1" * 32,
                        "platform": "tuya_local",
                        "config_entry_ids": ["A" * 26],
                    },
                    {
                        "entity_id": "binary_sensor.presence_occupancy",
                        "physical_device_hash": physical_hash,
                        "device_id": "2" * 32,
                        "platform": "tuya",
                        "config_entry_ids": ["B" * 26],
                    },
                    {
                        "entity_id": "sensor.presence_battery",
                        "physical_device_hash": physical_hash,
                        "device_id": "1" * 32,
                        "platform": "tuya_local",
                        "config_entry_ids": ["A" * 26],
                    },
                ], 90)
                for entity_id in (
                    "binary_sensor.presence_motion",
                    "binary_sensor.presence_occupancy",
                    "sensor.presence_battery",
                ):
                    store.observe(
                        entity_id, "entity", "unavailable", 100,
                        unavailable=True, source="test",
                    )
                store.confirm_due(160, 60)
                with mock.patch.dict(
                    os.environ, {"HOME_BUTLER_ALICE_NOTIFY": "live"}
                ), mock.patch(
                    "incident_notifier.ha_notify.send_device_incident",
                    return_value=response,
                ) as send_device, mock.patch(
                    "incident_notifier.ha_recovery.load_platform_map",
                    return_value={
                        "binary_sensor.presence_motion": "tuya_local",
                        "binary_sensor.presence_occupancy": "tuya",
                        "sensor.presence_battery": "tuya_local",
                    },
                ), mock.patch(
                    "incident_notifier.ha_recovery.load_ip_drift_map",
                    return_value={},
                ):
                    result = notifier.run_once(store, now=220)
                self.assertEqual(result["service_calls"], 1)
                self.assertEqual(send_device.call_count, 1)
                self.assertEqual(result["device_diagnosis"], {
                    "candidates": 2,
                    "devices": 1,
                    "service_calls": 0,
                })
                self.assertEqual(
                    send_device.call_args.kwargs["cause_code"],
                    "tuya_integration_unavailable",
                )
                device_incidents = store.connection.execute(
                    "SELECT COUNT(*) FROM device_incidents"
                ).fetchone()[0]
                members = store.connection.execute(
                    "SELECT COUNT(*) FROM device_incident_members"
                ).fetchone()[0]
                self.assertEqual((device_incidents, members), (1, 3))
            finally:
                store.close()

    def test_automation_failure_and_recovery_each_speak_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir(mode=0o700)
            store = monitor.IncidentStore(state / monitor.DATABASE_NAME)
            try:
                created = store.record_automation_run(
                    run_hash="a" * 64,
                    automation_entity_id="automation.garderob",
                    automation_item_hash="b" * 64,
                    outcome="failed",
                    started_epoch=100,
                    observed_epoch=101,
                    error_code="network_unreachable",
                    cause_code="yandex_cloud_unreachable",
                    cause_confidence="confirmed",
                    action_code="light.turn_on",
                    target_entity_id="light.garderob",
                    display_name="Гардероб",
                )
                sent: list[tuple[str, str]] = []
                with mock.patch.dict(os.environ, {"HOME_BUTLER_ALICE_NOTIFY": "live"}), mock.patch(
                    "incident_notifier.ha_notify.send_operational_incident",
                    side_effect=lambda name, phase, **_kwargs: (
                        sent.append((name, phase))
                        or {
                            "ok": True,
                            "speaker_entity_id": "media_player.yandex_station_x10x2a000qpm2b",
                        }
                    ),
                ):
                    first = notifier.run_once(store, now=110)
                    second = notifier.run_once(store, now=111)
                    self.assertEqual(first["accepted"], 1)
                    self.assertEqual(second["accepted"], 0)
                    store.resolve_operational_incident(
                        int(created["incident_id"]), 160,
                        "target_state_confirmed",
                    )
                    third = notifier.run_once(store, now=161)
                    fourth = notifier.run_once(store, now=162)
                self.assertEqual(third["accepted"], 1)
                self.assertEqual(fourth["accepted"], 0)
                self.assertEqual(sent, [
                    ("Гардероб", "detected"),
                    ("Гардероб", "resolved"),
                ])
            finally:
                store.close()

    def test_operational_failure_retries_three_times_then_stops(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir(mode=0o700)
            store = monitor.IncidentStore(state / monitor.DATABASE_NAME)
            try:
                created = store.record_automation_run(
                    run_hash="c" * 64,
                    automation_entity_id="automation.retry_test",
                    automation_item_hash="d" * 64,
                    outcome="failed",
                    started_epoch=100,
                    observed_epoch=101,
                    error_code="network_unreachable",
                    cause_code="integration_unavailable",
                    cause_confidence="confirmed",
                    action_code="switch.turn_on",
                    target_entity_id="switch.retry_test",
                    display_name="Проверка доставки",
                )
                with mock.patch.dict(
                    os.environ, {"HOME_BUTLER_ALICE_NOTIFY": "live"}
                ), mock.patch(
                    "incident_notifier.ha_notify.send_operational_incident",
                    return_value={
                        "ok": False,
                        "speaker_entity_id": "media_player.yandex_station_x10x2a000qpm2b",
                    },
                ) as send:
                    first = notifier.run_once(store, now=110)
                    early = notifier.run_once(store, now=139)
                    second = notifier.run_once(store, now=140)
                    third = notifier.run_once(store, now=170)
                    stopped = notifier.run_once(store, now=200)
                self.assertEqual(first["failed"], 1)
                self.assertEqual(early["candidates"], 0)
                self.assertEqual(second["failed"], 1)
                self.assertEqual(third["failed"], 1)
                self.assertEqual(stopped["candidates"], 0)
                self.assertEqual(send.call_count, 3)
                notice = store.connection.execute(
                    "SELECT status,attempts FROM operational_incident_notifications "
                    "WHERE operational_incident_id=? AND phase='detected'",
                    (int(created["incident_id"]),),
                ).fetchone()
                self.assertEqual((notice["status"], notice["attempts"]), (
                    "delivery_unknown", 3,
                ))
            finally:
                store.close()

    def test_operational_recovery_is_silent_without_delivered_outage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir(mode=0o700)
            store = monitor.IncidentStore(state / monitor.DATABASE_NAME)
            try:
                created = store.record_automation_run(
                    run_hash="e" * 64,
                    automation_entity_id="automation.silent_recovery",
                    automation_item_hash="f" * 64,
                    outcome="failed",
                    started_epoch=100,
                    observed_epoch=101,
                    error_code="network_unreachable",
                    cause_code="integration_unavailable",
                    cause_confidence="confirmed",
                    action_code="switch.turn_on",
                    target_entity_id="switch.silent_recovery",
                    display_name="Тихое восстановление",
                )
                with mock.patch.dict(
                    os.environ, {"HOME_BUTLER_ALICE_NOTIFY": "live"}
                ), mock.patch(
                    "incident_notifier.ha_notify.send_operational_incident",
                    return_value={
                        "ok": False,
                        "speaker_entity_id": "media_player.yandex_station_x10x2a000qpm2b",
                    },
                ):
                    notifier.run_once(store, now=110)
                store.resolve_operational_incident(
                    int(created["incident_id"]), 120, "target_state_confirmed"
                )
                with mock.patch.dict(
                    os.environ, {"HOME_BUTLER_ALICE_NOTIFY": "live"}
                ), mock.patch(
                    "incident_notifier.ha_notify.send_operational_incident"
                ) as send:
                    result = notifier.run_once(store, now=500)
                self.assertEqual(result["candidates"], 0)
                send.assert_not_called()
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
