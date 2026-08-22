#!/usr/bin/env python3
"""Contracts for the persistent Home Butler startup self-check."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import startup_self_check as check  # noqa: E402


BOOT_ID = "01234567-89ab-cdef-0123-456789abcdef"


def proof(endpoint: str, fully_on_gpu: bool) -> dict[str, object]:
    return {
        "verified": True,
        "ollama_endpoint": endpoint,
        "tool_call": {"name": "ha_get_snapshot", "arguments": {}},
        "home_assistant": {
            "status": "healthy",
            "entity_count": 204,
            "http_method": "GET",
            "service_calls": 0,
        },
        "accelerator": {"fully_on_gpu": fully_on_gpu},
    }


class StartupSelfCheckTests(unittest.TestCase):
    def test_gpu_proof_requires_all_runtime_components(self) -> None:
        units = {unit: True for unit in check.REQUIRED_UNITS}
        result = check.evaluate(
            proof("http://172.27.192.1:11434", True),
            units,
            boot_id=BOOT_ID,
            observed_epoch=100,
        )
        self.assertTrue(result["ready"])
        self.assertEqual(result["accelerator"], "gpu")
        self.assertEqual(result["entity_count"], 204)
        self.assertTrue(result["tool_call_verified"])

    def test_cpu_fallback_is_an_explicit_ready_mode(self) -> None:
        units = {unit: True for unit in check.REQUIRED_UNITS}
        result = check.evaluate(
            proof("http://127.0.0.1:11434", False),
            units,
            boot_id=BOOT_ID,
            observed_epoch=100,
        )
        self.assertTrue(result["ready"])
        self.assertEqual(result["accelerator"], "cpu_fallback")

    def test_fake_tool_claim_or_missing_unit_fails_closed(self) -> None:
        units = {unit: True for unit in check.REQUIRED_UNITS}
        units["home-butler-incident-monitor.service"] = False
        fake = proof("http://172.27.192.1:11434", True)
        fake["tool_call"] = {"name": "pretend_snapshot", "arguments": {}}
        result = check.evaluate(
            fake, units, boot_id=BOOT_ID, observed_epoch=100
        )
        self.assertFalse(result["ready"])
        self.assertFalse(result["tool_call_verified"])
        self.assertIn(
            "home-butler-incident-monitor.service", result["inactive_units"]
        )

    def test_status_is_private_and_bound_to_current_boot(self) -> None:
        units = {unit: True for unit in check.REQUIRED_UNITS}
        document = check.evaluate(
            proof("http://172.27.192.1:11434", True),
            units,
            boot_id=BOOT_ID,
            observed_epoch=100,
        )
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir(mode=0o700)
            path = check.write_status(document, state)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            loaded = check.read_status(state, current_boot_id=BOOT_ID)
            self.assertTrue(loaded["ready"])
            with self.assertRaises(check.SelfCheckError):
                check.read_status(
                    state,
                    current_boot_id="fedcba98-7654-3210-fedc-ba9876543210",
                )
            self.assertNotIn("token", json.dumps(loaded).casefold())

    def test_run_once_persists_failure_without_claiming_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir(mode=0o700)
            result = check.run_once(
                proof_runner=lambda: proof("http://172.27.192.1:11434", True),
                unit_checker=lambda unit: unit != "home-butler-alice-tunnel.service",
                boot_id_reader=lambda: BOOT_ID,
                clock=lambda: 100.0,
                state_dir=state,
            )
            self.assertFalse(result["ready"])
            self.assertEqual(
                result["inactive_units"], ["home-butler-alice-tunnel.service"]
            )


if __name__ == "__main__":
    unittest.main()
