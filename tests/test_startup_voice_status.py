#!/usr/bin/env python3

from __future__ import annotations

import os
import pwd
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import home_assistant_notify as ha_notify  # noqa: E402
import startup_voice_status as startup  # noqa: E402


BOOT_ID = "01234567-89ab-cdef-0123-456789abcdef"


def snapshot(status: str = "healthy") -> dict[str, object]:
    return {
        "status": status,
        "entity_count": 20,
        "available_entity_count": 17,
        "unavailable_entity_count": 3,
        "entities": [
            {
                "entity_id": "media_player.yandex_station_x10x2a000qpm2b",
                "state_kind": "enum",
            }
        ],
    }


class StartupVoiceStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / "status.json"
        self.calls: list[tuple[object, str, str]] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def execute(self, **overrides):
        arguments = {
            "state_path": self.path,
            "boot_id_reader": lambda: BOOT_ID,
            "snapshot_reader": lambda command: (snapshot(), 0),
            "tunnel_checker": lambda: {
                "local_ready": True,
                "public_ready": True,
            },
            "config_loader": lambda: object(),
            "state_reader": lambda config, speaker: {
                "last_updated": "before",
                "volume_ready": True,
                "muted": False,
            },
            "service_caller": lambda config, speaker, message: self.calls.append(
                (config, speaker, message)
            ),
            "delivery_verifier": lambda config, speaker, baseline: True,
            "now": lambda: 1_786_460_400,
        }
        arguments.update(overrides)
        return startup.execute(**arguments)

    def test_announces_fresh_ha_and_exact_alice_path_once(self) -> None:
        result = self.execute()
        self.assertEqual("verified", result["status"])
        self.assertEqual(1, result["service_calls"])
        self.assertEqual(1, len(self.calls))
        message = self.calls[0][2]
        self.assertIn("Home Assistant на связи", message)
        self.assertIn("Доступно сущностей: 17; недоступно: 3", message)
        self.assertIn("защищённый туннель до Яндекса отвечают", message)
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        state = startup.load_state(self.path)
        self.assertIsNotNone(state)
        self.assertEqual("verified", state["delivery_status"])

        second = self.execute(
            snapshot_reader=lambda command: self.fail("snapshot repeated"),
            service_caller=lambda config, speaker, message: self.fail("TTS repeated"),
        )
        self.assertEqual("already_verified", second["status"])
        self.assertEqual(0, second["service_calls"])

    def test_announces_public_tunnel_failure_without_recovery(self) -> None:
        result = self.execute(
            tunnel_checker=lambda: {
                "local_ready": True,
                "public_ready": False,
            }
        )
        self.assertEqual("verified", result["status"])
        self.assertIn("туннель до Яндекса не подтверждён", self.calls[0][2])
        state = startup.load_state(self.path)
        self.assertFalse(state["alice_public_ready"])

    def test_does_not_speak_until_home_assistant_is_ready(self) -> None:
        with self.assertRaises(startup.StartupVoiceError):
            self.execute(
                snapshot_reader=lambda command: (
                    {"status": "host_unreachable"},
                    0,
                )
            )
        self.assertFalse(self.path.exists())
        self.assertEqual([], self.calls)

    def test_unverified_acceptance_is_not_spoken_twice(self) -> None:
        result = self.execute(
            delivery_verifier=lambda config, speaker, baseline: False
        )
        self.assertEqual("accepted_unverified", result["status"])
        self.assertEqual(1, len(self.calls))
        second = self.execute(
            service_caller=lambda config, speaker, message: self.fail("TTS repeated")
        )
        self.assertEqual("already_attempted_unverified", second["status"])
        self.assertEqual(0, second["service_calls"])

    def test_delivery_unknown_is_recorded_to_prevent_duplicate_speech(self) -> None:
        def unknown(config, speaker, message):
            raise ha_notify.NotifyDeliveryUnknown("unknown")

        with self.assertRaises(ha_notify.NotifyDeliveryUnknown):
            self.execute(service_caller=unknown)
        state = startup.load_state(self.path)
        self.assertEqual("accepted_unverified", state["delivery_status"])

    def test_rejects_unsafe_status_file(self) -> None:
        self.path.write_text("{}", encoding="ascii")
        os.chmod(self.path, 0o644)
        with self.assertRaises(startup.StartupVoiceError):
            startup.load_state(self.path)

    def test_root_operator_can_read_homebutler_private_status(self) -> None:
        if os.geteuid() != 0:
            self.skipTest("root-only ownership contract")
        self.execute()
        account = pwd.getpwnam("homebutler")
        os.chown(self.path, account.pw_uid, account.pw_gid)
        os.chown(self.directory, account.pw_uid, account.pw_gid)
        self.assertEqual("verified", startup.load_state(self.path)["delivery_status"])

    def test_cli_failure_exposes_only_fixed_safe_reason(self) -> None:
        original = startup.execute
        try:
            startup.execute = lambda: (_ for _ in ()).throw(
                startup.StartupVoiceError("Home Assistant snapshot is unavailable")
            )
            self.assertEqual(3, startup.run([]))
        finally:
            startup.execute = original


if __name__ == "__main__":
    unittest.main()
