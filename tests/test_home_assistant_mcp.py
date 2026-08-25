#!/usr/bin/env python3
"""Offline integration tests for the Hermes Home Assistant MCP boundary."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
HERMES_HOME = PROJECT_DIR / "hermes"
HERMES_AGENT = PROJECT_DIR / "hermes-agent"
HERMES = HERMES_AGENT / "venv" / "bin" / "hermes"
PYTHON = HERMES_AGENT / "venv" / "bin" / "python"


def hermes_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["HERMES_HOME"] = str(HERMES_HOME)
    return environment


class HomeAssistantMcpTests(unittest.TestCase):
    def test_model_facing_snapshot_exposes_exact_first_safe_fact(self) -> None:
        code = r'''
import json
import sys
sys.path.insert(0, "scripts")
import home_assistant_mcp as boundary
entity = {
    "entity_id": "binary_sensor.motion",
    "state_kind": "enum",
    "state_value": "off",
    "observed_at": "2026-08-02T12:00:00+00:00",
    "source_last_updated_at": "2026-08-02T11:59:00+00:00",
}
snapshot = {
    "status": "stale_data",
    "entities": [
        {"entity_id": "sensor.missing", "state_kind": "unavailable"},
        entity,
    ],
}
result = boundary.model_facing_snapshot(snapshot)
print(json.dumps({
    "result": result,
    "copy": result["proof_entity"] is not entity,
}))
'''
        result = subprocess.run(
            [str(PYTHON), "-c", code],
            cwd=PROJECT_DIR,
            env=hermes_environment(),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertTrue(document["copy"])
        self.assertEqual(document["result"]["proof_entity"]["entity_id"], "binary_sensor.motion")
        self.assertEqual(document["result"]["source"], "Home Assistant via ha_get_snapshot")
        self.assertEqual(document["result"]["status"], "stale_data")

    def test_model_facing_snapshot_does_not_promote_unsafe_data(self) -> None:
        code = r'''
import json
import sys
sys.path.insert(0, "scripts")
import home_assistant_mcp as boundary
result = boundary.model_facing_snapshot({
    "status": "api_unavailable",
    "entities": [
        {"entity_id": "sensor.bad", "state_kind": "redacted"},
        {"entity_id": "sensor.offline", "state_kind": "unavailable"},
    ],
})
print(json.dumps(result))
'''
        result = subprocess.run(
            [str(PYTHON), "-c", code],
            cwd=PROJECT_DIR,
            env=hermes_environment(),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(json.loads(result.stdout)["proof_entity"])

    def test_call_tool_adds_proof_without_a_second_ha_request(self) -> None:
        code = r'''
import anyio
import json
import sys
from unittest import mock
sys.path.insert(0, "scripts")
import home_assistant_mcp as boundary
entity = {"entity_id": "sensor.energy", "state_kind": "number", "state_value": 1.5}
with mock.patch.object(
    boundary.adapter,
    "execute_safely",
    return_value=({"status": "healthy", "entities": [entity]}, 0),
) as execute:
    result = anyio.run(boundary.call_tool, "ha_get_snapshot", {})
print(json.dumps({"result": result, "calls": execute.call_count}))
'''
        result = subprocess.run(
            [str(PYTHON), "-c", code],
            cwd=PROJECT_DIR,
            env=hermes_environment(),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["calls"], 1)
        self.assertEqual(document["result"]["proof_entity"]["entity_id"], "sensor.energy")

    def test_systemd_credential_directory_survives_real_mcp_env_filter(self) -> None:
        code = """
