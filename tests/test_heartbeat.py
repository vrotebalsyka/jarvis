#!/usr/bin/env python3
"""Offline tests for the persistent read-only heartbeat runner."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import heartbeat  # noqa: E402


def snapshot_bytes(
    *,
    ha_status: str = "healthy",
    failed_units: tuple[str, ...] = (),
) -> bytes:
    observed_at = datetime.now(timezone.utc).isoformat()
    document = {
        "schema_version": 1,
        "observed_at": observed_at,
        "host": {"cpu_load_percent": 10, "memory_used_percent": 20, "swap_used_percent": 0},
        "disks": [{
            "filesystem": "/dev/sda", "type": "ext4", "total_bytes": 1000,
            "used_bytes": 100, "available_bytes": 900, "used_percent": 10,
        }],
        "temperatures": [],
        "failed_systemd_units": list(failed_units),
        "probes": {
            "temperatures": "unavailable", "systemd": "ok", "ollama_version": "ok",
            "ollama_models": "ok", "hermes_gateway": "ok",
        },
        "ollama": {
            "reachable": True, "version": "0.32.5", "model_loaded": True,
            "loaded_models": [{
                "name": "home-butler:latest", "size_bytes": 1, "size_vram_bytes": 1,
                "context_length": 8192, "expires_at": "2026-08-02T00:00:00Z",
            }],
        },
        "hermes": {
            "installed": True, "gateway_configured": True,
            "gateway_running": True, "status": "running",
        },
        "home_assistant": {"configured": True, "status": ha_status},
    }
    return json.dumps(document, ensure_ascii=False).encode()


class HeartbeatTests(unittest.TestCase):
    def _state_dir(self, temporary: str) -> Path:
        path = Path(temporary) / "state"
        path.mkdir(mode=0o700)
        return path

    def test_first_run_emits_and_duplicate_is_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = self._state_dir(temporary)
            snapshot = snapshot_bytes()
            report = "HEARTBEAT_OK\n".encode()

            first_output = StringIO()
            with redirect_stdout(first_output):
                emitted = heartbeat.run_once(
                    state_dir, now=lambda: 1000, collector=lambda: snapshot,
                    reporter=lambda _snapshot: report,
                )
            self.assertTrue(emitted)
            self.assertEqual(first_output.getvalue(), "HEARTBEAT_OK\n")

            second_output = StringIO()
            with redirect_stdout(second_output):
                emitted = heartbeat.run_once(
                    state_dir, now=lambda: 1100, collector=lambda: snapshot,
                    reporter=lambda _snapshot: report,
                )
            self.assertFalse(emitted)
            self.assertIn("HEARTBEAT_SUPPRESSED", second_output.getvalue())
            for name in ("heartbeat-state.json", "latest-snapshot.json", "latest-report.txt"):
                self.assertEqual(stat_mode(state_dir / name), 0o600)

    def test_changed_status_and_elapsed_cooldown_emit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = self._state_dir(temporary)
            healthy = snapshot_bytes()
            stale = snapshot_bytes(ha_status="stale_data")
            report = b"report\n"
            with redirect_stdout(StringIO()):
                heartbeat.run_once(state_dir, now=lambda: 1000, collector=lambda: healthy, reporter=lambda _: report)
                self.assertTrue(
                    heartbeat.run_once(state_dir, now=lambda: 1100, collector=lambda: stale, reporter=lambda _: report)
                )
                self.assertTrue(
                    heartbeat.run_once(
                        state_dir,
                        now=lambda: 1100 + heartbeat.DUPLICATE_COOLDOWN_SECONDS,
                        collector=lambda: stale,
                        reporter=lambda _: report,
                    )
                )

    def test_changed_failed_unit_identity_emits_with_same_problem_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = self._state_dir(temporary)
            first = snapshot_bytes(failed_units=("alpha.service",))
            second = snapshot_bytes(failed_units=("beta.service",))
            with redirect_stdout(StringIO()):
                heartbeat.run_once(
                    state_dir, now=lambda: 1000, collector=lambda: first,
                    reporter=lambda _: b"first\n",
                )
                emitted = heartbeat.run_once(
                    state_dir, now=lambda: 1100, collector=lambda: second,
                    reporter=lambda _: b"second\n",
                )
            self.assertTrue(emitted)

    def test_clock_rollback_does_not_extend_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = self._state_dir(temporary)
            snapshot = snapshot_bytes()
            with redirect_stdout(StringIO()):
                heartbeat.run_once(
                    state_dir, now=lambda: 1000, collector=lambda: snapshot,
                    reporter=lambda _: b"first\n",
                )
                emitted = heartbeat.run_once(
                    state_dir, now=lambda: 900, collector=lambda: snapshot,
                    reporter=lambda _: b"second\n",
                )
            self.assertTrue(emitted)
            state = json.loads((state_dir / "heartbeat-state.json").read_text())
            self.assertEqual(state["schema_version"], 2)
            self.assertEqual(len(state["snapshot_sha256"]), 64)
            self.assertEqual(len(state["report_sha256"]), 64)

    def test_main_fails_closed_without_exception_details(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = self._state_dir(temporary)
            previous = os.environ.get("HOME_BUTLER_HEARTBEAT_STATE_DIR")
            os.environ["HOME_BUTLER_HEARTBEAT_STATE_DIR"] = str(state_dir)
            original = heartbeat.collect_snapshot
            heartbeat.collect_snapshot = lambda: (_ for _ in ()).throw(heartbeat.HeartbeatError("SECRET"))
            output, errors = StringIO(), StringIO()
            try:
                with redirect_stdout(output), redirect_stderr(errors):
                    code = heartbeat.main()
            finally:
                heartbeat.collect_snapshot = original
                if previous is None:
                    os.environ.pop("HOME_BUTLER_HEARTBEAT_STATE_DIR", None)
                else:
                    os.environ["HOME_BUTLER_HEARTBEAT_STATE_DIR"] = previous
            self.assertEqual(code, 2)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(errors.getvalue(), "HEARTBEAT_FAILED\n")


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


if __name__ == "__main__":
    unittest.main()
