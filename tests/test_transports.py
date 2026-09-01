from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import alice_skill_gateway as alice
import local_chat_gateway as local


class TransportTests(unittest.TestCase):
    def test_local_chat_calls_injected_single_answerer(self) -> None:
        calls = []
        app = local.ChatApplication(
            answerer=lambda question, context, history: calls.append((question, context, history)) or "ok",
            context_factory=lambda: {"mode": "read_only"}, clock=lambda: 1.0,
        )
        answer = app.answer("a" * 43, "как зеркало?")
        self.assertEqual(answer, "ok")
        self.assertEqual(calls[0][0], "как зеркало?")
        self.assertEqual(calls[0][1]["transport"], "local_chat")

    def test_alice_calls_same_natural_answer_shape(self) -> None:
        config = alice.GatewayConfig("s" * 40, "skill-id-123", ("owner-id-123",))
        calls = []
        app = alice.SkillApplication(
            config, answerer=lambda q, c, h: calls.append((q, c, h)) or "Зеркало выключено.",
            context={"mode": "read_only"},
        )
        request = {
            "version": "1.0",
            "request": {"type": "SimpleUtterance", "original_utterance": "как зеркало", "command": "как зеркало"},
            "session": {"session_id": "session-id-123", "message_id": 0, "new": True, "skill_id": "skill-id-123", "user": {"user_id": "owner-id-123"}},
        }
        response, route = app.process(request)
        self.assertEqual(route, "read_only_conversation")
        self.assertEqual(response["response"]["text"], "Зеркало выключено.")
        self.assertEqual(calls[0][0], "как зеркало")


if __name__ == "__main__":
    unittest.main()