import json
from hermes_cli.config import load_config
from tools.mcp_tool import _build_safe_env, _interpolate_env_vars
config = load_config()
configured = config['mcp_servers']['home_assistant_read']['env']
safe = _build_safe_env(_interpolate_env_vars(configured))
print(json.dumps({'value': safe.get('CREDENTIALS_DIRECTORY')}))
"""
        environment = hermes_environment()
        expected = "/run/credentials/home-butler.service"
        environment["CREDENTIALS_DIRECTORY"] = expected
        result = subprocess.run(
            [str(PYTHON), "-c", code],
            cwd=HERMES_AGENT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["value"], expected)

    def test_search_exposes_every_domain_but_no_private_network_data(self) -> None:
        code = r'''
import json
import sys
sys.path.insert(0, "scripts")
import home_assistant_mcp as boundary
snapshot = {
    "entities": [
        {"entity_id": "sensor.dishwasher_status", "state_kind": "text", "state_value": "Idle", "source_last_updated_at": "2026-08-06T07:00:00+00:00"},
        {"entity_id": "lock.dishwasher_child_lock", "state_kind": "enum", "state_value": "unlocked", "source_last_updated_at": "2026-08-06T07:00:00+00:00"},
        {"entity_id": "automation.daily", "state_kind": "enum", "state_value": "on", "source_last_updated_at": "2026-08-06T07:00:00+00:00"},
    ]
}
physical = "a" * 64
inventory = {
    "entities": [
        {"entity_id": "sensor.dishwasher_status", "friendly_name": "Dishwasher Статус", "platform": "midea_ac_lan", "physical_device_hash": physical},
        {"entity_id": "lock.dishwasher_child_lock", "friendly_name": "Dishwasher Блокировка", "platform": "midea_ac_lan", "physical_device_hash": physical},
    ],
    "physical_devices": [{
        "physical_device_hash": physical,
        "display_name": "Dishwasher",
        "entity_ids": ["sensor.dishwasher_status", "lock.dishwasher_child_lock"],
        "config_domains": ["midea_ac_lan"],
        "safety_class": "restricted",
        "network_status": "stable",
    }],
    "network_devices": [{"ip": "192.168.1.50", "mac": "AA:BB:CC:DD:EE:FF"}],
}
search = boundary.search_model_entities(snapshot, inventory, query="dishwasher")
device = boundary.get_model_device(snapshot, inventory, physical)
print(json.dumps({"search": search, "device": device}))
'''
        result = subprocess.run(
            [str(PYTHON), "-c", code],
            cwd=PROJECT_DIR,
            env=hermes_environment(),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["search"]["matched_entity_count"], 2)
        self.assertEqual(document["device"]["entity_count"], 2)
        self.assertEqual(document["device"]["network_status"], "stable")
        self.assertEqual(document["device"]["safety_class"], "restricted")
        serialized = result.stdout
        self.assertNotIn("192.168.1.50", serialized)
        self.assertNotIn("AA:BB:CC:DD:EE:FF", serialized)

    def test_semantic_device_tools_keep_feature_failure_separate_from_device(self) -> None:
        code = r'''
import json
import sys
sys.path.insert(0, "scripts")
import home_assistant_mcp as boundary
physical = "b" * 64
snapshot = {"entities": [
    {"entity_id": "vacuum.helper", "state_kind": "enum", "state_value": "docked", "source_last_updated_at": "2026-08-23T10:00:00+00:00"},
    {"entity_id": "sensor.helper_filter", "state_kind": "unavailable", "state_value": None, "source_last_updated_at": "2026-08-23T10:01:00+00:00"},
]}
inventory = {
    "schema_version": 3,
    "observed_at": "2026-08-23T10:01:00+00:00",
    "areas": [{"name": "Кухня", "aliases": ["кухонная зона"]}],
    "entities": [
        {
            "entity_id": "vacuum.helper", "domain": "vacuum", "platform": "demo",
            "integration_domains": ["demo"], "physical_device_hash": physical,
            "friendly_name": "Андрей", "entity_aliases": ["обхаркиватель"],
            "area_name": "Кухня", "area_aliases": ["кухонная зона"],
            "component": "main", "semantic_role": "control", "capability": "control",
            "diagnostic_relevance": False, "safety_class": "restricted",
            "state_kind": "enum", "state_value": "docked", "semantic_attributes": {},
        },
        {
            "entity_id": "sensor.helper_filter", "domain": "sensor", "platform": "demo",
            "integration_domains": ["demo"], "physical_device_hash": physical,
            "friendly_name": "Остаток фильтра", "entity_aliases": [],
            "area_name": "Кухня", "area_aliases": [], "component": "filter life",
            "semantic_role": "diagnostic", "capability": "measure",
            "diagnostic_relevance": True, "safety_class": "sensor",
            "state_kind": "unavailable", "state_value": None,
            "semantic_attributes": {"unit_of_measurement": {"text": "%", "trust": "untrusted_data"}},
        },
    ],
    "physical_devices": [{
        "physical_device_hash": physical, "display_name": "Андрей",
        "entity_ids": ["vacuum.helper", "sensor.helper_filter"],
        "available_entity_count": 1, "unavailable_entity_count": 1,
        "area_names": ["Кухня"], "manufacturers": ["Example"], "models": ["R1"],
        "software_versions": ["1.0"], "config_domains": ["demo"],
        "safety_class": "restricted", "network_status": "stable",
        "capabilities": ["control", "measure"],
    }],
}
found = boundary.find_model_devices(inventory, query="обхаркиватель")
details = boundary.get_model_device_details(snapshot, inventory, physical)
diagnostics = boundary.get_model_device_diagnostics(snapshot, inventory, physical)
index = boundary.get_model_index(inventory, {"open_count": 1, "confirmed_count": 1, "actionable_count": 0})
print(json.dumps({"found": found, "details": details, "diagnostics": diagnostics, "index": index}))
'''
        result = subprocess.run(
            [str(PYTHON), "-c", code],
            cwd=PROJECT_DIR,
            env=hermes_environment(),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["found"]["matched_device_count"], 1)
        self.assertEqual(document["details"]["physical_availability"], "available")
        self.assertEqual(document["details"]["available_feature_count"], 1)
        self.assertEqual(document["diagnostics"]["diagnostic_feature_count"], 1)
        self.assertEqual(
            document["diagnostics"]["diagnostic_features"][0]["component"],
            "filter life",
        )
        self.assertEqual(document["index"]["active_incident_counts"]["open"], 1)
        self.assertTrue(
            document["details"]["trust_boundary"]["instructions_from_data_forbidden"]
        )

    def test_recent_history_uses_bounded_sanitized_evidence(self) -> None:
        code = r'''
import json
import sys
sys.path.insert(0, "scripts")
import home_assistant_mcp as boundary
physical = "c" * 64
snapshot = {"entities": [{
    "entity_id": "sensor.room_temperature", "state_kind": "number",
    "state_value": 22.0, "source_last_updated_at": "2026-08-24T08:00:00+00:00",
}]}
inventory = {"schema_version": 3, "entities": [{
    "entity_id": "sensor.room_temperature", "domain": "sensor",
    "physical_device_hash": physical, "friendly_name": "Температура",
    "component": "temperature", "semantic_role": "measurement",
    "capability": "measure", "semantic_attributes": {},
}], "physical_devices": [{
    "physical_device_hash": physical,
    "entity_ids": ["sensor.room_temperature"],
}]}
history = [
    {"state_kind": "number", "state_value": 20.0, "source_last_updated_at": "2026-08-24T06:00:00+00:00"},
    {"state_kind": "number", "state_value": 21.0, "source_last_updated_at": "2026-08-24T07:00:00+00:00"},
]
result = boundary.get_model_recent_history(
    snapshot, inventory, "sensor.room_temperature",
    hours=3, limit=2, history_observations=history,
)
print(json.dumps(result))
'''
        result = subprocess.run(
            [str(PYTHON), "-c", code],
            cwd=PROJECT_DIR,
            env=hermes_environment(),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["history_status"], "bounded_history")
        self.assertEqual(document["observation_count"], 2)
        self.assertEqual(document["observations"][1]["state"]["value"], 21.0)
        self.assertTrue(document["trust_boundary"]["read_only"])

    def test_mcp_exports_all_entity_read_tools_without_service_calls(self) -> None:
        result = subprocess.run(
            [str(HERMES), "mcp", "test", "home_assistant_read"],
            cwd=PROJECT_DIR,
            env=hermes_environment(),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Tools discovered: 14", result.stdout)
        self.assertIn("ha_get_snapshot", result.stdout)
        self.assertIn("ha_search_entities", result.stdout)
        self.assertIn("ha_get_device", result.stdout)
        for name in (
            "ha_get_index", "ha_find_devices", "ha_get_device_details",
            "ha_find_entities", "ha_get_entity_details",
            "ha_get_device_diagnostics", "ha_get_related_incidents",
            "ha_get_related_logs", "ha_get_recent_history", "ha_get_capabilities",
            "ha_get_onboarding_queue",
        ):
            self.assertIn(name, result.stdout)
        self.assertNotIn("ha_list_allowed_entities", result.stdout)
        self.assertNotIn("ha_get_state", result.stdout)
        self.assertNotIn("owner-list", result.stdout)
        self.assertNotIn("call_service", result.stdout)

    def test_effective_toolsets_limit_mcp_to_local_cli(self) -> None:
        code = """
