#!/usr/bin/env python3
"""Static safety contract for the Windows Home Butler watchdog."""

from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
WATCHDOG = (PROJECT_DIR / "scripts" / "home-butler-watchdog.ps1").read_text(
    encoding="utf-8"
)


class WindowsHomeButlerWatchdogTests(unittest.TestCase):
    def test_only_starts_fixed_home_butler_units(self) -> None:
        self.assertIn("$requiredUnits = @(", WATCHDOG)
        self.assertIn("'/usr/bin/systemctl', 'start', '--', $unit", WATCHDOG)
        self.assertIn("is-active", WATCHDOG)
        self.assertNotIn("homeassistant.restart", WATCHDOG)
        self.assertNotIn("ha_call_service", WATCHDOG)
        self.assertNotIn("switch.turn_", WATCHDOG)
        self.assertIn("alice_tailscale_funnel.py', '--public-probe'", WATCHDOG)
        self.assertIn("'restart', '--',", WATCHDOG)

    def test_runs_wsl_checks_without_shell_interpolation(self) -> None:
        self.assertIn("& $wslExe @Arguments", WATCHDOG)
        self.assertIn("return $exitCode", WATCHDOG)
        self.assertNotIn("bash -c", WATCHDOG)
        self.assertNotIn("Invoke-Expression", WATCHDOG)
        self.assertNotIn("Start-Process -FilePath $wslExe", WATCHDOG)

    def test_status_is_local_and_secret_free(self) -> None:
        self.assertIn("watchdog-status.json", WATCHDOG)
        self.assertIn("startup-proof-history.json", WATCHDOG)
        self.assertIn("startup_self_check.py', '--check-status'", WATCHDOG)
        self.assertIn("dialogue_qualification.py', '--check-status'", WATCHDOG)
        self.assertIn("home-butler-dialogue-qualification.timer", WATCHDOG)
        self.assertIn("alice_skill_health.py', '--check-status'", WATCHDOG)
        self.assertIn("home-butler-alice-health.timer", WATCHDOG)
        self.assertIn("alice_webhook_ready", WATCHDOG)
        self.assertIn("dialogue_qualification_ready", WATCHDOG)
        self.assertIn("schema_version = 3", WATCHDOG)
        self.assertIn("verified_reboot_count", WATCHDOG)
        self.assertIn("LastBootUpTime", WATCHDOG)
        self.assertIn("/proc/sys/kernel/random/boot_id", WATCHDOG)
        self.assertIn("ConvertTo-Json", WATCHDOG)
        self.assertNotIn("TOKEN", WATCHDOG.upper())
        self.assertNotIn("PASSWORD", WATCHDOG.upper())


if __name__ == "__main__":
    unittest.main()
