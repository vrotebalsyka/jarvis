#!/usr/bin/env python3
"""Static safety contract for Alice full-dialog provisioning."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys


PROJECT_DIR = Path(__file__).resolve().parents[1]
SETUP = PROJECT_DIR / "scripts" / "prepare-alice-full-dialog.sh"
TEXT = SETUP.read_text(encoding="utf-8")
FINALIZER = PROJECT_DIR / "scripts" / "alice_claim_finalizer.py"
FINALIZER_SERVICE = (
    PROJECT_DIR / "config/systemd/home-butler-alice-finalize.service"
)
FINALIZER_PATH = PROJECT_DIR / "config/systemd/home-butler-alice-finalize.path"
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import alice_claim_finalizer as finalizer  # noqa: E402


class AliceFullDialogSetupTests(unittest.TestCase):
    def test_script_is_valid_bash(self) -> None:
        subprocess.run(["bash", "-n", str(SETUP)], check=True)

    def test_prepare_is_fail_closed_and_never_prints_credentials(self) -> None:
        self.assertIn("--prepare|--finalize", TEXT)
        self.assertIn("PENDING_PRIVATE_SKILL", TEXT)
        self.assertIn("secrets.token_urlsafe(48)", TEXT)
        self.assertIn("alice-skill-secret-next", TEXT)
        self.assertIn("alice_skill_health.py", TEXT)
        self.assertIn("home-butler-alice-health.timer", TEXT)
        self.assertIn("systemctl enable --now home-butler-alice-health.timer", TEXT)
        self.assertIn("rotate-alice-webhook.py", TEXT)
        self.assertIn("chmod 0600", TEXT)
        self.assertIn("mv -fT", TEXT)
        self.assertIn("unset secret", TEXT)
        self.assertNotIn("printf '%s\\n' \"$secret\"", TEXT)
        self.assertNotIn("cat \"$SECRET_FILE\"", TEXT)
        self.assertNotIn("authtoken", TEXT)

    def test_only_loopback_gateway_is_tunneled(self) -> None:
        helper = (
            PROJECT_DIR / "scripts" / "alice_tailscale_funnel.py"
        ).read_text(encoding="utf-8")
        unit = (
            PROJECT_DIR / "config" / "systemd" /
            "home-butler-alice-tunnel.service"
        ).read_text(encoding="utf-8")
        self.assertIn('FUNNEL_TARGET = "http://127.0.0.1:8765"', helper)
        self.assertNotIn("0.0.0.0", helper)
        self.assertNotIn("192.168.1.127:8123", helper + unit)

    def test_finalize_requires_private_first_request_claim(self) -> None:
        self.assertIn("claim.json", TEXT)
        self.assertIn("No safe Yandex provisioning claim has arrived yet", TEXT)
        self.assertIn("write_private_root_file \"$SKILL_FILE\"", TEXT)
        self.assertIn("write_private_root_file \"$OWNERS_FILE\"", TEXT)
        self.assertIn("write_private_service_file pending", TEXT)
        self.assertIn("write_private_service_file pinned", TEXT)

    def test_first_claim_is_automatically_pinned_by_a_bounded_root_unit(self) -> None:
        service = FINALIZER_SERVICE.read_text(encoding="utf-8")
        path = FINALIZER_PATH.read_text(encoding="utf-8")
        self.assertIn("alice_claim_finalizer.py", TEXT)
        self.assertIn("home-butler-alice-finalize.path", TEXT)
        self.assertIn("systemctl enable --now home-butler-alice-finalize.path", TEXT)
        self.assertIn("ExecStart=/usr/bin/python3 /opt/home-butler/scripts/alice_claim_finalizer.py", service)
        self.assertIn("ExecStartPre=/usr/bin/sleep 3", service)
        self.assertIn("Restart=on-failure", service)
        self.assertIn("StartLimitBurst=3", service)
        self.assertIn("NoNewPrivileges=yes", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("RestrictAddressFamilies=AF_UNIX", service)
        self.assertIn("PathExists=/home/homebutler/.local/state/home-butler/alice/claim.json", path)
        self.assertNotIn("PathChanged=", path)


class AliceClaimFinalizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.secrets = root / "secrets"
        self.state = root / "state"
        self.secrets.mkdir(mode=0o700)
        self.state.mkdir(mode=0o700)
        os.chmod(self.secrets, 0o700)
        os.chmod(self.state, 0o700)
        self.claim = self.state / "claim.json"
        self.skill = self.secrets / "alice-skill-id"
        self.owners = self.secrets / "alice-owner-ids"
        self.mode = self.state / "mode"
        self.skill.write_text(finalizer.PENDING_SKILL_ID + "\n", encoding="ascii")
        self.owners.write_text("-\n", encoding="ascii")
        os.chmod(self.skill, 0o600)
        os.chmod(self.owners, 0o600)
        self.layout = finalizer.Layout(
            self.claim,
            self.skill,
            self.owners,
            self.mode,
            os.getuid(),
            os.getgid(),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_claim(self, skill_id: str = "skill-private-1234") -> None:
        self.claim.write_text(
            json.dumps({"skill_id": skill_id, "user_id": "owner-private-1234"}),
            encoding="ascii",
        )
        os.chmod(self.claim, 0o600)

    def test_valid_claim_pins_identity_restarts_exact_service_and_is_consumed(self) -> None:
        self._write_claim()
        calls: list[tuple[str, ...]] = []
        finalizer.finalize(self.layout, lambda args: calls.append(tuple(args)))
        self.assertEqual(self.skill.read_text(encoding="ascii").strip(), "skill-private-1234")
        self.assertEqual(self.owners.read_text(encoding="ascii").strip(), "owner-private-1234")
        self.assertEqual(self.mode.read_text(encoding="ascii").strip(), "pinned")
        self.assertEqual(calls, [
            ("restart", finalizer.SERVICE_NAME),
            ("is-active", "--quiet", finalizer.SERVICE_NAME),
        ])
        self.assertFalse(self.claim.exists())

    def test_failed_restart_keeps_claim_for_bounded_retry(self) -> None:
        self._write_claim()
        with self.assertRaises(RuntimeError):
            finalizer.finalize(
                self.layout,
                lambda _args: (_ for _ in ()).throw(RuntimeError("failed")),
            )
        self.assertTrue(self.claim.exists())

    def test_conflicting_or_malformed_claim_never_restarts_service(self) -> None:
        self._write_claim("skill-private-1234")
        self.skill.write_text("skill-different-1234\n", encoding="ascii")
        os.chmod(self.skill, 0o600)
        runner = mock.Mock()
        with self.assertRaises(finalizer.FinalizeError):
            finalizer.finalize(self.layout, runner)
        runner.assert_not_called()
        self.assertTrue(self.claim.exists())


if __name__ == "__main__":
    unittest.main()
