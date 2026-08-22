#!/usr/bin/env python3
"""Safety tests for zero-downtime Alice webhook rotation."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_DIR / "scripts" / "rotate-alice-webhook.py"
SPEC = importlib.util.spec_from_file_location("rotate_alice_webhook", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
rotation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rotation
SPEC.loader.exec_module(rotation)


class AliceWebhookRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.secrets = root / "secrets"
        self.state = root / "state"
        self.secrets.mkdir(mode=0o700)
        self.state.mkdir(mode=0o700)
        os.chmod(self.secrets, 0o700)
        os.chmod(self.state, 0o700)
        self.primary = self.secrets / "alice-skill-secret"
        self.next = self.secrets / "alice-skill-secret-next"
        self.url = self.secrets / "alice-webhook-url.txt"
        self.origin = self.secrets / "alice-public-origin.txt"
        self.marker = self.state / "webhook-next-used"
        self._write(self.primary, "A" * 40)
        self._write(self.next, "A" * 40)
        self._write(self.url, "https://example.invalid/original")
        self._write(self.origin, "https://home-butler.example-tail.ts.net")
        self.layout = rotation.Layout(
            self.primary,
            self.next,
            self.url,
            self.origin,
            self.marker,
            os.getuid(),
            os.getgid(),
        )
        self.calls: list[tuple[str, ...]] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write(path: Path, value: str) -> None:
        path.write_text(value + "\n", encoding="ascii")
        os.chmod(path, 0o600)

    def _runner(self, args: object) -> None:
        self.calls.append(tuple(args))

    def test_stage_keeps_primary_and_publishes_a_distinct_next_url(self) -> None:
        self.assertEqual(rotation.stage(self.layout, self._runner), "staged")
        primary = self.primary.read_text(encoding="ascii").strip()
        next_secret = self.next.read_text(encoding="ascii").strip()
        self.assertEqual(primary, "A" * 40)
        self.assertNotEqual(primary, next_secret)
        self.assertTrue(self.url.read_text(encoding="ascii").strip().endswith(next_secret))
        self.assertEqual(rotation.status(self.layout), "staged_waiting_for_new_request")
        self.assertEqual(
            self.calls,
            [
                ("restart", rotation.SERVICE_NAME),
                ("is-active", "--quiet", rotation.SERVICE_NAME),
            ],
        )

    def test_commit_requires_authenticated_use_of_next_secret(self) -> None:
        rotation.stage(self.layout, self._runner)
        with self.assertRaises(rotation.RotationError):
            rotation.commit(self.layout, self._runner)
        self.assertEqual(self.primary.read_text(encoding="ascii").strip(), "A" * 40)

    def test_verified_commit_retires_old_secret(self) -> None:
        rotation.stage(self.layout, self._runner)
        next_secret = self.next.read_text(encoding="ascii").strip()
        self._write(self.marker, rotation.marker_value(next_secret))
        self.assertEqual(
            rotation.status(self.layout), "staged_verified_ready_to_commit"
        )
        self.assertEqual(rotation.commit(self.layout, self._runner), "committed")
        self.assertEqual(self.primary.read_text(encoding="ascii").strip(), next_secret)
        self.assertFalse(self.marker.exists())
        self.assertEqual(rotation.status(self.layout), "idle")

    def test_abort_restores_single_old_secret(self) -> None:
        rotation.stage(self.layout, self._runner)
        self.assertEqual(rotation.abort(self.layout, self._runner), "aborted")
        self.assertEqual(
            self.primary.read_text(encoding="ascii"),
            self.next.read_text(encoding="ascii"),
        )
        self.assertEqual(rotation.status(self.layout), "idle")

    def test_stage_rejects_non_tailscale_origin(self) -> None:
        self._write(self.origin, "https://example.invalid")
        with self.assertRaises(rotation.RotationError):
            rotation.stage(self.layout, self._runner)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
