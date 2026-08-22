#!/usr/bin/env python3
"""Offline contracts for the read-only HA Container deployment preflight."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_DIR / "scripts" / "ha-container-upgrade-preflight.py"
SPEC = importlib.util.spec_from_file_location("ha_container_upgrade_preflight", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)

CONTAINER_ID = "a" * 64
SECRET = "SECRET_ENV_VALUE_MUST_NOT_ESCAPE"


def inspected(*, compose: bool) -> list[dict[str, object]]:
    labels: dict[str, str] = {"io.hass.type": "core"}
    if compose:
        labels |= {
            "com.docker.compose.project": "homeassistant",
            "com.docker.compose.service": "homeassistant",
            "com.docker.compose.project.working_dir": "/srv/homeassistant",
            "com.docker.compose.project.config_files": "compose.yaml",
        }
    return [
        {
            "Id": CONTAINER_ID,
            "Name": "/homeassistant",
            "Image": "sha256:" + "b" * 64,
            "Config": {
                "Image": "ghcr.io/home-assistant/home-assistant:2026.5.2",
                "Labels": labels,
                "Env": ["TOKEN=" + SECRET],
            },
            "HostConfig": {
                "NetworkMode": "host",
                "RestartPolicy": {"Name": "unless-stopped"},
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": "/srv/homeassistant/config",
                    "Destination": "/config",
                }
            ],
        }
    ]


def fake_text(arguments: list[str], limit: int = 512) -> str:
    del limit
    if "container" in arguments:
        return CONTAINER_ID
    if "version" in arguments:
        return "28.3.3"
    if "info" in arguments:
        return "x86_64"
    raise AssertionError(arguments)


class HaContainerUpgradePreflightTests(unittest.TestCase):
    def _collect(self, *, compose: bool) -> dict[str, object]:
        completed = subprocess.CompletedProcess([], 0, b"", b"")
        with (
            mock.patch.object(preflight.os, "geteuid", return_value=0),
            mock.patch.object(preflight, "_run_text", side_effect=fake_text),
            mock.patch.object(preflight, "_run_json", return_value=inspected(compose=compose)),
            mock.patch.object(preflight, "_safe_compose_file", return_value=True),
            mock.patch.object(preflight.subprocess, "run", return_value=completed),
            mock.patch.object(
                preflight.os,
                "statvfs",
                return_value=SimpleNamespace(f_bavail=1_000_000, f_frsize=4096),
            ),
        ):
            return preflight.collect()

    def test_compose_deployment_is_classified_without_exporting_environment(self) -> None:
        result = self._collect(compose=True)
        self.assertEqual(result["upgrade_method"], "docker_compose")
        self.assertEqual(result["network_mode"], "host")
        self.assertEqual(result["restart_policy"], "unless-stopped")
        self.assertEqual(
            result["compose"]["config_files"],
            ["/srv/homeassistant/compose.yaml"],
        )
        self.assertFalse(result["environment_exported"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(CONTAINER_ID, json.dumps(result))

    def test_non_compose_deployment_requires_reviewed_recreation(self) -> None:
        result = self._collect(compose=False)
        self.assertEqual(result["upgrade_method"], "manual_recreate_required")
        self.assertFalse(result["compose"]["detected"])

    def test_multiple_container_ids_fail_closed(self) -> None:
        with (
            mock.patch.object(preflight.os, "geteuid", return_value=0),
            mock.patch.object(
                preflight,
                "_run_text",
                return_value=CONTAINER_ID + "\n" + "b" * 64,
            ),
        ):
            with self.assertRaises(preflight.PreflightError):
                preflight.collect()


if __name__ == "__main__":
    unittest.main()
