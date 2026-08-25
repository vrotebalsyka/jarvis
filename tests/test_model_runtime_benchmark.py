from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock


SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import benchmark_model_runtime as benchmark  # noqa: E402


class ModelRuntimeBenchmarkTests(unittest.TestCase):
    def test_benchmark_scope_is_local_bounded_and_complete(self) -> None:
        self.assertEqual(
            benchmark.MODELS,
            ("home-butler:latest", "qwen3.5:4b-q4_K_M"),
        )
        self.assertEqual(
            benchmark.CONTEXT_WINDOWS,
            (8_192, 16_384, 32_768, 65_536),
        )
        self.assertLessEqual(benchmark.MAX_MEMORY_PROBE_CHARS, 180_000)

    def test_payload_has_no_action_surface(self) -> None:
        payload = benchmark.chat_payload(
            benchmark.MODELS[0],
            benchmark.CONTEXT_WINDOWS[0],
            [{"role": "user", "content": "test"}],
            output_limit=64,
            tools=[benchmark.READ_TOOL],
        )
        self.assertEqual(payload["options"]["num_ctx"], 8_192)
        self.assertEqual(payload["options"]["num_predict"], 64)
        self.assertEqual(
            payload["tools"][0]["function"]["name"],
            "ha_get_device_details",
        )
        rendered = repr(payload)
        self.assertNotIn("ha_call_service", rendered)
        self.assertNotIn("shell", rendered.casefold())
        self.assertNotIn("token", rendered.casefold())

    def test_unlisted_model_context_and_output_are_rejected(self) -> None:
        with self.assertRaises(benchmark.BenchmarkError):
            benchmark.chat_payload(
                "cloud-model",
                8_192,
                [],
                output_limit=64,
            )
        with self.assertRaises(benchmark.BenchmarkError):
            benchmark.options(4_096, 64)
        with self.assertRaises(benchmark.BenchmarkError):
            benchmark.options(8_192, 513)

    def test_memory_probe_is_bounded_and_keeps_early_marker(self) -> None:
        marker, text = benchmark.memory_probe_text(65_536)
        self.assertIn(marker, text[:100])
        self.assertLessEqual(len(text), benchmark.MAX_MEMORY_PROBE_CHARS + 100)

    def test_percentile_is_deterministic(self) -> None:
        self.assertEqual(benchmark.percentile([1.0, 2.0, 3.0], 0.50), 2.0)
        self.assertEqual(benchmark.percentile([1.0, 2.0, 3.0], 0.95), 3.0)
        self.assertIsNone(benchmark.percentile([], 0.95))

    def test_entity_resolution_requires_the_named_physical_device(self) -> None:
        valid_response = {
            "message": {
                "tool_calls": [{
                    "function": {
                        "name": "ha_get_device_details",
                        "arguments": {"query": "посудомойка"},
                    }
                }]
            }
        }
        with mock.patch.object(
            benchmark,
            "_call",
            return_value=(valid_response, {"wall_seconds": 0.1}),
        ):
            result = benchmark._tool_probe(object(), benchmark.MODELS[0], 8_192)
        self.assertTrue(result["tool_call_passed"])
        self.assertTrue(result["entity_resolution_passed"])

        wrong_device = {
            "message": {
                "tool_calls": [{
                    "function": {
                        "name": "ha_get_device_details",
                        "arguments": {"query": "зеркало"},
                    }
                }]
            }
        }
        with mock.patch.object(
            benchmark,
            "_call",
            return_value=(wrong_device, {"wall_seconds": 0.1}),
        ):
            result = benchmark._tool_probe(object(), benchmark.MODELS[0], 8_192)
        self.assertTrue(result["tool_call_passed"])
        self.assertFalse(result["entity_resolution_passed"])


if __name__ == "__main__":
    unittest.main()
