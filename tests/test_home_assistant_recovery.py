#!/usr/bin/env python3
"""Offline safety contracts for bounded LocalTuya recovery."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import home_assistant_recovery as recovery  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402
import incident_monitor as monitor  # noqa: E402


TOKEN = "SECRET_SENTINEL.DO_NOT_LEAK.abcdefghijklmnopqrstuvwxyz"
ENTITY = "switch.new_failure"
XIAOMI_ENTITY = "sensor.xiaomi_second_failure"
XIAOMI_ENTRY = "A" * 26
CLOUD_ENTITY = "binary_sensor.new_failure_cloud"


def config() -> ha_read.AdapterConfig:
    return ha_read.AdapterConfig("http", "192.168.1.127", 8123, TOKEN, (), True)


class FakeResponse:
    status = 200

    def read(self, _amount: int) -> bytes:
        return b"[]"


class FakeConnection:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, bytes, dict[str, str]]] = []

    def request(self, method, path, *, body, headers) -> None:
        self.requests.append((method, path, body, headers))

    def getresponse(self) -> FakeResponse:
        return FakeResponse()

    def close(self) -> None:
        pass


def snapshot(kind: str) -> dict[str, object]:
    return {"entities": [{"entity_id": ENTITY, "state_kind": kind}]}


class RecoveryTests(unittest.TestCase):
    def _store(self, temporary: str, *, baseline: bool = False) -> monitor.IncidentStore:
        directory = Path(temporary) / "state"
        directory.mkdir(mode=0o700)
        store = monitor.IncidentStore(directory / monitor.DATABASE_NAME)
        store.observe(
            ENTITY, "entity", "unavailable", 100,
            unavailable=True, source="startup_snapshot" if baseline else "websocket",
        )
        store.confirm_due(160, 60)
        return store

    def test_fixed_reload_path_and_empty_body(self) -> None:
        connection = FakeConnection()
        config = ha_read.AdapterConfig("http", "192.168.1.127", 8123, TOKEN, (), True)
        recovery.post_localtuya_reload(
            config, connection_factory=lambda _config: connection
        )
        method, path, body, headers = connection.requests[0]
        self.assertEqual(
            (method, path, body),
            ("POST", recovery.LOCALTUYA_SERVICE_PATH, b"{}"),
        )
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)

        local_connection = FakeConnection()
        recovery.post_tuya_local_reload(
            config,
            ENTITY,
            connection_factory=lambda _config: local_connection,
        )
        method, path, body, headers = local_connection.requests[0]
        self.assertEqual((method, path), ("POST", recovery.TUYA_LOCAL_SERVICE_PATH))
        self.assertEqual(json.loads(body), {"entity_id": ENTITY})
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)

    def test_new_confirmed_localtuya_failure_reloads_once_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            calls: list[str] = []
            snapshots = iter((snapshot("unavailable"), snapshot("enum")))
            try:
                with mock.patch.object(
                    ha_read,
                    "load_config",
                    return_value=ha_read.AdapterConfig("http", "192.168.1.127", 8123, TOKEN, (), True),
                ):
                    first = recovery.run_once(
                        store,
                        {ENTITY: "localtuya"},
                        now=200,
                        live=True,
                        snapshot_reader=lambda _action: (next(snapshots), 0),
                        localtuya_caller=lambda _config: calls.append("reload"),
                        sleeper=lambda _seconds: None,
                    )
                    second = recovery.run_once(
                        store,
                        {ENTITY: "localtuya"},
                        now=201,
                        live=True,
                        snapshot_reader=lambda _action: (snapshot("unavailable"), 0),
                        localtuya_caller=lambda _config: calls.append("unexpected"),
                        sleeper=lambda _seconds: None,
                    )
                self.assertEqual(first["service_calls"], 1)
                self.assertEqual(first["verified"], 1)
                self.assertEqual(second["candidates"], 0)
                self.assertEqual(calls, ["reload"])
                row = store.connection.execute("SELECT * FROM recovery_actions").fetchone()
                self.assertEqual(row["status"], "verified")
                self.assertEqual(row["action"], "localtuya.reload")
            finally:
                store.close()

    def test_reload_readback_retries_at_20_40_and_60_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            snapshots = iter((
                snapshot("unavailable"),
                snapshot("unavailable"),
                snapshot("unavailable"),
                snapshot("enum"),
            ))
            sleeps: list[float] = []
            try:
                with mock.patch.object(ha_read, "load_config", return_value=config()):
                    result = recovery.run_once(
                        store,
                        {ENTITY: "localtuya"},
                        now=200,
                        live=True,
                        snapshot_reader=lambda _action: (next(snapshots), 0),
                        localtuya_caller=lambda _config: None,
                        sleeper=sleeps.append,
                    )
                self.assertEqual(sleeps, [20, 20, 20])
                self.assertEqual(result["verification_checks"], 3)
                self.assertEqual(result["verified"], 1)
                row = store.connection.execute(
                    "SELECT verification_checks FROM recovery_actions"
                ).fetchone()
                self.assertEqual(row["verification_checks"], 3)
            finally:
                store.close()

    def test_baseline_and_other_platform_are_never_recovered(self) -> None:
        for baseline, platform in ((True, "localtuya"), (False, "tuya")):
            with self.subTest(baseline=baseline, platform=platform), tempfile.TemporaryDirectory() as temporary:
                store = self._store(temporary, baseline=baseline)
                try:
                    result = recovery.run_once(
                        store,
                        {ENTITY: platform},
                        now=200,
                        live=True,
                        snapshot_reader=lambda _action: (snapshot("unavailable"), 0),
                        localtuya_caller=lambda _config: self.fail("service must not be called"),
                        tuya_local_caller=lambda _config, _entity: self.fail("service must not be called"),
                        sleeper=lambda _seconds: None,
                    )
                    self.assertEqual(result["service_calls"], 0)
                finally:
                    store.close()

    def test_restricted_or_unknown_device_requires_permission(self) -> None:
        for entity_id in ("lock.front_door", "fan.unknown_device"):
            with self.subTest(entity_id=entity_id), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "state"
                directory.mkdir(mode=0o700)
                store = monitor.IncidentStore(directory / monitor.DATABASE_NAME)
                store.observe(
                    entity_id, "entity", "unavailable", 100,
                    unavailable=True, source="websocket",
                )
                store.confirm_due(160, 60)
                try:
                    result = recovery.run_once(
                        store,
                        {entity_id: "tuya_local"},
                        now=200,
                        live=True,
                        snapshot_reader=lambda _action: ({
                            "entities": [{
                                "entity_id": entity_id,
                                "state_kind": "unavailable",
                            }],
                        }, 0),
                        tuya_local_caller=lambda *_args: self.fail(
                            "restricted recovery called a service"
                        ),
                    )
                    self.assertEqual(result["outcome"], "permission_required")
                    self.assertEqual(result["service_calls"], 0)
                finally:
                    store.close()

    def test_dry_run_reports_candidate_without_service_or_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                result = recovery.run_once(
                    store,
                    {ENTITY: "localtuya"},
                    now=200,
                    live=False,
                    snapshot_reader=lambda _action: (snapshot("unavailable"), 0),
                    localtuya_caller=lambda _config: self.fail("dry-run called service"),
                    tuya_local_caller=lambda _config, _entity: self.fail("dry-run called service"),
                )
                self.assertEqual(result["candidates"], 1)
                self.assertEqual(result["service_calls"], 0)
                amount = store.connection.execute("SELECT COUNT(*) FROM recovery_actions").fetchone()[0]
                self.assertEqual(amount, 0)
            finally:
                store.close()

    def test_diagnosis_only_enriches_device_before_notification_without_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                store.reconcile_device_incidents(170)
                result = recovery.diagnose_open_device_incidents(
                    store,
                    {ENTITY: "tuya_local"},
                    ip_drift_map={},
                )
                self.assertEqual(result, {
                    "candidates": 1,
                    "devices": 1,
                    "service_calls": 0,
                })
                device = store.connection.execute(
                    "SELECT cause_code,cause_confidence FROM device_incidents"
                ).fetchone()
                self.assertEqual(
                    (device["cause_code"], device["cause_confidence"]),
                    ("tuya_integration_unavailable", "probable"),
                )
                action_count = store.connection.execute(
                    "SELECT COUNT(*) FROM recovery_actions"
                ).fetchone()[0]
                self.assertEqual(action_count, 0)
            finally:
                store.close()

    def test_confirmed_ip_drift_is_used_as_recovery_diagnosis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                result = recovery.run_once(
                    store,
                    {ENTITY: "localtuya"},
                    now=200,
                    live=False,
                    snapshot_reader=lambda _action: (snapshot("unavailable"), 0),
                    ip_drift_map={ENTITY: "ip_changed"},
                )
                self.assertEqual(result["diagnosis"], "confirmed_ip_change")
                self.assertEqual(result["service_calls"], 0)
            finally:
                store.close()

    def test_new_tuya_local_failure_reloads_exact_config_entry_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            calls: list[str] = []
            snapshots = iter((snapshot("unavailable"), snapshot("enum")))
            try:
                with mock.patch.object(ha_read, "load_config", return_value=config()):
                    result = recovery.run_once(
                        store,
                        {ENTITY: "tuya_local"},
                        now=200,
                        live=True,
                        snapshot_reader=lambda _action: (next(snapshots), 0),
                        localtuya_caller=lambda _config: self.fail("wrong adapter"),
                        tuya_local_caller=lambda _config, entity_id: calls.append(entity_id),
                        sleeper=lambda _seconds: None,
                    )
                self.assertEqual(calls, [ENTITY])
                self.assertEqual(result["integration"], "tuya_local")
                self.assertEqual(result["action"], "homeassistant.reload_config_entry")
                self.assertEqual(result["verified"], 1)
                row = store.connection.execute("SELECT * FROM recovery_actions").fetchone()
                self.assertEqual(row["integration"], "tuya_local")
                self.assertEqual(row["action"], "homeassistant.reload_config_entry")
            finally:
                store.close()

    def test_tuya_recovery_groups_duplicate_entities_and_verifies_every_member(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            store.observe(
                CLOUD_ENTITY, "entity", "unavailable", 101,
                unavailable=True, source="websocket",
            )
            store.confirm_due(161, 60)
            store.replace_entity_device_map([
                {
                    "entity_id": ENTITY,
                    "physical_device_hash": "a" * 64,
                    "device_id": "b" * 32,
                    "platform": "tuya_local",
                    "config_entry_ids": [],
                },
                {
                    "entity_id": CLOUD_ENTITY,
                    "physical_device_hash": "a" * 64,
                    "device_id": "c" * 32,
                    "platform": "tuya",
                    "config_entry_ids": [],
                },
            ], 170)
            snapshots = iter((
                {"entities": [
                    {"entity_id": ENTITY, "state_kind": "unavailable"},
                    {"entity_id": CLOUD_ENTITY, "state_kind": "unavailable"},
                ]},
                {"entities": [
                    {"entity_id": ENTITY, "state_kind": "enum"},
                    {"entity_id": CLOUD_ENTITY, "state_kind": "enum"},
                ]},
            ))
            calls: list[str] = []
            try:
                with mock.patch.object(ha_read, "load_config", return_value=config()):
                    result = recovery.run_once(
                        store,
                        {ENTITY: "tuya_local", CLOUD_ENTITY: "tuya"},
                        now=200,
                        live=True,
                        snapshot_reader=lambda _action: (next(snapshots), 0),
                        tuya_local_caller=lambda _config, entity_id: calls.append(entity_id),
                        sleeper=lambda _seconds: None,
                    )
                self.assertEqual(calls, [ENTITY])
                self.assertEqual(result["candidates"], 2)
                self.assertEqual(result["physical_member_count"], 2)
                self.assertEqual(result["verified_member_count"], 2)
                self.assertTrue(result["physical_device_verified"])
                device = store.connection.execute(
                    "SELECT cause_code,cause_confidence FROM device_incidents"
                ).fetchone()
                self.assertEqual(device["cause_code"], "tuya_integration_unavailable")
                self.assertEqual(device["cause_confidence"], "probable")
            finally:
                store.close()

    def test_xiaomi_group_is_visible_but_requires_separate_permission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                result = recovery.run_once(
                    store,
                    {ENTITY: "xiaomi_miot"},
                    now=200,
                    live=True,
                    snapshot_reader=lambda _action: (snapshot("unavailable"), 0),
                    xiaomi_entry_map={ENTITY: XIAOMI_ENTRY},
                    xiaomi_caller=lambda _config, _entity: self.fail(
                        "unapproved Xiaomi recovery called a service"
                    ),
                )
                self.assertEqual(result["outcome"], "permission_required")
                self.assertEqual(result["recovery_scope"], "one_config_entry")
                self.assertEqual(result["candidates"], 1)
                self.assertEqual(result["service_calls"], 0)
                amount = store.connection.execute(
                    "SELECT COUNT(*) FROM recovery_actions"
                ).fetchone()[0]
                self.assertEqual(amount, 0)
            finally:
                store.close()

    def test_approved_xiaomi_group_reloads_one_entry_once_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            store.observe(
                XIAOMI_ENTITY,
                "entity",
                "unavailable",
                101,
                unavailable=True,
                source="websocket",
            )
            store.confirm_due(161, 60)
            before = {
                "entities": [
                    {"entity_id": ENTITY, "state_kind": "unavailable"},
                    {"entity_id": XIAOMI_ENTITY, "state_kind": "unavailable"},
                ]
            }
            after = {
                "entities": [
                    {"entity_id": ENTITY, "state_kind": "enum"},
                    {"entity_id": XIAOMI_ENTITY, "state_kind": "number"},
                ]
            }
            snapshots = iter((before, after))
            calls: list[str] = []
            try:
                with mock.patch.object(ha_read, "load_config", return_value=config()):
                    result = recovery.run_once(
                        store,
                        {ENTITY: "xiaomi_miot", XIAOMI_ENTITY: "xiaomi_miot"},
                        now=200,
                        live=True,
                        snapshot_reader=lambda _action: (next(snapshots), 0),
                        xiaomi_entry_map={
                            ENTITY: XIAOMI_ENTRY,
                            XIAOMI_ENTITY: XIAOMI_ENTRY,
                        },
                        xiaomi_approved=True,
                        xiaomi_caller=lambda _config, entity_id: calls.append(entity_id),
                        sleeper=lambda _seconds: None,
                    )
                self.assertEqual(calls, [ENTITY])
                self.assertEqual(result["integration"], "xiaomi_miot")
                self.assertEqual(result["action"], "homeassistant.reload_config_entry")
                self.assertEqual(result["candidates"], 2)
                self.assertEqual(result["service_calls"], 1)
                self.assertEqual(result["verified"], 2)
                rows = store.connection.execute(
                    "SELECT * FROM recovery_actions ORDER BY id"
                ).fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual(sum(row["service_calls"] for row in rows), 1)
            finally:
                store.close()

    def test_xiaomi_inventory_map_requires_one_exact_config_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "inventory.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "config_entries": [
                    {"entry_id": XIAOMI_ENTRY, "domain": "xiaomi_miot"},
                ],
                "entities": [
                    {
                        "entity_id": ENTITY,
                        "platform": "xiaomi_miot",
                        "config_entry_ids": [XIAOMI_ENTRY],
                    },
                    {
                        "entity_id": XIAOMI_ENTITY,
                        "platform": "xiaomi_miot",
                        "config_entry_ids": [XIAOMI_ENTRY, "B" * 26],
                    },
                ],
            }))
            path.chmod(0o600)
            self.assertEqual(
                recovery.load_xiaomi_entry_map(path),
                {ENTITY: XIAOMI_ENTRY},
            )

    def test_inventory_loader_rejects_duplicate_or_hostile_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "inventory.json"
            path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "entities": [
                        {"entity_id": ENTITY, "platform": "localtuya"},
                        {"entity_id": ENTITY, "platform": "localtuya"},
                    ],
                })
            )
            path.chmod(0o600)
            with self.assertRaises(recovery.RecoveryError):
                recovery.load_platform_map(path)

    def test_inventory_drift_loader_maps_binding_back_to_entity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "inventory.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "entities": [
                    {
                        "entity_id": ENTITY,
                        "platform": "localtuya",
                        "device_id": "a" * 32,
                    }
                ],
                "identity_bindings": [
                    {
                        "device_id": "a" * 32,
                        "status": "ip_changed",
                    }
                ],
            }))
            path.chmod(0o600)
            self.assertEqual(
                recovery.load_ip_drift_map(path),
                {ENTITY: "ip_changed"},
            )


if __name__ == "__main__":
    unittest.main()
