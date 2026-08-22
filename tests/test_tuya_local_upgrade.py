#!/usr/bin/env python3
"""Safety and behavior tests for the fixed Tuya Local upgrade."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = PROJECT_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import tuya_local_upgrade as upgrade  # noqa: E402


class TuyaLocalUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = mock.Mock()

    @staticmethod
    def state(installed: str, *, in_progress: bool = False) -> dict[str, object]:
        return {
            "entity_id": "update.tuya_local_update",
            "state": "off",
            "attributes": {
                "installed_version": installed,
                "latest_version": "2026.7.2",
                "in_progress": in_progress,
            },
        }

    def test_install_uses_only_fixed_entity_version_and_service(self) -> None:
        responses = [
            {"version": "2026.7.4"},
            self.state("2026.5.4"),
            [],
            self.state("2026.7.2"),
        ]
        calls: list[tuple[str, str, bytes | None]] = []

        def request(_config, method, path, *, body=None):
            calls.append((method, path, body))
            return responses.pop(0)

        upgrade.install_exact_update(self.config, request=request)
        self.assertEqual(calls[2][0:2], ("POST", "/api/services/update/install"))
        self.assertEqual(
            json.loads(calls[2][2].decode("ascii")),
            {
                "backup": False,
                "entity_id": "update.tuya_local_update",
                "version": "2026.7.2",
            },
        )

    def test_install_rejects_unreviewed_start_version(self) -> None:
        responses = [{"version": "2026.7.4"}, self.state("2026.6.0")]

        def request(_config, _method, _path, *, body=None):
            return responses.pop(0)

        with self.assertRaises(upgrade.TuyaLocalUpgradeError):
            upgrade.install_exact_update(self.config, request=request)

    def test_python_requires_owner_maintenance_wrapper(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(upgrade.TuyaLocalUpgradeError):
                upgrade.run()

    def test_shell_wrapper_is_valid_and_bounded(self) -> None:
        path = SCRIPT_DIR / "upgrade-tuya-local.sh"
        result = subprocess.run(
            ["/bin/bash", "-n", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        text = path.read_text()
        self.assertIn("recent_complete_backup", text)
        self.assertIn("status=healthy_no_action", text)
        self.assertIn("home-butler-out-of-band-recovery.timer", text)
        self.assertIn("automatic_ip_recovery", text)
        self.assertIn("seq 1 24", text)
        self.assertIn("postcheck_passed", text)
        self.assertNotIn("ha_call_service", text)
        self.assertNotIn("sshpass", text)


if __name__ == "__main__":
    unittest.main()
