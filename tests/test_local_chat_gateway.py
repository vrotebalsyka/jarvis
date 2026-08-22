#!/usr/bin/env python3
"""Contracts for the protected local Home Butler browser chat."""

from __future__ import annotations

import sys
import ipaddress
from unittest import mock
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import local_chat_gateway as gateway  # noqa: E402


class LocalChatGatewayTests(unittest.TestCase):
    def test_message_parser_is_strict(self) -> None:
        self.assertEqual(
            gateway.parse_message('{"message":"  привет  "}'.encode("utf-8")),
            "привет",
        )
        for raw in (
            b"",
            b"{}",
            b'{"message":"x","extra":1}',
            b'{"message":"x","message":"y"}',
            b'{"message":1}',
        ):
            with self.subTest(raw=raw), self.assertRaises(gateway.LocalChatError):
                gateway.parse_message(raw)

    def test_application_uses_same_owner_chat_history_engine(self) -> None:
        calls: list[tuple[str, list[dict[str, str]]]] = []

        def answer(question, _context, history):
            calls.append((question, history))
            return "Первый ответ" if len(calls) == 1 else "Второй ответ"

        app = gateway.ChatApplication(
            answerer=answer,
            context_factory=lambda: {"trusted": True},
            clock=lambda: 10.0,
        )
        session_id = "A" * 43
        self.assertEqual(app.answer(session_id, "первый"), "Первый ответ")
        self.assertEqual(app.answer(session_id, "продолжи"), "Второй ответ")
        self.assertEqual(calls[1][1][-2]["content"], "первый")
        self.assertEqual(calls[1][1][-1]["content"], "Первый ответ")

    def test_ui_has_no_external_assets(self) -> None:
        self.assertEqual(gateway.DEFAULT_BIND_HOST, "127.0.0.1")
        self.assertIn("X-Home-Butler-CSRF", gateway.HTML)
        self.assertIn("Свободный диалог", gateway.HTML)
        self.assertIn("Свободный ИИ без шаблонных ответов", gateway.HTML)
        self.assertIn("'/модель '+message", gateway.HTML)
        self.assertIn("repeat(5", gateway.HTML)
        self.assertNotIn("https://", gateway.HTML)
        self.assertNotIn("http://", gateway.HTML)

    def test_lan_configuration_is_strict(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "HOME_BUTLER_LOCAL_CHAT_BIND_HOST": "0.0.0.0",
                "HOME_BUTLER_LOCAL_CHAT_ALLOWED_HOSTS": (
                    "127.0.0.1:8780,192.168.1.175:8780"
                ),
                "HOME_BUTLER_LOCAL_CHAT_ALLOWED_NETWORKS": (
                    "127.0.0.0/8,192.168.1.0/24"
                ),
            },
            clear=False,
        ):
            self.assertEqual(gateway.load_bind_host(), "0.0.0.0")
            self.assertIn("192.168.1.175:8780", gateway.load_allowed_hosts(8780))
            networks = gateway.load_allowed_networks()
        self.assertTrue(gateway.address_allowed("192.168.1.42", networks))
        self.assertTrue(gateway.address_allowed("127.0.0.1", networks))
        self.assertFalse(gateway.address_allowed("192.168.2.42", networks))
        self.assertFalse(gateway.address_allowed("not-an-address", networks))
        with mock.patch.dict(
            "os.environ", {"HOME_BUTLER_LOCAL_CHAT_BIND_HOST": "192.168.1.175"}
        ), self.assertRaises(gateway.LocalChatError):
            gateway.load_bind_host()

    def test_lan_access_key_signs_browser_session(self) -> None:
        app = gateway.ChatApplication(
            answerer=lambda *_args: "ok",
            context_factory=dict,
            lan_access_key="owner-key-with-enough-entropy-123",
        )
        session_id = "A" * 43
        self.assertTrue(app.verify_access_key("owner-key-with-enough-entropy-123"))
        self.assertFalse(app.verify_access_key("wrong-key-with-enough-entropy-123"))
        signature = app.lan_signature(session_id)
        self.assertRegex(signature, gateway.SESSION_RE)
        self.assertNotEqual(signature, app.lan_signature("B" * 43))

    def test_lan_backend_port_is_optional_and_bounded(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(gateway.load_lan_backend_port())
        with mock.patch.dict(
            "os.environ", {"HOME_BUTLER_LAN_CHAT_BACKEND_PORT": "8781"}, clear=True
        ):
            self.assertEqual(gateway.load_lan_backend_port(), 8781)
        for value in ("not-a-port", "80", "70000"):
            with self.subTest(value=value), mock.patch.dict(
                "os.environ", {"HOME_BUTLER_LAN_CHAT_BACKEND_PORT": value}, clear=True
            ), self.assertRaises(gateway.LocalChatError):
                gateway.load_lan_backend_port()

    def test_access_key_parser_is_strict(self) -> None:
        self.assertEqual(
            gateway.parse_access_key(
                b'{"key":"owner-key-with-enough-entropy-123"}'
            ),
            "owner-key-with-enough-entropy-123",
        )
        for raw in (b"", b"{}", b'{"key":"short"}', b'{"key":1}'):
            with self.subTest(raw=raw), self.assertRaises(gateway.LocalChatError):
                gateway.parse_access_key(raw)

    def test_local_status_prefers_physical_device_names(self) -> None:
        with mock.patch(
            "local_chat_gateway.incident_status.read_summary",
            return_value={
                "device_incidents": [{
                    "display_name": "Увлажнитель",
                    "status": "confirmed",
                    "first_observed_epoch": 200,
                    "member_subjects": ["switch.humidifier"],
                }],
                "operational_incidents": [],
                "incidents": [{
                    "subject": "switch.humidifier",
                    "status": "confirmed",
                    "baseline": False,
                    "first_observed_epoch": 200,
                }],
                "device_notification_enabled_epoch": 100,
            },
        ), mock.patch(
            "local_chat_gateway.startup_self_check.read_boot_id",
            return_value="01234567-89ab-cdef-0123-456789abcdef",
        ), mock.patch(
            "local_chat_gateway.startup_self_check.read_status",
            return_value={
                "accelerator": "gpu",
                "home_assistant_ready": True,
                "observer_ready": True,
                "notifications_ready": True,
                "alice_local_ready": True,
            },
        ):
            result = gateway.local_status()
        self.assertEqual(result["monitor"], "ready")
        self.assertEqual(result["alerts"][0]["name"], "Увлажнитель")
        self.assertEqual(len(result["alerts"]), 1)
        self.assertEqual(result["self_check"]["accelerator"], "gpu")
        self.assertTrue(result["self_check"]["ready"])

    def test_local_status_hides_pre_universal_device_history(self) -> None:
        with mock.patch(
            "local_chat_gateway.incident_status.read_summary",
            return_value={
                "device_incidents": [{
                    "display_name": "Старая техническая запись",
                    "status": "confirmed",
                    "first_observed_epoch": 99,
                }],
                "operational_incidents": [],
                "incidents": [],
                "device_notification_enabled_epoch": 100,
            },
        ), mock.patch(
            "local_chat_gateway.startup_self_check.read_boot_id",
            side_effect=gateway.startup_self_check.SelfCheckError("test"),
        ):
            result = gateway.local_status()
        self.assertEqual(result["alerts"], [])

    def test_local_status_hides_stale_operational_history(self) -> None:
        with mock.patch(
            "local_chat_gateway.incident_status.read_summary",
            return_value={
                "device_incidents": [],
                "operational_incidents": [{
                    "display_name": "Старый сбой сценария",
                    "status": "confirmed",
                    "first_observed_epoch": 10,
                    "last_observed_epoch": 20,
                }],
                "incidents": [],
                "device_notification_enabled_epoch": 100,
            },
        ), mock.patch(
            "local_chat_gateway.startup_self_check.read_boot_id",
            side_effect=gateway.startup_self_check.SelfCheckError("test"),
        ), mock.patch("local_chat_gateway.time.time", return_value=200_000.0):
            result = gateway.local_status()
        self.assertEqual(result["alerts"], [])


if __name__ == "__main__":
    unittest.main()
