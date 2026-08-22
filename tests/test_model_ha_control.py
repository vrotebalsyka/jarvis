#!/usr/bin/env python3
"""Offline proof that the model emits exact switch/button/light control arguments."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import model_ha_control as proof  # noqa: E402
from ollama_endpoint import OllamaEndpoint  # noqa: E402


class ModelControlProofTests(unittest.TestCase):
    def test_rejects_changed_or_wrong_tool_call(self) -> None:
        for call in (
            {"function": {"name": "ha_call_service", "arguments": {}}},
            {
                "function": {
                    "name": proof.TOOL_NAME,
                    "arguments": {"entity_id": "switch.other", "action": "turn_on"},
                }
            },
        ):
            with self.subTest(call=call), self.assertRaises(proof.ControlProofError):
                proof.extract_exact_call(
                    {"message": {"tool_calls": [call]}},
                    "switch.kavidor_switch_1",
                    "turn_on",
                )

    def test_exact_model_call_is_required_before_control_executor(self) -> None:
        endpoint = OllamaEndpoint("http://172.27.192.1:11434", "172.27.192.1", 11434)
        calls: list[tuple[str, str]] = []

        def fake_ollama(_endpoint, path, payload):
            self.assertEqual(path, "/api/chat")
            self.assertEqual(payload["options"]["num_ctx"], 2048)
            self.assertEqual(payload["keep_alive"], "24h")
            schema = payload["tools"][0]["function"]["parameters"]
            self.assertEqual(schema["properties"]["entity_id"]["enum"], ["switch.kavidor_switch_1"])
            self.assertEqual(schema["properties"]["action"]["enum"], ["turn_on"])
            return {
                "message": {
                    "tool_calls": [
                        {
                            "id": "call-control",
                            "function": {
                                "name": proof.TOOL_NAME,
                                "arguments": {
                                    "entity_id": "switch.kavidor_switch_1",
                                    "action": "turn_on",
                                },
                            },
                        }
                    ]
                }
            }

        result = proof.run_control_proof(
            "switch.kavidor_switch_1",
            "turn_on",
            endpoint_loader=lambda: endpoint,
            ollama_call=fake_ollama,
            control_executor=lambda entity_id, action: (
                calls.append((entity_id, action))
                or {
                    "ok": True,
                    "status": "verified",
                    "entity_id": entity_id,
                    "action": action,
                    "service_calls": 1,
                },
                0,
            ),
        )
        self.assertTrue(result["tool_call_verified"])
        self.assertEqual(calls, [("switch.kavidor_switch_1", "turn_on")])
        self.assertEqual(result["control_result"]["service_calls"], 1)

    def test_parameter_value_is_part_of_the_exact_model_contract(self) -> None:
        endpoint = OllamaEndpoint("http://172.27.192.1:11434", "172.27.192.1", 11434)
        calls = []

        def fake_ollama(_endpoint, _path, payload):
            schema = payload["tools"][0]["function"]["parameters"]
            self.assertEqual(schema["properties"]["value"]["enum"], [5.0])
            self.assertIn("value", schema["required"])
            return {
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": proof.TOOL_NAME,
                            "arguments": {
                                "entity_id": "number.andrey_volume",
                                "action": "set_value",
                                "value": 5.0,
                            },
                        }
                    }]
                }
            }

        result = proof.run_control_proof(
            "number.andrey_volume",
            "set_value",
            5.0,
            endpoint_loader=lambda: endpoint,
            ollama_call=fake_ollama,
            control_executor=lambda entity_id, action, value: (
                calls.append((entity_id, action, value)) or {
                    "status": "verified",
                    "service_calls": 1,
                },
                0,
            ),
        )
        self.assertTrue(result["tool_call_verified"])
        self.assertEqual(calls, [("number.andrey_volume", "set_value", 5.0)])


if __name__ == "__main__":
    unittest.main()
