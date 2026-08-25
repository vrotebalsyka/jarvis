#!/usr/bin/env python3
"""Integration contracts for bounded natural chat facades."""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import alice_skill_gateway  # noqa: E402
import local_chat_gateway  # noqa: E402
import owner_chat  # noqa: E402


class NaturalChatFacadeTests(unittest.TestCase):
    def test_natural_ha_answer_precedes_compatibility_router(self) -> None:
        natural = mock.Mock(return_value="У Андрея заряд восемьдесят процентов.")
        fallback = mock.Mock(return_value="legacy")
        result = owner_chat.answer_natural(
            "А батарея у него?",
            {"memory": {"conversation_summary": {"device": "Андрей"}}},
            [{"role": "user", "content": "Что с роботом Андреем?"}],
            natural_agent=natural,
            fallback_answerer=fallback,
        )
        self.assertIn("Андрея", result)
        natural.assert_called_once()
        fallback.assert_not_called()

    def test_slash_command_keeps_compatibility_boundary(self) -> None:
        natural = mock.Mock(return_value="unexpected")
        fallback = mock.Mock(return_value="health")
        result = owner_chat.answer_natural(
            "/health", {}, [], natural_agent=natural, fallback_answerer=fallback
        )
        self.assertEqual(result, "health")
        natural.assert_not_called()

    def test_conversation_falls_back_after_model_classification(self) -> None:
        natural = mock.Mock(return_value=None)
        fallback = mock.Mock(return_value="Привет.")
        result = owner_chat.answer_natural(
            "Привет", {}, [], natural_agent=natural, fallback_answerer=fallback
        )
        self.assertEqual(result, "Привет.")
        natural.assert_called_once()
        fallback.assert_called_once()

    def test_transport_defaults_use_natural_facades(self) -> None:
        local_default = inspect.signature(
            local_chat_gateway.ChatApplication.__init__
        ).parameters["answerer"].default
        alice_default = inspect.signature(
            alice_skill_gateway.SkillApplication.__init__
        ).parameters["answerer"].default
        self.assertIs(local_default, owner_chat.answer_natural)
        self.assertIs(alice_default, alice_skill_gateway.natural_voice_answer)

    def test_voice_facade_preserves_fast_fallback(self) -> None:
        with mock.patch.object(
            alice_skill_gateway.owner_chat,
            "answer_natural",
            return_value="голос",
        ) as natural:
            self.assertEqual(
                alice_skill_gateway.natural_voice_answer("привет", {}, []),
                "голос",
            )
        self.assertIs(
            natural.call_args.kwargs["fallback_answerer"],
            alice_skill_gateway.fast_model_answer,
        )
        self.assertEqual(natural.call_args.kwargs["runtime_profile"], "voice_fast")


if __name__ == "__main__":
    unittest.main()
