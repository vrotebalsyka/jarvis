#!/usr/bin/env python3
"""Contracts for the bounded real Home Assistant GPU stress test."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import home_stress_test as stress  # noqa: E402


def snapshot() -> dict[str, object]:
    return {
        "status": "stale_data",
        "entity_count": 2,
        "available_entity_count": 2,
        "unavailable_entity_count": 0,
        "entities": [
            {
                "entity_id": "switch.zerkalo",
                "state_kind": "enum",
                "state_value": "on",
                "source_last_updated_at": "2026-08-11T10:00:00+00:00",
            },
            {
                "entity_id": "sensor.room",
                "state_kind": "number",
                "state_value": 24.0,
                "source_last_updated_at": "2026-08-11T10:00:00+00:00",
            },
        ],
    }


class HomeStressTestTests(unittest.TestCase):
    def _lock(self, directory: str) -> int:
        return os.open(Path(directory) / "lock", os.O_RDWR | os.O_CREAT, 0o600)

    def test_announces_each_action_then_restores_before_gpu_analysis(self) -> None:
        events: list[tuple[str, str]] = []
        moment = [0.0]

        def sleeper(seconds: float) -> None:
            moment[0] += seconds

        def controller(_entity_id: str, action: str):
            events.append(("action", action))
            after = "off" if action == "turn_off" else "on"
            return ({"status": "verified", "after_state": after}, 0)

        def model_call(_endpoint, path: str, payload: dict[str, object]):
            self.assertEqual(path, "/api/generate")
            self.assertEqual(payload["model"], "home-butler")
            moment[0] += 70.0
            events.append(("model", "analyze"))
            return {"response": "Подробный диагностический анализ.", "eval_count": 384}

        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "home_stress_test.model_ha_proof.get_ollama",
            return_value={
                "models": [{
                    "name": "home-butler:latest",
                    "size": 100,
                    "size_vram": 100,
                    "context_length": 8192,
                }]
            },
        ):
            result = stress.run_test(
                1,
                "switch.zerkalo",
                "зеркало",
                snapshot_reader=lambda _action: (snapshot(), 0),
                config_loader=object,
                tts_caller=lambda _config, _speaker, message: events.append(("tts", message)),
                speaker_reader=lambda _config, _speaker: {
                    "last_updated": "before",
                    "volume_ready": True,
                    "muted": False,
                },
                speaker_verifier=lambda _config, _speaker, _baseline: True,
                controller=controller,
                endpoint_loader=object,
                model_call=model_call,
                clock=lambda: moment[0],
                sleeper=sleeper,
                lock_opener=lambda: self._lock(temporary),
            )
        self.assertEqual([kind for kind, _value in events], [
            "tts", "action", "tts", "action", "model"
        ])
        self.assertIn("выключу зеркало", events[0][1])
        self.assertIn("включу зеркало", events[0][1])
        self.assertIn("включу зеркало", events[2][1])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["generated_tokens"], 384)
        self.assertEqual(result["accelerator"], "gpu")
        self.assertEqual(result["initial_state"], result["restored_state"])

    def test_unconfirmed_first_warning_prevents_every_device_action(self) -> None:
        controller = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(stress.StressTestError):
                stress.run_test(
                    1,
                    "switch.zerkalo",
                    "зеркало",
                    snapshot_reader=lambda _action: (snapshot(), 0),
                    config_loader=object,
                    tts_caller=lambda _config, _speaker, _message: None,
                    speaker_reader=lambda _config, _speaker: {
                        "last_updated": "before",
                        "volume_ready": True,
                        "muted": False,
                    },
                    speaker_verifier=lambda _config, _speaker, _baseline: False,
                    controller=controller,
                    lock_opener=lambda: self._lock(temporary),
                )
        controller.assert_not_called()

    def test_keyboard_interrupt_between_actions_restores_announced_original_state(self) -> None:
        current = {"value": "on"}
        actions: list[str] = []
        verifications = {"count": 0}

        def current_snapshot() -> dict[str, object]:
            document = snapshot()
            document["entities"][0]["state_value"] = current["value"]
            return document

        def controller(_entity_id: str, action: str):
            actions.append(action)
            current["value"] = "off" if action == "turn_off" else "on"
            return ({"status": "verified", "after_state": current["value"]}, 0)

        def verifier(_config, _speaker, _baseline):
            verifications["count"] += 1
            if verifications["count"] == 2:
                raise KeyboardInterrupt
            return True

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(KeyboardInterrupt):
                stress.run_test(
                    1,
                    "switch.zerkalo",
                    "зеркало",
                    snapshot_reader=lambda _action: (current_snapshot(), 0),
                    config_loader=object,
                    tts_caller=lambda _config, _speaker, _message: None,
                    speaker_reader=lambda _config, _speaker: {
                        "last_updated": "before",
                        "volume_ready": True,
                        "muted": False,
                    },
                    speaker_verifier=verifier,
                    controller=controller,
                    sleeper=lambda _seconds: None,
                    lock_opener=lambda: self._lock(temporary),
                )
        self.assertEqual(actions, ["turn_off", "turn_on"])
        self.assertEqual(current["value"], "on")

    def test_only_switch_and_light_and_one_to_ten_minutes_are_allowed(self) -> None:
        for minutes, entity_id in ((0, "switch.one"), (11, "light.one"), (1, "lock.door")):
            with self.assertRaises(stress.StressTestError):
                stress.run_test(
                    minutes,
                    entity_id,
                    "тест",
                    lock_opener=lambda: -1,
                )

    def test_relay_plan_excludes_my_pc_settings_and_duplicate_integrations(self) -> None:
        catalogue = {
            "control_entities": [
                {"entity_id": "switch.my_pc", "friendly_name": "my-pc", "available": True},
                {"entity_id": "switch.mirror_cloud", "friendly_name": "зеркало Switch 1", "available": True},
                {"entity_id": "switch.mirror_local", "friendly_name": "зеркало", "available": True},
                {"entity_id": "switch.lock", "friendly_name": "зеркало child lock", "available": True},
                {"entity_id": "switch.offline", "friendly_name": "кухня", "available": False},
            ]
        }
        physical = "a" * 64
        inventory = {
            "entities": [
                {"entity_id": "switch.my_pc", "platform": "wake_on_lan", "physical_device_hash": None},
                {"entity_id": "switch.mirror_cloud", "platform": "tuya", "physical_device_hash": physical},
                {"entity_id": "switch.mirror_local", "platform": "tuya_local", "physical_device_hash": physical},
                {"entity_id": "switch.lock", "platform": "tuya_local", "physical_device_hash": physical},
                {"entity_id": "switch.offline", "platform": "tuya_local", "physical_device_hash": "b" * 64},
            ]
        }
        self.assertEqual(
            stress.select_relay_targets(catalogue, inventory),
            [{"entity_id": "switch.mirror_local", "friendly_name": "зеркало"}],
        )

    def test_all_relays_are_announced_sequentially_and_restored_before_gpu(self) -> None:
        states = {"switch.one": "on", "switch.two": "off"}
        events: list[tuple[str, str]] = []
        moment = [0.0]

        def current_snapshot() -> dict[str, object]:
            entities = [
                {
                    "entity_id": entity_id,
                    "state_kind": "enum",
                    "state_value": state,
                    "source_last_updated_at": "2026-08-11T10:00:00+00:00",
                }
                for entity_id, state in states.items()
            ]
            return {
                "status": "healthy",
                "entity_count": len(entities),
                "available_entity_count": len(entities),
                "unavailable_entity_count": 0,
                "entities": entities,
            }

        def controller(entity_id: str, action: str):
            events.append(("action", f"{entity_id}:{action}"))
            states[entity_id] = "on" if action == "turn_on" else "off"
            return ({"status": "verified", "after_state": states[entity_id]}, 0)

        def sleeper(seconds: float) -> None:
            moment[0] += seconds

        def model_call(_endpoint, _path: str, _payload: dict[str, object]):
            events.append(("model", "analyze"))
            moment[0] += 70.0
            return {"response": "Диагностический анализ.", "eval_count": 384}

        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "home_stress_test.model_ha_proof.get_ollama",
            return_value={
                "models": [{
                    "name": "home-butler:latest",
                    "size": 100,
                    "size_vram": 100,
                    "context_length": 8192,
                }]
            },
        ):
            result = stress.run_all_relays_test(
                1,
                [
                    {"entity_id": "switch.one", "friendly_name": "первое реле"},
                    {"entity_id": "switch.two", "friendly_name": "второе реле"},
                ],
                snapshot_reader=lambda _action: (current_snapshot(), 0),
                config_loader=object,
                tts_caller=lambda _config, _speaker, message: events.append(("tts", message)),
                speaker_reader=lambda _config, _speaker: {
                    "last_updated": "before",
                    "volume_ready": True,
                    "muted": False,
                },
                speaker_verifier=lambda _config, _speaker, _baseline: True,
                controller=controller,
                endpoint_loader=object,
                model_call=model_call,
                clock=lambda: moment[0],
                sleeper=sleeper,
                lock_opener=lambda: self._lock(temporary),
            )
        self.assertEqual(states, {"switch.one": "on", "switch.two": "off"})
        self.assertEqual([kind for kind, _value in events], [
            "tts", "action", "tts", "action",
            "tts", "action", "tts", "action", "model",
        ])
        self.assertEqual(result["relay_count"], 2)
        self.assertEqual(result["service_calls"], 4)

    def test_all_relay_worker_rejects_my_pc_even_if_preselected(self) -> None:
        for target in (
            {"entity_id": "switch.my_pc", "friendly_name": "my-pc"},
            {"entity_id": "switch.renamed", "friendly_name": "MY PC"},
        ):
            with self.assertRaises(stress.StressTestError):
                stress.run_all_relays_test(
                    1,
                    [target],
                    lock_opener=lambda: -1,
                )


if __name__ == "__main__":
    unittest.main()
