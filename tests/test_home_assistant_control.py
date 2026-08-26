#!/usr/bin/env python3
"""Contracts for the strict Home Assistant control and receipt boundary."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import home_assistant_control as control  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402


TEST_TOKEN = "SECRET_SENTINEL.DO_NOT_LEAK.abcdefghijklmnopqrstuvwxyz"


def config() -> ha_read.AdapterConfig:
    return ha_read.AdapterConfig(
        "http", "192.168.1.127", 8123, TEST_TOKEN, (), True
    )


def snapshot(entity_id: str, value: object) -> dict[str, object]:
    return {
        "status": "healthy",
        "entities": [
            {
                "entity_id": entity_id,
                "state_kind": "enum" if value is not None else "unavailable",
                "state_value": value,
            }
        ],
    }


class FakeResponse:
    status = 200

    def read(self, _amount: int) -> bytes:
        return b"[]"


class FakeConnection:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, bytes, dict[str, str]]] = []
        self.closed = False

    def request(self, method, path, *, body, headers) -> None:
        self.requests.append((method, path, body, headers))

    def getresponse(self) -> FakeResponse:
        return FakeResponse()

    def close(self) -> None:
        self.closed = True


class ControlBoundaryTests(unittest.TestCase):
    def test_failed_post_is_counted_as_sent_and_not_as_preflight_rejection(self) -> None:
        error = control.ControlError(
            "sanitized", status="failed", service_calls=1, delivery="ha_rejected"
        )
        with mock.patch.object(control, "execute", side_effect=error):
            result, exit_code = control.execute_safely(
                "switch.kavidor_switch_1", "turn_on"
            )
        self.assertEqual(exit_code, 4)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["service_calls"], 1)
        self.assertEqual(result["http_method"], "POST")
        self.assertEqual(result["delivery"], "ha_rejected")
        self.assertEqual(result["verification_strength"], "none")

    def test_preflight_rejection_reports_zero_service_calls(self) -> None:
        with mock.patch.object(
            control, "execute", side_effect=control.ControlError("invalid entity")
        ):
            result, exit_code = control.execute_safely("lock.front_door", "unlock")
        self.assertEqual(exit_code, 3)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["service_calls"], 0)
        self.assertEqual(result["delivery"], "not_sent")

    def test_only_bounded_device_feature_actions_are_accepted(self) -> None:
        accepted = (
            ("switch.kavidor", "turn_on"),
            ("switch.kavidor", "turn_off"),
            ("switch.kavidor", "toggle"),
            ("button.identify", "press"),
            ("light.kitchen", "turn_on"),
            ("light.kitchen", "turn_off"),
            ("light.kitchen", "toggle"),
            ("fan.office", "turn_on"),
            ("humidifier.bedroom", "turn_off"),
            ("siren.garage", "toggle"),
            ("vacuum.andrey", "start"),
            ("vacuum.andrey", "stop"),
            ("vacuum.andrey", "return_home"),
            ("number.andrey_volume", "set_value", 5),
            ("select.andrey_mode", "set_option", "Sweep"),
        )
        for item in accepted:
            entity_id, action, *values = item
            with self.subTest(entity_id=entity_id, action=action):
                control.validate_request(entity_id, action, *values)
        for entity_id, action in (
            ("lock.door", "unlock"),
            ("script.cleanup", "turn_on"),
            ("button.identify", "turn_on"),
            ("switch.kavidor", "press"),
            ("vacuum.andrey", "turn_on"),
        ):
            with self.subTest(entity_id=entity_id, action=action), self.assertRaises(
                control.ControlError
            ):
                control.validate_request(entity_id, action)

    def test_post_uses_one_fixed_service_path_and_minimal_body(self) -> None:
        connection = FakeConnection()
        control.post_service(
            config(), "switch.kavidor_switch_1", "turn_on",
            connection_factory=lambda _config: connection,
        )
        self.assertTrue(connection.closed)
        method, path, body, headers = connection.requests[0]
        self.assertEqual((method, path), ("POST", "/api/services/switch/turn_on"))
        self.assertEqual(json.loads(body), {"entity_id": "switch.kavidor_switch_1"})
        self.assertNotIn(TEST_TOKEN, path)
        self.assertNotIn(TEST_TOKEN, body.decode())
        self.assertEqual(headers["Authorization"], f"Bearer {TEST_TOKEN}")

    def test_vacuum_return_uses_fixed_service_path(self) -> None:
        connection = FakeConnection()
        control.post_service(
            config(), "vacuum.andrey", "return_home",
            connection_factory=lambda _config: connection,
        )
        method, path, body, _headers = connection.requests[0]
        self.assertEqual((method, path), ("POST", "/api/services/vacuum/return_to_base"))
        self.assertEqual(json.loads(body), {"entity_id": "vacuum.andrey"})

    def test_number_and_select_posts_have_only_bounded_value_fields(self) -> None:
        for entity_id, action, value, expected_path, expected_body in (
            (
                "number.andrey_volume", "set_value", 5,
                "/api/services/number/set_value",
                {"entity_id": "number.andrey_volume", "value": 5.0},
            ),
            (
                "select.andrey_mode", "set_option", "Sweep",
                "/api/services/select/select_option",
                {"entity_id": "select.andrey_mode", "option": "Sweep"},
            ),
        ):
            with self.subTest(entity_id=entity_id):
                connection = FakeConnection()
                control.post_service(
                    config(), entity_id, action, value,
                    connection_factory=lambda _config: connection,
                )
                method, path, body, _headers = connection.requests[0]
                self.assertEqual((method, path), ("POST", expected_path))
                self.assertEqual(json.loads(body), expected_body)

    def test_number_value_is_range_checked_and_requires_stable_readback(self) -> None:
        reads = iter((
            snapshot("number.andrey_volume", 1.0),
            snapshot("number.andrey_volume", 5.0),
            snapshot("number.andrey_volume", 5.0),
        ))
        calls = []

        def read(command):
            if command == "control-catalog":
                return {
                    "status": "healthy",
                    "control_entities": [{
                        "entity_id": "number.andrey_volume",
                        "friendly_name": "Андрей Alarm Volume",
                        "available": True,
                        "min": 0.0, "max": 10.0, "step": 1.0,
                    }],
                }, 0
            return next(reads), 0

        with mock.patch.object(ha_read, "load_config", return_value=config()):
            result, exit_code = control.execute(
                "number.andrey_volume", "set_value", 5,
                snapshot_reader=read,
                service_caller=lambda _cfg, entity_id, action, value: calls.append(
                    (entity_id, action, value)
                ),
                sleeper=lambda _seconds: None,
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [("number.andrey_volume", "set_value", 5)])
        self.assertEqual(result["after_state"], 5.0)
        self.assertEqual(result["verification"], "stable_state_matches_expected")

    def test_switch_action_requires_two_matching_readbacks(self) -> None:
        snapshots = iter((
            snapshot("switch.kavidor_switch_1", "off"),
            snapshot("switch.kavidor_switch_1", "on"),
            snapshot("switch.kavidor_switch_1", "on"),
        ))
        calls: list[tuple[str, str]] = []
        with mock.patch.object(ha_read, "load_config", return_value=config()):
            result, exit_code = control.execute(
                "switch.kavidor_switch_1", "turn_on",
                snapshot_reader=lambda _command: (next(snapshots), 0),
                service_caller=lambda _cfg, entity_id, action: calls.append((entity_id, action)),
                sleeper=lambda _seconds: None,
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [("switch.kavidor_switch_1", "turn_on")])
        self.assertEqual(result["before_state"], "off")
        self.assertEqual(result["after_state"], "on")
        self.assertEqual(result["verification"], "stable_state_matches_expected")
        self.assertEqual(result["verification_strength"], "state_readback")

    def test_transient_switch_state_is_not_verified(self) -> None:
        values = ["off", "on", "off", "off", "off", "off", "off", "off", "off"]
        snapshots = iter(snapshot("switch.dishwasher_power", value) for value in values)
        with mock.patch.object(ha_read, "load_config", return_value=config()):
            result, exit_code = control.execute(
                "switch.dishwasher_power", "turn_on",
                snapshot_reader=lambda _command: (next(snapshots), 0),
                service_caller=lambda *_args: None,
                sleeper=lambda _seconds: None,
            )
        self.assertEqual(exit_code, 4)
        self.assertEqual(result["status"], "not_verified")
        self.assertEqual(result["after_state"], "off")

    def test_button_press_is_accepted_but_never_physically_verified(self) -> None:
        snapshots = iter((snapshot("button.identify", None), snapshot("button.identify", None)))
        with mock.patch.object(ha_read, "load_config", return_value=config()):
            result, exit_code = control.execute(
                "button.identify", "press",
                snapshot_reader=lambda _command: (next(snapshots), 0),
                service_caller=lambda *_args: None,
            )
        self.assertEqual(exit_code, control.ACCEPTED_UNVERIFIED_EXIT)
        self.assertEqual(result["status"], "accepted_unverified")
        self.assertEqual(result["verification"], "command_accepted_no_physical_proof")
        self.assertEqual(result["verification_strength"], "transport_only")
        self.assertFalse(result["ok"])

    def test_vacuum_return_requires_returning_docked_or_charging_state(self) -> None:
        snapshots = iter((
            snapshot("vacuum.andrey", "cleaning"),
            snapshot("vacuum.andrey", "cleaning"),
            snapshot("vacuum.andrey", "returning"),
        ))
        with mock.patch.object(ha_read, "load_config", return_value=config()):
            result, exit_code = control.execute(
                "vacuum.andrey", "return_home",
                snapshot_reader=lambda _command: (next(snapshots), 0),
                service_caller=lambda *_args: None,
                sleeper=lambda _seconds: None,
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["after_state"], "returning")
        self.assertEqual(result["verification_strength"], "physical_state")


if __name__ == "__main__":
    unittest.main()
