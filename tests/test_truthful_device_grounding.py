#!/usr/bin/env python3
"""Regression tests for owner-reported hallucinations and clarification loss."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import model_ha_proof as proof  # noqa: E402


class TruthfulDeviceGroundingTests(unittest.TestCase):
    @staticmethod
    def andrey_observation() -> dict:
        path = PROJECT_DIR / "tests" / "fixtures" / "stage67" / "andrey_sanitized.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_russian_case_forms_resolve_to_registry_name(self) -> None:
        for value in (
            "Андрей", "Андрея", "Андрею", "Андреем", "об Андрее",
            "что с роботом Андреем", "заряд робота Андрея",
        ):
            with self.subTest(value=value):
                self.assertEqual(proof.normalize_device_query(value), "Андрей")

    def test_andrey_correction_or_multi_device_phrase_does_not_silently_collapse(self) -> None:
        for value in ("не Андрей, а второй робот", "Андрей и Roborock"):
            with self.subTest(value=value):
                self.assertNotEqual(proof.normalize_device_query(value), "Андрей")

    def test_andrey_summary_copies_real_status_battery_and_resources(self) -> None:
        answer = proof.render_device_observation(
            self.andrey_observation(), "Что с роботом Андреем?"
        )
        self.assertIn("на док-станции и заряжается", answer)
        self.assertIn("заряд 100%", answer)
        for value in ("13%", "56%", "72%"):
            self.assertIn(value, answer)
        for forbidden in (
            "ниже 20", "в движении", "полной мощности", "сброс связи",
            "зависание", "разных зонах",
        ):
            self.assertNotIn(forbidden, answer.casefold())
        self.assertIn("недоступны 2 функции", answer.casefold())
        self.assertIn("причина по текущим данным не подтверждена", answer)

    def test_specific_battery_question_is_short_and_exact(self) -> None:
        answer = proof.render_device_observation(
            self.andrey_observation(), "Сколько заряда у Андрея?"
        )
        self.assertEqual(answer, "Андрей: заряд 100%.")

    def test_battery_control_timestamp_cannot_replace_numeric_measurement(self) -> None:
        observation = {
            "display_name": "Андрей",
            "physical_availability": "available",
            "features": [
                {
                    "human_name": "Возврат на зарядку",
                    "component": "battery-start_charge",
                    "semantic_role": "control",
                    "domain": "button",
                    "availability": "available",
                    "state": {"kind": "text", "value": "2026-08-11T17:28:09+00:00"},
                },
                {
                    "human_name": "Battery Level",
                    "component": "battery-battery_level",
                    "semantic_role": "measurement",
                    "domain": "sensor",
                    "availability": "available",
                    "state": {"kind": "number", "value": 100.0},
                },
                {
                    "human_name": "Статус",
                    "component": "vacuum-status",
                    "semantic_role": "measurement",
                    "domain": "sensor",
                    "availability": "available",
                    "state": {"kind": "text", "value": "charging"},
                },
            ],
        }
        answer = proof.render_device_observation(observation, "Что с Андреем?")
        self.assertIn("на док-станции и заряжается", answer)
        self.assertIn("заряд 100%", answer)
        self.assertNotIn("2026", answer)

    def test_accepted_action_is_never_rendered_as_success(self) -> None:
        answer = proof.render_action_receipt({
            "status": "partially_verified",
            "device_name": "Dishwasher",
            "feature_name": "Старт",
            "service_calls": 1,
            "steps": [{
                "adapter_status": "accepted_unverified",
                "device_name": "Dishwasher",
                "feature_name": "Старт",
                "service_calls": 1,
            }],
        })
        self.assertIn("физический результат не подтверждён", answer)
        self.assertNotIn("запустил", answer.casefold())
        self.assertNotIn("готово", answer.casefold())


    def test_verified_training_corpus_is_valid_and_covers_owner_failures(self) -> None:
        path = PROJECT_DIR / "training" / "stage67_verified_examples.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertGreaterEqual(len(rows), 20)
        self.assertEqual(len({item["id"] for item in rows}), len(rows))
        rendered = json.dumps(rows, ensure_ascii=False)
        for marker in ("charging", "battery", "accepted_unverified", "rinse_aid", "conditional"):
            self.assertIn(marker, rendered)

    def test_model_tool_query_is_normalized_before_device_search(self) -> None:
        document = {
            "message": {
                "tool_calls": [{
                    "function": {
                        "name": "ha_find_devices",
                        "arguments": {"query": "роботом Андреем"},
                    }
                }]
            }
        }
        proof.postprocess_model_document({"messages": []}, document)
        arguments = document["message"]["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(arguments["query"], "Андрей")

    def test_short_clarification_keeps_previous_explicit_action(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "kind": {}, "device_query": {}, "requested_action": {},
                "requested_value": {}, "uses_coreference": {},
                "separate_confirmation": {},
            },
        }
        payload = {
            "format": schema,
            "messages": [
                {"role": "user", "content": "включи dishwasher"},
                {
                    "role": "assistant",
                    "content": (
                        "Нашёл несколько функций. Назовите одну; команда пока "
                        "не отправлена, ничего не менял."
                    ),
                },
                {"role": "user", "content": "CURRENT_USER=Dishwasher Питание"},
            ],
        }
        document = {
            "message": {
                "content": json.dumps({
                    "kind": "conversation",
                    "device_query": None,
                    "requested_action": None,
                    "requested_value": None,
                    "uses_coreference": False,
                    "separate_confirmation": False,
                }, ensure_ascii=False)
            }
        }
        proof.postprocess_model_document(payload, document)
        parsed = json.loads(document["message"]["content"])
        self.assertEqual(parsed["kind"], "ha_action")
        self.assertEqual(parsed["device_query"], "Dishwasher Питание")
        self.assertTrue(parsed["uses_coreference"])


if __name__ == "__main__":
    unittest.main()
