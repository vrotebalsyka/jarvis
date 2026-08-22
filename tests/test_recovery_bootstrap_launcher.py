#!/usr/bin/env python3
"""Static safety contracts for the interactive victor bootstrap launcher."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT_DIR / "scripts" / "bootstrap-ha-recovery-victor.sh"


class RecoveryBootstrapLauncherTests(unittest.TestCase):
    def test_launcher_is_valid_bash_and_targets_only_victor_host(self) -> None:
        result = subprocess.run(
            ["/bin/bash", "-n", str(LAUNCHER)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        text = LAUNCHER.read_text()
        self.assertIn('readonly REMOTE_USER="victor"', text)
        self.assertIn('readonly REMOTE_HOST="192.168.1.127"', text)
        self.assertIn('readonly HOST_ALIAS="homebutler-recovery-target"', text)
        self.assertIn("StrictHostKeyChecking=yes", text)
        self.assertIn("GlobalKnownHostsFile=/dev/null", text)
        self.assertIn("BOOTSTRAP_LAUNCHER_CHECK_OK", text)
        self.assertIn("[--check]", text)
        self.assertNotIn('""":"', text)

    def test_launcher_never_handles_or_persists_a_password(self) -> None:
        text = LAUNCHER.read_text().lower()
        self.assertNotIn("sshpass", text)
        self.assertNotIn("expect ", text)
        self.assertNotIn("read -s", text)
        self.assertNotIn("password=", text)
        self.assertIn("sudo --", text)

    def test_launcher_transfers_only_the_four_reviewed_files(self) -> None:
        text = LAUNCHER.read_text()
        self.assertIn("bootstrap-ha-recovery-host.sh", text)
        self.assertIn("ha-recovery-host-command.sh", text)
        self.assertIn("ha-recovery-ssh-gate.sh", text)
        self.assertIn("ha-container-upgrade-preflight.py", text)
        self.assertIn("sha256sum", text)
        self.assertIn("homebutler-recovery-bootstrap", text)
        self.assertNotIn("scp -r", text)

    def test_launcher_stores_only_validated_private_preflight(self) -> None:
        text = LAUNCHER.read_text()
        self.assertIn("ha-host-upgrade-preflight.json", text)
        self.assertIn('"environment_exported"', text)
        self.assertIn('"read_only"', text)
        self.assertIn("install -o root -g root -m 0600", text)

    def test_launcher_does_not_require_nonexecuted_payloads_to_be_executable(self) -> None:
        text = LAUNCHER.read_text()
        self.assertNotIn('! -L "$path" && -x "$path"', text)
        self.assertIn("sudo -- /bin/bash", text)

    def test_launcher_repairs_existing_identity_and_proves_key_before_success(self) -> None:
        text = LAUNCHER.read_text()
        self.assertIn("remote_identity", text)
        self.assertIn("host_mode='--repair'", text)
        self.assertIn("IdentitiesOnly=yes", text)
        self.assertIn("ControlMaster=no", text)
        self.assertIn("status=healthy_no_action", text)
        self.assertLess(text.index("recovery_result="), text.index("Recovery bootstrap completed"))


if __name__ == "__main__":
    unittest.main()
