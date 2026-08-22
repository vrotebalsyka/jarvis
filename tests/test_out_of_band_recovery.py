#!/usr/bin/env python3
"""Offline safety contracts for independent HA Container recovery."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import incident_monitor as monitor  # noqa: E402
import out_of_band_recovery as recovery  # noqa: E402


class OutOfBandRecoveryTests(unittest.TestCase):
    def _store(self, temporary: str, *, baseline: bool = False) -> monitor.IncidentStore:
        directory = Path(temporary) / "state"
        directory.mkdir(mode=0o700)
        store = monitor.IncidentStore(directory / monitor.DATABASE_NAME)
        store.observe(
            monitor.RESERVED_SUBJECT,
            "system",
            "unreachable",
            100,
            unavailable=True,
            source="startup_snapshot" if baseline else "websocket_watchdog",
        )
        store.confirm_due(160, 60)
        return store

    def test_baseline_and_dry_run_never_probe_or_open_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            baseline_store = self._store(temporary, baseline=True)
            try:
                baseline = recovery.run_once(
                    baseline_store,
                    now=460,
                    live=True,
                    api_unreachable=lambda: self.fail("baseline must not be probed"),
                )
                self.assertEqual(baseline["candidates"], 0)
            finally:
                baseline_store.close()

        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                planned = recovery.run_once(
                    store,
                    now=460,
                    live=False,
                    api_unreachable=lambda: self.fail("dry run must not be probed"),
                    key_loader=lambda: self.fail("dry run must not load a key"),
                )
                self.assertEqual(planned["outcome"], "planned")
                self.assertEqual(planned["ssh_calls"], 0)
                count = store.connection.execute(
                    "SELECT COUNT(*) FROM out_of_band_recovery_actions"
                ).fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                store.close()

    def test_local_recovery_records_health_without_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                result = recovery.run_once(
                    store,
                    now=460,
                    live=True,
                    api_unreachable=lambda: False,
                    key_loader=lambda: self.fail("healthy HA must not load a key"),
                )
                self.assertEqual(result["outcome"], "local_api_recovered")
                self.assertEqual(result["ssh_calls"], 0)
                row = store.connection.execute(
                    "SELECT status,ssh_calls,restart_calls FROM out_of_band_recovery_actions"
                ).fetchone()
                self.assertEqual(tuple(row), ("healthy", 0, 0))
            finally:
                store.close()

    def test_dead_ha_uses_one_forced_command_and_records_verified_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            key = Path(temporary) / "key"
            calls: list[Path] = []
            try:
                result = recovery.run_once(
                    store,
                    now=460,
                    live=True,
                    api_unreachable=lambda: True,
                    key_loader=lambda: key,
                    remote_recover=lambda used_key: calls.append(used_key) or "restarted_verified",
                )
                self.assertEqual(calls, [key])
                self.assertEqual(result["restart_calls"], 1)
                self.assertEqual(result["verified"], 1)
                row = store.connection.execute(
                    "SELECT status,attempts,ssh_calls,restart_calls FROM out_of_band_recovery_actions"
                ).fetchone()
                self.assertEqual(tuple(row), ("verified", 1, 1, 1))
            finally:
                store.close()

    def test_failed_channel_retries_only_three_times_at_five_minute_intervals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                for now in (460, 760, 1060):
                    result = recovery.run_once(
                        store,
                        now=now,
                        live=True,
                        api_unreachable=lambda: True,
                        key_loader=lambda: (_ for _ in ()).throw(
                            recovery.OutOfBandRecoveryError("offline")
                        ),
                    )
                    self.assertEqual(result["outcome"], "channel_failed")
                exhausted = recovery.run_once(
                    store,
                    now=1360,
                    live=True,
                    api_unreachable=lambda: self.fail("budget exhausted"),
                )
                self.assertEqual(exhausted["candidates"], 0)
                attempts = store.connection.execute(
                    "SELECT attempts FROM out_of_band_recovery_actions"
                ).fetchone()[0]
                self.assertEqual(attempts, 3)
            finally:
                store.close()

    def test_ssh_command_is_pinned_noninteractive_and_has_no_shell_or_token(self) -> None:
        captured: dict[str, object] = {}

        def fake_runner(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(command, 0, b"status=healthy_no_action\n", b"")

        result = recovery.ssh_recover(Path("/run/credentials/test/key"), runner=fake_runner)
        self.assertEqual(result, "healthy_no_action")
        command = captured["command"]
        self.assertIsInstance(command, list)
        serialized = " ".join(command)
        self.assertIn("StrictHostKeyChecking=yes", serialized)
        self.assertIn("HostKeyAlias=homebutler-recovery-target", serialized)
        self.assertIn("BatchMode=yes", serialized)
        self.assertIn("ClearAllForwardings=yes", serialized)
        self.assertEqual(command[-1], "recover")
        self.assertNotIn("sh -c", serialized)
        self.assertNotIn("token", serialized.casefold())
        self.assertIs(captured["kwargs"]["stdin"], subprocess.DEVNULL)

    def test_timeout_is_delivery_unknown(self) -> None:
        def timeout_runner(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        with self.assertRaises(recovery.DeliveryUnknown):
            recovery.ssh_recover(Path("/run/credentials/test/key"), runner=timeout_runner)


if __name__ == "__main__":
    unittest.main()
