#!/usr/bin/env python3
"""Contracts for the bounded Ubuntu-to-Windows scheduler wake bridge."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import windows_wake_sync as wake  # noqa: E402


NOW = 1_787_590_000
DESIRED = NOW + 3600


def completed(document: dict[str, object], *, returncode: int = 0):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode,
        stdout=json.dumps(document, separators=(",", ":")) + "\n", stderr="",
    )


class WindowsWakeSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name) / "wake.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_sync_accepts_only_verified_fixed_task_evidence(self) -> None:
        calls: list[list[str]] = []

        def runner(arguments, **kwargs):
            calls.append(list(arguments))
            self.assertNotIn("payload", " ".join(arguments))
            return completed({
                "schema_version": 1,
                "status": "synced",
                "wake_epoch": DESIRED,
                "task": wake.WAKE_TASK_NAME,
            })

        original = wake.CMD_EXE
        wake.CMD_EXE = Path("/bin/true")
        try:
            result = wake.sync_status(
                {"wake_epoch": DESIRED}, now=NOW,
                state_path=self.state, runner=runner,
                helper_resolver=lambda **kwargs: Path("/bin/true"),
            )
        finally:
            wake.CMD_EXE = original
        self.assertEqual(result["status"], "synced")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_unchanged_epoch_is_cached_without_windows_process(self) -> None:
        self.state.write_text(json.dumps({
            "schema_version": 1,
            "wake_epoch": DESIRED,
            "verified_epoch": NOW - 10,
            "task": wake.WAKE_TASK_NAME,
        }), encoding="ascii")

        def forbidden(*args, **kwargs):
            raise AssertionError("cached sync must not start Windows")

        result = wake.sync_status(
            {"wake_epoch": DESIRED}, now=NOW,
            state_path=self.state, runner=forbidden,
        )
        self.assertEqual(result["status"], "cached")

    def test_invalid_or_due_epoch_never_reaches_windows(self) -> None:
        def forbidden(*args, **kwargs):
            raise AssertionError("invalid epoch must not start Windows")

        for value in (None, True, "123", NOW, NOW + 10, NOW + wake.MAXIMUM_LEAD_SECONDS + 1):
            with self.subTest(value=value):
                result = wake.sync_status(
                    {"wake_epoch": value}, now=NOW,
                    state_path=self.state, runner=forbidden,
                )
                self.assertEqual(result["status"], "not_scheduled")

    def test_wrong_task_or_epoch_fails_closed(self) -> None:
        original = wake.CMD_EXE
        wake.CMD_EXE = Path("/bin/true")
        try:
            for document in (
                {"schema_version": 1, "status": "synced", "wake_epoch": DESIRED, "task": "Other"},
                {"schema_version": 1, "status": "synced", "wake_epoch": DESIRED + 1, "task": wake.WAKE_TASK_NAME},
                {"schema_version": 1, "status": "unavailable", "wake_epoch": DESIRED, "task": wake.WAKE_TASK_NAME},
            ):
                with self.subTest(document=document):
                    result = wake.sync_status(
                        {"wake_epoch": DESIRED}, now=NOW,
                        state_path=self.state,
                        runner=lambda *args, document=document, **kwargs: completed(document),
                        helper_resolver=lambda **kwargs: Path("/bin/true"),
                    )
                    self.assertEqual(result["status"], "unavailable")
        finally:
            wake.CMD_EXE = original

    def test_csharp_adapter_has_no_arbitrary_task_or_action_arguments(self) -> None:
        source = (PROJECT_DIR / "scripts" / "windows-wake-sync.cs").read_text(
            encoding="utf-8"
        )
        self.assertIn('const string WakeTaskName = "Home Butler Scheduler Wake"', source)
        self.assertIn('const string RuntimeTaskName = "Home Butler WSL Runtime"', source)
        self.assertIn("args.Length != 1", source)
        self.assertIn("WakeToRun = true", source)
        self.assertNotIn("powershell", source.casefold())
        self.assertNotIn("Home Assistant", source)

    def test_windows_local_appdata_is_resolved_without_a_shell_payload(self) -> None:
        original = wake.CMD_EXE
        wake.CMD_EXE = Path("/bin/true")
        calls: list[list[str]] = []

        def runner(arguments, **kwargs):
            calls.append(list(arguments))
            return subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="C:\\Users\\Owner\\AppData\\Local\r\n".encode("utf-16-le"),
                stderr=b"",
            )

        try:
            path = wake._resolve_helper(runner=runner)
        finally:
            wake.CMD_EXE = original
        self.assertEqual(
            path,
            Path("/mnt/c/Users/Owner/AppData/Local/HomeButler/HomeButlerWakeSync.exe"),
        )
        self.assertEqual(calls[0][-1], "echo %LOCALAPPDATA%")


if __name__ == "__main__":
    unittest.main()
