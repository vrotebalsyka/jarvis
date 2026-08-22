#!/usr/bin/env python3
"""Static safety contract for the fixed Compose Core upgrade workflow."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
HOST = PROJECT_DIR / "scripts" / "ha-core-upgrade-host.sh"
LAUNCHER = PROJECT_DIR / "scripts" / "upgrade-ha-core-victor.sh"


class HaCoreUpgradeTests(unittest.TestCase):
    def test_both_scripts_are_valid_bash_and_versions_are_fixed(self) -> None:
        for path in (HOST, LAUNCHER):
            result = subprocess.run(
                ["/bin/bash", "-n", str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        text = HOST.read_text()
        self.assertIn('EXPECTED_CURRENT_VERSION="2026.5.2"', text)
        self.assertIn('TARGET_VERSION="2026.7.4"', text)
        self.assertIn('TARGET_IMAGE="ghcr.io/home-assistant/home-assistant:${TARGET_VERSION}"', text)
        self.assertNotIn("latest", text.lower())

    def test_host_upgrade_has_maintenance_lock_health_gate_and_rollback(self) -> None:
        text = HOST.read_text()
        self.assertIn("/run/homebutler-ha-maintenance.lock", text)
        self.assertIn("flock -n", text)
        self.assertIn("--pull never", text)
        self.assertIn("--no-deps", text)
        self.assertIn("--force-recreate", text)
        self.assertIn("rollback_to_old", text)
        self.assertIn("upgrade_failed_rolled_back", text)
        self.assertIn("upgrade_failed_rollback_failed", text)
        self.assertIn("http://127.0.0.1:8123/", text)
        self.assertIn("seq 1 120", text)
        self.assertIn("python -m homeassistant --version", text)
        self.assertNotIn("docker system prune", text)
        self.assertNotIn("/var/run/docker.sock", text)

    def test_launcher_requires_fresh_complete_backup_and_recovery_proof(self) -> None:
        text = LAUNCHER.read_text()
        self.assertIn("recent_complete_backup", text)
        self.assertIn('backup.get("core_version") != "2026.5.2"', text)
        self.assertIn('backup["age_seconds"] <= 3600', text)
        self.assertIn("restore_tested", text)
        self.assertIn("status=healthy_no_action", text)
        self.assertIn("home-butler-out-of-band-recovery.timer", text)
        self.assertIn("StrictHostKeyChecking=yes", text)
        self.assertIn("GlobalKnownHostsFile=/dev/null", text)
        self.assertNotIn("sshpass", text)
        self.assertNotIn("password=", text.lower())
        self.assertIn("home_assistant_read.py", text)
        self.assertIn("backup_required_before_update", text)
        self.assertGreaterEqual(text.count("status=healthy_no_action"), 2)

    def test_launcher_transfers_only_reviewed_files_and_supports_fixed_rollback(self) -> None:
        text = LAUNCHER.read_text()
        self.assertIn("ha-core-upgrade-host.sh ha-container-upgrade-preflight.py", text)
        self.assertIn("sha256sum", text)
        self.assertIn("[--check|--verify|--rollback]", text)
        self.assertIn('MODE="--verify"', text)
        self.assertIn("Core уже работает на версии 2026.7.4", text)
        self.assertIn("status=rollback_completed core=2026.5.2", text)
        self.assertNotIn("scp -r", text)


if __name__ == "__main__":
    unittest.main()
