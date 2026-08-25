#!/usr/bin/env python3
"""Contracts for secret-safe per-turn observability."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import context_builder  # noqa: E402
import local_chat_gateway  # noqa: E402
import memory_store  # noqa: E402
import turn_observability as observability  # noqa: E402


class TurnObservabilityTests(unittest.TestCase):
    def _store(self, directory: Path) -> memory_store.MemoryStore:
        directory.chmod(0o700)
        return memory_store.MemoryStore(directory / "memory.db")

    def test_complete_trace_is_bounded_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            token = observability.begin_turn(
                owner_scope=memory_store.PRIMARY_OWNER_SCOPE,
                transport="local_chat",
                session_key="a" * 32,
            )
            observability.observe_memory_context({
                "retrieval_trace_id": "b" * 32,
                "relevant_memories": [{"id": "c" * 32, "text": "private"}],
                "active_goals": [],
            })
            observability.record_route("ha_read")
            observability.record_policy("dialogue", "qwen3.5:4b-q4_K_M")
            observability.record_model_call(
                {"model": "qwen3.5:4b-q4_K_M"},
                {"prompt_eval_count": 120, "eval_count": 17},
                path="/api/chat",
                latency_ms=25,
                status="completed",
            )
            observability.record_tool_call(
                "ha_get_device_details",
                latency_ms=8,
                policy_result="allowed",
                result_status="healthy",
            )
            observability.record_action("ha_control.turn_on")
            observability.record_verification("state_matches_expected")
            document = observability.finish_turn(
                token, final_disposition="completed"
            )
            self.assertIsNotNone(document)
            trace_id = store.write_agent_turn_trace(document or {})
            stored = store.read_agent_turn_trace(
                trace_id, memory_store.PRIMARY_OWNER_SCOPE
            )
            self.assertEqual(stored["route"], "ha_read")
            self.assertEqual(stored["token_counts"], {"input": 120, "output": 17})
            self.assertEqual(stored["retrieved_memory_ids"], ["c" * 32])
            self.assertEqual(stored["tool_calls"][0]["name"], "ha_get_device_details")
            self.assertNotIn("private", str(stored))

    def test_invalid_codes_are_redacted_instead_of_stored(self) -> None:
        token = observability.begin_turn(
            owner_scope=memory_store.PRIMARY_OWNER_SCOPE,
            transport="alice",
            session_key="d" * 32,
        )
        observability.record_policy("dialogue", "Bearer dangerous value")
        observability.record_tool_call(
            "tool with raw arguments",
            latency_ms=1,
            policy_result="allowed",
            result_status="healthy",
        )
        document = observability.finish_turn(token, final_disposition="completed")
        self.assertEqual(document["models"], ["unknown"])
        self.assertEqual(document["tool_calls"][0]["name"], "unknown")
        self.assertIsNone(observability.current_trace_id())

    def test_local_chat_persists_one_trace_for_the_agent_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            conversation = context_builder.ConversationMemory(store)

            def answerer(_question, _context, _history):
                observability.record_route("general")
                observability.record_policy("dialogue", "qwen3.5:4b-q4_K_M")
                observability.record_model_call(
                    {"model": "qwen3.5:4b-q4_K_M"},
                    {"prompt_eval_count": 12, "eval_count": 4},
                    path="/api/chat",
                    latency_ms=4,
                    status="completed",
                )
                return "Проверка завершена."

            application = local_chat_gateway.ChatApplication(
                answerer=answerer,
                context_factory=lambda: {},
                conversation_memory=conversation,
            )
            self.assertEqual(
                application.answer("A" * 43, "Что происходит?"),
                "Проверка завершена.",
            )
            traces = store.recent_agent_turn_traces(
                memory_store.PRIMARY_OWNER_SCOPE
            )
            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0]["transport"], "local_chat")
            self.assertEqual(traces[0]["route"], "general")
            self.assertTrue(traces[0]["retrieval_trace_id"])
            self.assertEqual(traces[0]["final_disposition"], "completed")


if __name__ == "__main__":
    unittest.main()
