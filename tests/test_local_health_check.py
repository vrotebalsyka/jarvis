#!/usr/bin/env python3
"""Fixture tests for the collector's strict JSON filters."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "local-health-check.sh"


def run_filter(function: str, fixture: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", 'source "$1"; "$2"', "fixture-test", str(SCRIPT), function],
        input=json.dumps(fixture),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def run_home_assistant_filter(project: Path, adapter: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash", "-c",
            'source "$1"; collect_home_assistant_json "$2" "$3"',
            "ha-filter-test", str(SCRIPT), str(project), str(adapter),
        ],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


class CollectorFilterTests(unittest.TestCase):
    def test_collector_supports_unprivileged_system_gateway_service(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("/etc/systemd/system/home-butler.service", script)
        self.assertIn("gateway_command=(systemctl is-active home-butler.service)", script)

    def test_temperature_filter_ignores_non_temperature_inputs(self) -> None:
        fixture = {
            "chip": {
                "feature": {
                    "temp1_input": 42.5,
                    "fan1_input": 1200,
                    "power1_input": 88,
                    "in0_input": 12,
                }
            }
        }
        result = run_filter("parse_temperatures_json", fixture)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [{"chip": "chip", "sensor": "feature", "celsius": 42.5}],
        )

    def test_ollama_models_filter_requires_complete_typed_shape(self) -> None:
        valid = {
            "models": [
                {
                    "name": "home-butler:latest",
                    "size": 1,
                    "size_vram": 0,
                    "context_length": 8192,
                    "expires_at": "2026-08-02T00:00:00Z",
                }
            ]
        }
        result = run_filter("parse_ollama_models_json", valid)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)[0]["name"], "home-butler:latest")

        for malformed in ({}, {"models": None}, {"models": [{}]}):
            with self.subTest(malformed=malformed):
                result = run_filter("parse_ollama_models_json", malformed)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_home_assistant_filter_fails_closed_when_adapter_is_missing_or_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / "config").mkdir()
            (project / "secrets").mkdir()
            (project / "config" / "home-assistant.env").write_text("fixture")
            (project / "secrets" / "home-assistant.token").write_text("fixture")
            adapter = project / "home_assistant_read.py"

            for fixture in ("missing", "non-executable", "malformed", "oversized"):
                if adapter.exists():
                    adapter.unlink()
                if fixture == "non-executable":
                    adapter.write_text("#!/bin/sh\nexit 0\n")
                    adapter.chmod(0o600)
                elif fixture == "malformed":
                    adapter.write_text("#!/bin/sh\nprintf 'not-json\\n'\n")
                    adapter.chmod(0o700)
                elif fixture == "oversized":
                    adapter.write_text("#!/bin/sh\nhead -c 1048577 /dev/zero\n")
                    adapter.chmod(0o700)
                with self.subTest(fixture=fixture):
                    result = run_home_assistant_filter(project, adapter)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        json.loads(result.stdout),
                        {"configured": True, "status": "api_unavailable"},
                    )

    def test_home_assistant_filter_accepts_every_contract_status(self) -> None:
        statuses = (
            "not_configured", "dns_failure", "host_unreachable", "port_closed",
            "unauthorized", "api_unavailable", "stale_data", "healthy",
        )
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / "config").mkdir()
            (project / "secrets").mkdir()
            adapter = project / "home_assistant_read.py"
            for status in statuses:
                configured = status != "not_configured"
                payload = json.dumps({
                    "schema_version": 1,
                    "observed_at": "2026-08-02T00:00:00+00:00",
                    "configured": configured,
                    "status": status,
                })
                adapter.write_text(f"#!/bin/sh\nprintf '%s\\n' '{payload}'\n")
                adapter.chmod(0o700)
                with self.subTest(status=status):
                    result = run_home_assistant_filter(project, adapter)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        json.loads(result.stdout),
                        {"configured": configured, "status": status},
                    )

    def test_home_assistant_filter_prevents_configured_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / "config").mkdir()
            (project / "secrets").mkdir()
            (project / "config" / "home-assistant.env").write_text("fixture")
            (project / "secrets" / "home-assistant.token").write_text("fixture")
            adapter = project / "home_assistant_read.py"
            payload = json.dumps({
                "schema_version": 1,
                "observed_at": "2026-08-02T00:00:00+00:00",
                "configured": False,
                "status": "not_configured",
            })
            adapter.write_text(f"#!/bin/sh\nprintf '%s\\n' '{payload}'\n")
            adapter.chmod(0o700)
            result = run_home_assistant_filter(project, adapter)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                {"configured": True, "status": "api_unavailable"},
            )


if __name__ == "__main__":
    unittest.main()
