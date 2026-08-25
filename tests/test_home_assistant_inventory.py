#!/usr/bin/env python3
"""Offline contracts for the private HA/LAN identity inventory."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import home_assistant_inventory as inventory  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402


TOKEN = "SECRET_SENTINEL.DO_NOT_LEAK.abcdefghijklmnopqrstuvwxyz"
ENTRY_ID = "01KJF759CXHGZ0YJWQGT60M1R5"
DEVICE_ID = "c91bf12b42f25fac48fee74e5596a5b8"


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.replies = [
            {"type": "auth_required"},
            {"type": "auth_ok"},
            {
                "id": 10, "type": "result", "success": True,
                "result": {
                    "entities": [
                        {"ei": "switch.kukhnia", "pl": "localtuya", "di": DEVICE_ID, "en": "HOSTILE TITLE"},
                        {"ei": "sensor.temperature", "pl": "tuya", "di": None},
                    ]
                },
            },
            {
                "id": 11, "type": "result", "success": True,
                "result": [
                    {
                        "id": DEVICE_ID,
                        "config_entries": [ENTRY_ID],
                        "identifiers": [["localtuya", "local_SECRET_DEVICE_ID"]],
                        "connections": [["mac", "d8:d6:68:c8:27:84"]],
                        "name": "Кухонное реле",
                        "manufacturer": "Example Devices",
                        "model": "Relay 2",
                        "sw_version": "1.2.3",
                        "area_id": "kitchen",
                    },
                ],
            },
            {
                "id": 12, "type": "result", "success": True,
                "result": [
                    {
                        "entry_id": ENTRY_ID, "domain": "localtuya", "title": "owner@example.com",
                        "state": "loaded", "supports_reconfigure": False,
                        "supports_options": True, "supports_unload": True,
                    }
                ],
            },
            {
                "id": 13, "type": "result", "success": True,
                "result": {
                    "state": "idle",
                    "agent_errors": {},
                    "backups": [
                        {
                            "backup_id": "PRIVATE_BACKUP_ID",
                            "name": "PRIVATE BACKUP NAME",
                            "date": datetime.now(timezone.utc).isoformat(),
                            "homeassistant_version": "2026.5.2",
                            "homeassistant_included": True,
                            "database_included": True,
                            "failed_agent_ids": [],
                            "failed_addons": [],
                            "failed_folders": [],
                        }
                    ],
                },
            },
            {
                "id": 14, "type": "result", "success": True,
                "result": [
                    {"area_id": "kitchen", "name": "Кухня", "aliases": ["кухонная зона"]}
                ],
            },
            {
                "id": 15, "type": "result", "success": True,
                "result": [
                    {
                        "entity_id": "switch.kukhnia",
                        "aliases": ["главное реле"],
                        "original_name": "Power",
                        "translation_key": "power",
                        "area_id": None,
                    },
                    {
                        "entity_id": "sensor.temperature",
                        "aliases": [],
                        "original_name": "Temperature",
                        "translation_key": "temperature",
                        "area_id": None,
                    },
                ],
            },
        ]

    def recv(self) -> str:
        return json.dumps(self.replies.pop(0))

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def close(self) -> None:
        pass


class InventoryTests(unittest.TestCase):
    def test_official_mcp_probe_is_bounded_get_only_and_body_blind(self) -> None:
        class Response:
            def __init__(self, status: int) -> None:
                self.status = status
                self.read_amounts: list[int] = []

            def read(self, amount: int) -> bytes:
                self.read_amounts.append(amount)
                return b"SECRET_SENTINEL"[:amount]

        class Connection:
            def __init__(self, status: int) -> None:
                self.response = Response(status)
                self.requests: list[tuple[str, str, dict[str, str]]] = []
                self.closed = False

            def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
                self.requests.append((method, path, headers))

            def getresponse(self) -> Response:
                return self.response

            def close(self) -> None:
                self.closed = True

        config = ha_read.AdapterConfig(
            "http", "192.168.1.127", 8123, TOKEN, (), True
        )
        for status, expected in ((404, "unavailable"), (405, "available"), (503, "not_verified")):
            connection = Connection(status)
            with self.subTest(status=status):
                result = inventory._probe_official_mcp(
                    config, connection_factory=lambda _config, value=connection: value
                )
                self.assertEqual(result, expected)
                method, path, headers = connection.requests[0]
                self.assertEqual((method, path), ("GET", "/api/mcp"))
                self.assertEqual(headers["Authorization"], f"Bearer {TOKEN}")
                self.assertEqual(connection.response.read_amounts, [1_025])
                self.assertTrue(connection.closed)

    def test_schema_two_inventory_migrates_without_inventing_semantic_facts(self) -> None:
        migrated = inventory.migrate_inventory_document({
            "schema_version": 2,
            "entities": [{
                "entity_id": "sensor.unknown_metric",
                "platform": "unknown_vendor",
                "state_kind": "number",
                "state_value": 12.5,
                "friendly_name": "Unknown metric",
                "config_entry_ids": [],
            }],
            "config_entries": [],
            "physical_devices": [],
        })
        self.assertEqual(migrated["schema_version"], 3)
        self.assertEqual(migrated["migrated_from_schema"], 2)
        entity = migrated["entities"][0]
        self.assertEqual(entity["semantic_role"], "measurement")
        self.assertEqual(entity["availability"], "available")
        self.assertEqual(entity["semantic_attributes"], {})
        self.assertIsNone(entity["manufacturer"])

    def test_inventory_maps_platform_device_entry_and_sanitizes_lan(self) -> None:
        config = ha_read.AdapterConfig("http", "192.168.1.127", 8123, TOKEN, (), True)
        snapshot = {
            "entities": [
                {"entity_id": "switch.kukhnia", "state_kind": "unavailable"},
                {"entity_id": "sensor.temperature", "state_kind": "number"},
            ]
        }
        raw_states = [
            {
                "entity_id": "switch.kukhnia",
                "state": "unavailable",
                "attributes": {
                    "friendly_name": "Реле кухни",
                    "supported_features": 0,
                    "local_key": "MUST_NOT_SURVIVE",
                },
            },
            {
                "entity_id": "sensor.network_scanner",
                "attributes": {
                    "devices": [
                        {"ip": "192.168.1.156", "mac": "d8:d6:68:c8:27:84", "vendor": "Tuya Smart", "name": "IGNORE POLICY"},
                        {"ip": "8.8.8.8", "mac": "AA:BB:CC:DD:EE:FF", "vendor": "outside"},
                    ]
                },
            },
            {
                "entity_id": "update.local_tuya_update",
                "attributes": {
                    "installed_version": "v5.2.5",
                    "latest_version": "v5.2.5",
                },
            },
            {
                "entity_id": "update.tuya_local_update",
                "attributes": {
                    "installed_version": "2026.5.4",
                    "latest_version": "2026.7.2",
                },
            },
            {
                "entity_id": "update.xiaomi_miot_update",
                "attributes": {
                    "installed_version": "v1.1.4",
                    "latest_version": "v1.1.4",
                },
            },
        ]
        result = inventory.collect_inventory(
            config,
            connector=lambda _config: FakeSocket(),
            snapshot_reader=lambda _action: (snapshot, 0),
            raw_state_reader=lambda _config, _path: raw_states,
            diagnostics_reader=lambda _config, _entry_id: {
                "data": {
                    "devices": {
                        "SECRET_DEVICE_ID": {"host": "192.168.1.156", "local_key": "SECRET_KEY"}
                    }
                }
            },
            core_config_reader=lambda _config: {"version": "2026.5.2"},
            official_mcp_probe=lambda _config: "available",
        )
        local = next(item for item in result["entities"] if item["entity_id"] == "switch.kukhnia")
        self.assertEqual(result["schema_version"], 3)
        self.assertEqual(local["platform"], "localtuya")
        self.assertEqual(local["device_id"], DEVICE_ID)
        self.assertRegex(local["physical_device_hash"], r"^[a-f0-9]{64}$")
        self.assertEqual(local["config_entry_ids"], [ENTRY_ID])
        self.assertEqual(local["state_kind"], "unavailable")
        self.assertEqual(local["availability"], "unavailable")
        self.assertEqual(local["area_name"], "Кухня")
        self.assertEqual(local["area_aliases"], ["кухонная зона"])
        self.assertEqual(local["entity_aliases"], ["главное реле"])
        self.assertEqual(local["translation_key"], "power")
        self.assertEqual(local["semantic_role"], "control")
        self.assertEqual(local["capability"], "control")
        self.assertEqual(local["semantic_attributes"], {"supported_features": 0})
        self.assertEqual(
            result["network_devices"],
            [{"ip": "192.168.1.156", "mac": "D8:D6:68:C8:27:84", "vendor_kind": "tuya"}],
        )
        self.assertEqual(result["identity_binding_count"], 1)
        self.assertEqual(result["device_network_binding_count"], 1)
        self.assertEqual(result["ip_changed_count"], 0)
        capabilities = result["integration_capabilities"]
        self.assertEqual(
            capabilities["localtuya"]["ip_recovery_mode"],
            "stable_id_udp_auto_update",
        )
        self.assertEqual(
            capabilities["tuya_local"]["upgrade_status"],
            "core_upgrade_required",
        )
        self.assertFalse(
            capabilities["tuya_local"]["automatic_ip_recovery"]
        )
        self.assertTrue(
            capabilities["xiaomi_miot"]["bounded_config_entry_reload"]
        )
        self.assertFalse(
            capabilities["xiaomi_miot"]["automatic_recovery_enabled"]
        )
        self.assertEqual(
            result["backup_readiness"]["status"], "recent_complete_backup"
        )
        self.assertFalse(result["backup_readiness"]["restore_tested"])
        self.assertEqual(result["official_home_assistant"]["api_mcp"], "available")
        self.assertEqual(
            result["official_home_assistant"]["alias_matching"],
            "registry_aliases_available",
        )
        self.assertEqual(
            result["official_home_assistant"]["get_live_context"],
            "not_verified",
        )
        self.assertEqual(
            result["official_home_assistant"]["exposed_entity_policy"],
            "not_imported",
        )
        self.assertEqual(
            result["official_home_assistant"]["inventory_role"],
            "stable_identity_source_of_truth",
        )
        binding = result["identity_bindings"][0]
        self.assertEqual(binding["device_id"], DEVICE_ID)
        self.assertEqual(binding["configured_ip"], "192.168.1.156")
        self.assertEqual(binding["observed_ip"], "192.168.1.156")
        self.assertEqual(binding["mac"], "D8:D6:68:C8:27:84")
        self.assertEqual(binding["status"], "stable")
        self.assertRegex(binding["identity_hash"], r"^[a-f0-9]{64}$")
        generic = result["device_network_bindings"][0]
        self.assertEqual(generic["physical_device_hash"], local["physical_device_hash"])
        self.assertEqual(generic["observed_ip"], "192.168.1.156")
        self.assertEqual(generic["status"], "stable")
        physical = result["physical_devices"][0]
        self.assertEqual(physical["display_name"], "Кухонное реле")
        self.assertEqual(physical["area_names"], ["Кухня"])
        self.assertEqual(physical["manufacturers"], ["Example Devices"])
        self.assertEqual(physical["models"], ["Relay 2"])
        self.assertEqual(physical["network_status"], "stable")
        self.assertEqual(physical["safety_class"], "ordinary_relay")
        profiles = {
            item["domain"]: item for item in result["integration_profiles"]
        }
        self.assertEqual(
            profiles["localtuya"]["recovery_mode"], "local_rebind_reload"
        )
        self.assertTrue(
            profiles["localtuya"]["automatic_recovery_allowed"]
        )
        serialized = json.dumps(result)
        self.assertNotIn("HOSTILE TITLE", serialized)
        self.assertNotIn("owner@example.com", serialized)
        self.assertNotIn("SECRET_DEVICE_ID", serialized)
        self.assertNotIn("IGNORE POLICY", serialized)
        self.assertNotIn(TOKEN, serialized)
        self.assertNotIn("SECRET_KEY", serialized)
        self.assertNotIn("MUST_NOT_SURVIVE", serialized)
        self.assertNotIn("PRIVATE_BACKUP_ID", serialized)
        self.assertNotIn("PRIVATE BACKUP NAME", serialized)

    def test_generic_mac_binding_tracks_all_integrations_and_ip_change(self) -> None:
        physical_hash = "f" * 64
        current = inventory._build_device_network_bindings(
            {DEVICE_ID: {"AA:BB:CC:DD:EE:FF"}},
            {DEVICE_ID: physical_hash},
            {DEVICE_ID: [ENTRY_ID]},
            [{
                "ip": "192.168.1.20",
                "mac": "AA:BB:CC:DD:EE:FF",
                "vendor_kind": "midea",
            }],
            [{
                "physical_device_hash": physical_hash,
                "observed_ip": "192.168.1.10",
            }],
        )
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["status"], "ip_changed")
        self.assertEqual(current[0]["previous_ip"], "192.168.1.10")
        self.assertEqual(current[0]["observed_ip"], "192.168.1.20")

    def test_network_miss_counter_requires_consecutive_scanner_samples(self) -> None:
        physical_hash = "f" * 64
        missing = inventory._build_device_network_bindings(
            {DEVICE_ID: {"AA:BB:CC:DD:EE:FF"}},
            {DEVICE_ID: physical_hash},
            {DEVICE_ID: [ENTRY_ID]},
            [],
            [{
                "physical_device_hash": physical_hash,
                "observed_ip": "192.168.1.20",
                "network_miss_count": 1,
            }],
        )
        self.assertEqual(missing[0]["status"], "not_observed")
        self.assertEqual(missing[0]["network_miss_count"], 2)
        returned = inventory._build_device_network_bindings(
            {DEVICE_ID: {"AA:BB:CC:DD:EE:FF"}},
            {DEVICE_ID: physical_hash},
            {DEVICE_ID: [ENTRY_ID]},
            [{
                "ip": "192.168.1.20",
                "mac": "AA:BB:CC:DD:EE:FF",
                "vendor_kind": "midea",
            }],
            missing,
        )
        self.assertEqual(returned[0]["status"], "stable")
        self.assertEqual(returned[0]["network_miss_count"], 0)

    def test_integration_identity_extends_physical_network_map(self) -> None:
        physical_hash = "f" * 64
        merged = inventory._merge_identity_network_bindings(
            [],
            [{
                "device_id": DEVICE_ID,
                "config_entry_id": ENTRY_ID,
                "mac": "AA:BB:CC:DD:EE:FF",
                "configured_ip": "192.168.1.10",
                "observed_ip": "192.168.1.20",
                "status": "ip_changed",
            }],
            {DEVICE_ID: physical_hash},
            {DEVICE_ID: [ENTRY_ID]},
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["physical_device_hash"], physical_hash)
        self.assertEqual(merged[0]["observed_ip"], "192.168.1.20")
        self.assertEqual(merged[0]["previous_ip"], "192.168.1.10")
        self.assertEqual(merged[0]["status"], "ip_changed")

    def test_every_integration_gets_fail_closed_recovery_profile(self) -> None:
        profiles = inventory._integration_profiles(
            {
                ENTRY_ID: {
                    "domain": "midea_ac_lan",
                    "state": "loaded",
                    "supports_unload": True,
                }
            },
            {"midea_ac_lan", "unknown_vendor"},
        )
        by_domain = {item["domain"]: item for item in profiles}
        self.assertEqual(
            by_domain["midea_ac_lan"]["recovery_mode"], "idle_entry_reload"
        )
        self.assertTrue(
            by_domain["midea_ac_lan"]["automatic_recovery_allowed"]
        )
        self.assertEqual(
            by_domain["unknown_vendor"]["recovery_mode"], "diagnose_only"
        )
        self.assertFalse(
            by_domain["unknown_vendor"]["automatic_recovery_allowed"]
        )

        failed_profiles = inventory._integration_profiles(
            {
                ENTRY_ID: {
                    "domain": "midea_ac_lan",
                    "state": "setup_retry",
                    "supports_unload": True,
                }
            },
            {"midea_ac_lan"},
        )
        self.assertTrue(failed_profiles[0]["automatic_recovery_allowed"])

    def test_cloud_and_local_tuya_entries_share_one_physical_identity(self) -> None:
        identifier = "stable_device_123456"
        cloud = inventory._physical_device_hash(
            "a" * 32, [["tuya", identifier]]
        )
        local = inventory._physical_device_hash(
            "b" * 32, [["tuya_local", identifier]]
        )
        legacy = inventory._physical_device_hash(
            "c" * 32, [["localtuya", f"local_{identifier}"]]
        )
        self.assertEqual(cloud, local)
        self.assertEqual(local, legacy)
        self.assertNotIn(identifier, cloud)

    def test_config_entry_fallback_refuses_ambiguous_devices(self) -> None:
        identity_hash = inventory._identity_hash("localtuya", "stable_device_123")
        completed = inventory._complete_identity_devices(
            {identity_hash: (ENTRY_ID, "192.168.1.156")},
            {},
            [
                {
                    "platform": "localtuya",
                    "device_id": DEVICE_ID,
                    "config_entry_ids": [ENTRY_ID],
                },
                {
                    "platform": "localtuya",
                    "device_id": "a" * 32,
                    "config_entry_ids": [ENTRY_ID],
                },
            ],
        )
        self.assertEqual(completed, {})

    def test_tuya_local_preflight_requires_backup_after_core_upgrade(self) -> None:
        states = [
            {
                "entity_id": "update.tuya_local_update",
                "attributes": {
                    "installed_version": "2026.5.4",
                    "latest_version": "2026.7.2",
                },
            }
        ]
        result = inventory._integration_capabilities(
            states, {"version": "2026.7.4"}
        )
        self.assertEqual(
            result["tuya_local"]["upgrade_status"],
            "backup_required_before_update",
        )

    def test_tuya_local_preflight_recognizes_installed_ip_repair(self) -> None:
        states = [
            {
                "entity_id": "update.tuya_local_update",
                "attributes": {
                    "installed_version": "2026.7.2",
                    "latest_version": "2026.7.2",
                },
            }
        ]
        result = inventory._integration_capabilities(
            states, {"version": "2026.7.4"}
        )
        self.assertTrue(result["tuya_local"]["automatic_ip_recovery"])
        self.assertEqual(
            result["tuya_local"]["upgrade_status"],
            "automatic_ip_recovery_available",
        )

    def test_unreviewed_versions_fail_closed(self) -> None:
        states = [
            {
                "entity_id": "update.local_tuya_update",
                "attributes": {
                    "installed_version": "v9.9.9",
                    "latest_version": "v9.9.9",
                },
            },
            {
                "entity_id": "update.tuya_local_update",
                "attributes": {
                    "installed_version": "2026.5.4",
                    "latest_version": "2026.8.0",
                },
            },
            {
                "entity_id": "update.xiaomi_miot_update",
                "attributes": {
                    "installed_version": "9.9.9",
                    "latest_version": "9.9.9",
                },
            },
        ]
        result = inventory._integration_capabilities(
            states, {"version": "2026.7.4"}
        )
        self.assertEqual(
            result["localtuya"]["ip_recovery_mode"], "review_required"
        )
        self.assertEqual(
            result["tuya_local"]["upgrade_status"], "review_required"
        )
        self.assertEqual(
            result["xiaomi_miot"]["review_status"], "review_required"
        )
        self.assertFalse(
            result["xiaomi_miot"]["bounded_config_entry_reload"]
        )
        self.assertFalse(
            result["xiaomi_miot"]["automatic_recovery_enabled"]
        )

    def test_backup_readiness_requires_complete_current_core_backup(self) -> None:
        now = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
        result = inventory._backup_readiness(
            {
                "state": "idle",
                "agent_errors": {},
                "backups": [
                    {
                        "backup_id": "SECRET",
                        "date": (now - timedelta(hours=2)).isoformat(),
                        "homeassistant_version": "2026.5.2",
                        "homeassistant_included": True,
                        "database_included": True,
                        "failed_agent_ids": [],
                        "failed_addons": [],
                        "failed_folders": [],
                    }
                ],
            },
            "2026.5.2",
            now=now,
        )
        self.assertEqual(result["status"], "recent_complete_backup")
        self.assertEqual(result["age_seconds"], 7200)
        self.assertNotIn("backup_id", result)

    def test_backup_readiness_rejects_agent_errors_and_partial_backups(self) -> None:
        now = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
        agent_error = inventory._backup_readiness(
            {"state": "idle", "agent_errors": {"private": "failed"}, "backups": []},
            "2026.5.2",
            now=now,
        )
        partial = inventory._backup_readiness(
            {
                "state": "idle",
                "agent_errors": {},
                "backups": [
                    {
                        "date": (now - timedelta(minutes=5)).isoformat(),
                        "homeassistant_version": "2026.5.2",
                        "homeassistant_included": True,
                        "database_included": False,
                        "failed_agent_ids": [],
                        "failed_addons": [],
                        "failed_folders": [],
                    }
                ],
            },
            "2026.5.2",
            now=now,
        )
        self.assertEqual(agent_error["status"], "not_ready")
        self.assertEqual(partial["status"], "missing_complete_backup")

    def test_localtuya_registry_identifier_is_normalized(self) -> None:
        self.assertEqual(
            inventory._registry_identity_hash("localtuya", "local_stable_device_123"),
            inventory._identity_hash("localtuya", "stable_device_123"),
        )

    def test_previous_mac_proves_same_device_moved_to_new_ip(self) -> None:
        identity_hash = inventory._identity_hash("localtuya", "stable_device_123")
        bindings = inventory._build_identity_bindings(
            {identity_hash: (ENTRY_ID, "192.168.1.156")},
            {identity_hash: DEVICE_ID},
            [
                {
                    "ip": "192.168.1.199",
                    "mac": "D8:D6:68:C8:27:84",
                    "vendor_kind": "tuya",
                }
            ],
            [
                {
                    "identity_hash": identity_hash,
                    "mac": "D8:D6:68:C8:27:84",
                }
            ],
        )
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]["configured_ip"], "192.168.1.156")
        self.assertEqual(bindings[0]["observed_ip"], "192.168.1.199")
        self.assertEqual(bindings[0]["status"], "ip_changed")

    def test_xiaomi_registry_mac_tracks_drift_until_entities_recover(self) -> None:
        raw_identifier = "D8:D6:68:C8:27:84-roborock.vacuum.a15"
        parsed = inventory._xiaomi_identity(raw_identifier)
        self.assertIsNotNone(parsed)
        identity_hash, mac = parsed
        identities = {
            identity_hash: {
                "device_id": DEVICE_ID,
                "config_entry_id": ENTRY_ID,
                "mac": mac,
            }
        }
        unavailable = [{
            "platform": "xiaomi_miot",
            "device_id": DEVICE_ID,
            "state_kind": "unavailable",
        }]
        first = inventory._build_xiaomi_bindings(
            identities,
            unavailable,
            [{"ip": "192.168.1.10", "mac": mac, "vendor_kind": "xiaomi"}],
            [],
        )
        self.assertEqual(first[0]["status"], "stable")
        moved = inventory._build_xiaomi_bindings(
            identities,
            unavailable,
            [{"ip": "192.168.1.20", "mac": mac, "vendor_kind": "xiaomi"}],
            first,
        )
        self.assertEqual(moved[0]["configured_ip"], "192.168.1.10")
        self.assertEqual(moved[0]["observed_ip"], "192.168.1.20")
        self.assertEqual(moved[0]["status"], "ip_changed")
        recovered = inventory._build_xiaomi_bindings(
            identities,
            [{
                "platform": "xiaomi_miot",
                "device_id": DEVICE_ID,
                "state_kind": "number",
            }],
            [{"ip": "192.168.1.20", "mac": mac, "vendor_kind": "xiaomi"}],
            moved,
        )
        self.assertEqual(recovered[0]["configured_ip"], "192.168.1.20")
        self.assertEqual(recovered[0]["status"], "stable")
        self.assertNotIn(raw_identifier, json.dumps(recovered))

    def test_xiaomi_registry_identity_parser_fails_closed(self) -> None:
        self.assertIsNone(inventory._xiaomi_identity("host-token-secret"))
        self.assertIsNone(
            inventory._xiaomi_identity("model-192.168.1.10")
        )

    def test_atomic_inventory_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "state"
            directory.mkdir(mode=0o700)
            target = directory / inventory.INVENTORY_NAME
            inventory._atomic_write(target, b"{}\n")
            self.assertEqual(target.read_bytes(), b"{}\n")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.stat().st_uid, os.geteuid())


if __name__ == "__main__":
    unittest.main()
