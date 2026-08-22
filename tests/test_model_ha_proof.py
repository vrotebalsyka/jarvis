#!/usr/bin/env python3
"""Offline tests for the fail-closed Ollama/Home Assistant proof."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import model_ha_proof as proof  # noqa: E402
from ollama_endpoint import OllamaEndpoint  # noqa: E402


ENTITY = {
    "entity_id": "binary_sensor.motion",
    "state_kind": "enum",
    "state_value": "off",
    "source_last_updated_at": "2026-08-02T11:59:00+00:00",
    "observed_at": "2026-08-02T12:00:00+00:00",
}


class ModelHaProofTests(unittest.TestCase):
    def test_voice_read_proof_is_bounded_and_model_consumes_exact_fact(self) -> None:
        endpoint = OllamaEndpoint("http://172.27.192.1:11434", "172.27.192.1", 11434)
        expected = {**ENTITY, "source": proof.SOURCE}
        calls: list[dict[str, object]] = []

        def fake_call(_endpoint, path, payload):
            self.assertEqual(path, "/api/chat")
            calls.append(payload)
            if len(calls) == 1:
                return {
                    "message": {
                        "tool_calls": [{
                            "id": "call-voice",
                            "function": {"name": proof.TOOL_NAME, "arguments": {}},
                        }]
                    }
                }
            return {
                "message": {
                    "content": (
                        "Home Assistant на связи. Из 1 сущности доступна 1, "
                        "недоступно 0; ничего не менял."
                    )
                }
            }

        result = proof.run_voice_read_proof(
            endpoint_loader=lambda: endpoint,
            ollama_call=fake_call,
            ollama_get=lambda *_args: {
                "models": [{
                    "name": "home-butler:latest",
                    "size": 100,
                    "size_vram": 100,
                    "context_length": 2048,
                }]
            },
            snapshot_reader=lambda _command: (
                {
                    "status": "healthy",
                    "entity_count": 1,
                    "available_entity_count": 1,
                    "unavailable_entity_count": 0,
                    "entities": [ENTITY],
                },
                0,
            ),
        )
        self.assertTrue(result["verified"])
        self.assertEqual(result["proof_mode"], "voice_bounded")
        self.assertEqual(result["consumed_fact"], expected)
        self.assertEqual(calls[0]["options"]["num_predict"], 48)
        self.assertEqual(calls[1]["options"]["num_predict"], 64)
        tool_result = json.loads(calls[1]["messages"][3]["content"])
        self.assertEqual(tool_result["proof_entity"], expected)
        self.assertEqual(tool_result["home_assistant"]["entity_count"], 1)
        self.assertIn("Home Assistant на связи", result["spoken_answer"])
        self.assertEqual(result["spoken_answer_source"], "model")

    def test_voice_summary_rejects_changed_counts_and_tool_syntax(self) -> None:
        facts = {
            "status": "stale_data",
            "entity_count": 198,
            "available_entity_count": 139,
            "unavailable_entity_count": 59,
            "service_calls": 0,
        }
        with self.assertRaises(proof.ProofError):
            proof.validate_voice_summary(
                "Home Assistant на связи. Из 198 доступны 140, 58 недоступны; ничего не менял.",
                facts,
            )

    def test_voice_summary_uses_natural_verified_fallback_for_omitted_count(self) -> None:
        facts = {
            "status": "stale_data",
            "entity_count": 198,
            "available_entity_count": 139,
            "unavailable_entity_count": 59,
            "service_calls": 0,
        }
        summary, source = proof.safe_voice_summary(
            "Home Assistant на связи; всего 198 сущностей, 139 доступны; ничего не менял.",
            facts,
        )
        self.assertEqual(source, "verified_fallback")
        self.assertEqual(
            summary,
            "Home Assistant на связи. В нём 198 сущностей: "
            "139 доступны, 59 недоступны. Ничего не менял.",
        )
        with self.assertRaises(proof.ProofError):
            proof.validate_voice_summary(
                "Home Assistant на связи. Из 198 доступны 139, 59 недоступны; "
                "ha_get_snapshot, ничего не менял.",
                facts,
            )

    def test_voice_summary_distinguishes_entities_physical_and_network_devices(self) -> None:
        facts = proof._voice_summary_facts(
            {
                "status": "healthy",
                "entity_count": 221,
                "available_entity_count": 160,
                "unavailable_entity_count": 61,
            },
            {
                "physical_device_count": 41,
                "network_device_count": 22,
                "device_network_binding_count": 9,
            },
        )
        summary, source = proof.safe_voice_summary("неполный ответ", facts)
        self.assertEqual(source, "verified_fallback")
        self.assertIn("221 сущностей", summary)
        self.assertIn("Физических устройств 41", summary)
        self.assertIn("в сети сейчас 22", summary)
        self.assertIn("с сетью сопоставлено 9", summary)

    def test_gpu_evidence_can_select_the_isolated_voice_alias(self) -> None:
        document = {
            "models": [
                {
                    "name": "home-butler:latest",
                    "size": 200,
                    "size_vram": 200,
                    "context_length": 8192,
                },
                {
                    "name": "home-butler-voice:latest",
                    "size": 100,
                    "size_vram": 100,
                    "context_length": 2048,
                },
            ]
        }
        evidence = proof.gpu_evidence(
            document, expected_model="home-butler-voice"
        )
        self.assertEqual(evidence["model"], "home-butler-voice:latest")
        self.assertEqual(evidence["context_length"], 2048)
        self.assertTrue(evidence["fully_on_gpu"])
        with self.assertRaises(proof.ProofError):
            proof.gpu_evidence(document, expected_model="attacker-model")

    def test_selects_first_safe_available_entity(self) -> None:
        snapshot = {
            "status": "stale_data",
            "entities": [
                {
                    "entity_id": "sensor.bad",
                    "state_kind": "unavailable",
                    "state_value": None,
                    "source_last_updated_at": "2026-08-02T11:00:00+00:00",
                    "observed_at": "2026-08-02T12:00:00+00:00",
                },
                ENTITY,
            ],
        }
        self.assertEqual(
            proof.select_proof_entity(snapshot),
            {**ENTITY, "source": proof.SOURCE},
        )

    def test_rejects_a_hallucinated_model_fact(self) -> None:
        expected = {**ENTITY, "source": proof.SOURCE}
        actual = {**expected, "entity_id": "binary_sensor.invented"}
        with self.assertRaises(proof.ProofError):
            proof.validate_model_fact(actual, expected)

    def test_rejects_extra_model_fields(self) -> None:
        expected = {**ENTITY, "source": proof.SOURCE}
        with self.assertRaises(proof.ProofError):
            proof.validate_model_fact({**expected, "guess": True}, expected)

    def test_rejects_wrong_or_mutating_tool(self) -> None:
        for name in ("ha_call_service", "restart_router"):
            with self.subTest(name=name), self.assertRaises(proof.ProofError):
                proof.extract_tool_call(
                    {
                        "message": {
                            "tool_calls": [
                                {"function": {"name": name, "arguments": {}}}
                            ]
                        }
                    }
                )

    def test_run_proof_requires_exact_tool_and_exact_fact(self) -> None:
        endpoint = OllamaEndpoint("http://172.27.192.1:11434", "172.27.192.1", 11434)
        expected = {**ENTITY, "source": proof.SOURCE}
        calls: list[dict[str, object]] = []

        def fake_call(_endpoint, path, payload):
            self.assertEqual(path, "/api/chat")
            calls.append(payload)
            if len(calls) == 1:
                return {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-proof",
                                "function": {"name": proof.TOOL_NAME, "arguments": {}},
                            }
                        ],
                    }
                }
            return {"message": {"content": json.dumps(expected)}}

        result = proof.run_proof(
            require_gpu=True,
            endpoint_loader=lambda: endpoint,
            ollama_call=fake_call,
            ollama_get=lambda *_args: {
                "models": [
                    {
                        "name": "home-butler:latest",
                        "size": 100,
                        "size_vram": 100,
                        "context_length": 8192,
                    }
                ]
            },
            snapshot_reader=lambda _command: (
                {
                    "status": "healthy",
                    "available_entity_count": 1,
                    "unavailable_entity_count": 0,
                    "entities": [ENTITY],
                },
                0,
            ),
        )

        self.assertTrue(result["verified"])
        self.assertEqual(result["tool_call"]["name"], proof.TOOL_NAME)
        self.assertEqual(result["model_fact"], expected)
        self.assertEqual(result["home_assistant"]["service_calls"], 0)
        self.assertTrue(result["accelerator"]["fully_on_gpu"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call["options"]["num_ctx"] == 2048 for call in calls))
        self.assertTrue(all(call["keep_alive"] == "24h" for call in calls))
        self.assertEqual(calls[1]["messages"][3]["role"], "tool")
        self.assertEqual(json.loads(calls[1]["messages"][3]["content"]), expected)


if __name__ == "__main__":
    unittest.main()
