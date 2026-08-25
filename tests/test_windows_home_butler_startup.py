#!/usr/bin/env python3
"""Static safety contract for Windows-to-WSL Home Butler startup."""

from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
INSTALLER = (
    PROJECT_DIR / "scripts" / "install-windows-home-butler-tasks.ps1"
).read_text(encoding="utf-8")


class WindowsHomeButlerStartupTests(unittest.TestCase):
    def test_keeps_only_the_named_wsl_runtime_alive_as_service_user(self) -> None:
        self.assertIn("Home Butler WSL Runtime", INSTALLER)
        self.assertIn("$conhostExe = Join-Path $env:SystemRoot 'System32\\conhost.exe'", INSTALLER)
        self.assertIn('--headless `"$wslExe`" -d $distroName -u $serviceUser --exec /usr/bin/sleep infinity', INSTALLER)
        self.assertIn("$distroName = 'Ubuntu'", INSTALLER)
        self.assertIn("$serviceUser = 'homebutler'", INSTALLER)
        self.assertNotIn("-u root", INSTALLER)

    def test_tasks_run_only_after_owner_logon_with_limited_privilege(self) -> None:
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn -User $identity", INSTALLER)
        self.assertIn("-LogonType Interactive", INSTALLER)
        self.assertIn("-RunLevel Limited", INSTALLER)
        self.assertNotIn("-RunLevel Highest", INSTALLER)
        self.assertNotIn("LogonType ServiceAccount", INSTALLER)

    def test_runtime_is_restarted_before_report_and_can_wake_windows(self) -> None:
        self.assertIn("persistent_scheduler.py", INSTALLER)
        self.assertIn("--wake-json", INSTALLER)
        self.assertIn("wake_epoch", INSTALLER)
        self.assertIn("windows-wake-sync.cs", INSTALLER)
        self.assertIn("HomeButlerWakeSync.exe", INSTALLER)
        self.assertIn("Home Butler Scheduler Wake", INSTALLER)
        self.assertIn("Home Butler Scheduler Wake Sync", INSTALLER)
        self.assertIn("/opt/home-butler/scripts/windows_wake_sync.py", INSTALLER)
        self.assertIn("-RepetitionInterval (New-TimeSpan -Minutes 5)", INSTALLER)
        self.assertIn("failed readback verification", INSTALLER)
        self.assertNotIn("AddHours(12)", INSTALLER)
        self.assertIn("-WakeToRun", INSTALLER)
        self.assertIn("-Trigger $runtimeTriggers", INSTALLER)
        self.assertNotIn("New-ScheduledTaskTrigger -Once", INSTALLER)

    def test_restart_policy_is_bounded_and_startup_is_headless(self) -> None:
        self.assertIn("-RestartCount 5", INSTALLER)
        self.assertIn("-DontStopOnIdleEnd", INSTALLER)
        self.assertIn("-MultipleInstances IgnoreNew", INSTALLER)
        self.assertIn("-Hidden", INSTALLER)
        self.assertIn("--headless", INSTALLER)
        self.assertIn("Home Butler Ollama GPU", INSTALLER)
        self.assertNotIn("home-butler-ollama-supervisor.ps1", INSTALLER)
        self.assertIn("/opt/home-butler/scripts/windows_gpu_supervisor.py", INSTALLER)
        self.assertNotIn("powershell.exe", INSTALLER.casefold())
        self.assertNotIn("0.0.0.0", INSTALLER)
        self.assertNotIn("HA_TOKEN", INSTALLER)
        self.assertIn("Register-BoundedHomeButlerTask", INSTALLER)
        self.assertIn("Stop-ScheduledTask -TaskName $Name -ErrorAction Stop", INSTALLER)
        self.assertIn("Register-ScheduledTask `", INSTALLER)
        self.assertIn("-ErrorAction Stop | Out-Null", INSTALLER)

    def test_removes_legacy_powershell_watchdog_task(self) -> None:
        self.assertIn("Home Butler Watchdog", INSTALLER)
        self.assertIn("Unregister-ScheduledTask", INSTALLER)
        self.assertNotIn("home-butler-watchdog.ps1", INSTALLER)
        self.assertIn("-RunLevel Limited", INSTALLER)

    def test_creates_simple_loopback_chat_shortcut(self) -> None:
        self.assertIn("Домашний дворецкий.url", INSTALLER)
        self.assertIn("URL=http://127.0.0.1:8780/", INSTALLER)


if __name__ == "__main__":
    unittest.main()