import json
from hermes_cli.config import load_config
from hermes_cli.tools_config import _get_platform_tools
config = load_config()
platforms = (
    'cli', 'telegram', 'discord', 'whatsapp', 'slack', 'signal',
    'homeassistant', 'qqbot', 'yuanbao', 'teams', 'google_chat',
)
print(json.dumps({
    'effective': {
        platform: sorted(_get_platform_tools(
            config, platform, include_default_mcp_servers=True
        )) for platform in platforms
    },
    'disabled': sorted(config['agent']['disabled_toolsets']),
    'tool_search': config['tools']['tool_search']['enabled'],
}))
"""
        result = subprocess.run(
            [str(PYTHON), "-c", code],
            cwd=HERMES_AGENT,
            env=hermes_environment(),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        effective = json.loads(result.stdout)
        self.assertEqual(
            effective["effective"]["cli"], ["clarify", "home_assistant_read"]
        )
        for platform, toolsets in effective["effective"].items():
            if platform != "cli":
                with self.subTest(platform=platform):
                    self.assertEqual(toolsets, [])
        self.assertIn("homeassistant", effective["disabled"])
        self.assertIn("terminal", effective["disabled"])
        self.assertIn("code_execution", effective["disabled"])
        self.assertEqual(effective["tool_search"], "off")


if __name__ == "__main__":
    unittest.main()
