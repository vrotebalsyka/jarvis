from __future__ import annotations

import json
import sys
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
import bounded_ha_agent as agent
import owner_chat
import stage71_fixtures as fixtures


class OwnerAnswerUnicodeTests(unittest.TestCase):
    def test_safe_unicode_is_preserved_and_utf8_serializable(self):
        for answer in (
            "Привет! 👋", "Хорошего дня! 👋🏽", "Здравствуйте! 👩‍💻",
            "Семья 👨‍👩‍👧‍👦", "Флаг 🇷🇺", "Иероглиф 𠮷", "Привет! ☀️",
        ):
            with self.subTest(answer=answer):
                actual = agent.validate_owner_answer(answer)
                self.assertEqual(actual, answer)
                encoded = json.dumps({"answer": actual}, ensure_ascii=False).encode("utf-8")
                self.assertEqual(json.loads(encoded)["answer"], answer)

    def test_raw_greeting_uses_owner_path_without_ha_read(self):
        reply = "Привет! 👋 Чем могу помочь?"
        model = Mock(return_value={"message": {"content": reply}})
        reader = Mock(side_effect=AssertionError("Greeting must not read HA"))
        responder = partial(
            agent.respond, inventory_loader=fixtures.graph, snapshot_reader=reader,
            endpoint_loader=lambda: None, ollama_call=model, trace_sink=None,
        )
        with patch.object(agent, "respond", responder):
            answer = owner_chat.answer_natural("привет", owner_chat.startup_context(), [])
        self.assertEqual(answer, reply)
        model.assert_called_once()
        reader.assert_not_called()

    def test_existing_secret_and_identifier_checks_survive_unicode_decoration(self):
        # Synthetic identifiers/credential-shaped data only, never real HA data.
        examples = (
            "sensor.fixture_status", "device_id=fixture", "f" * 32,
            "/api/services/light/turn_on", "http://example.test",
            "адрес 192.168.1.2", "eyJ" + "A" * 20 + "." + "B" * 20 + "." + "C" * 20,
        )
        decorations = (
            lambda text: text,
            lambda text: "👋 " + text,
            lambda text: "👋".join(text),
            lambda text: "\u200d".join(text),
            lambda text: "\ufe0f".join(text),
            lambda text: "".join(chr(ord(ch) + 0xFEE0) if "!" <= ch <= "~" else ch for ch in text),
        )
        for example_index, example in enumerate(examples):
            for decoration_index, decorate in enumerate(decorations):
                with self.subTest(example=example_index, decoration=decoration_index):
                    with self.assertRaises(agent.BoundedAgentError):
                        agent.validate_owner_answer(decorate(example))

    def test_supplementary_compatibility_letters_do_not_hide_an_identifier(self):
        stylized = "".join(chr(0x1D41A + ord(ch) - ord("a")) if "a" <= ch <= "z" else ch
                           for ch in "sensor.fixture_status")
        with self.assertRaises(agent.BoundedAgentError):
            agent.validate_owner_answer(stylized)

    def test_invalid_surrogates_and_control_characters_are_rejected(self):
        for index, character in enumerate(("\ud800", "\udfff", "\x00", "\x1b")):
            with self.subTest(index=index):
                with self.assertRaises(agent.BoundedAgentError):
                    agent.validate_owner_answer("Привет" + character)

    def test_answer_size_limit_and_whitespace_contract_are_preserved(self):
        for answer in ("", " \n\t", "я" * (agent.MAX_OWNER_ANSWER_CHARS + 1),
                       "👋" * (agent.MAX_OWNER_ANSWER_CHARS + 1)):
            with self.assertRaises(agent.BoundedAgentError):
                agent.validate_owner_answer(answer)
        self.assertEqual(agent.validate_owner_answer("  Привет!\n\t👋  "), "Привет! 👋")

    def test_values_units_and_unknown_markers_are_not_rewritten(self):
        answer = "Ресурс щётки: 43 %. Температура: 21,5 °C 🌡️. Ошибка: неизвестно ❓."
        self.assertEqual(agent.validate_owner_answer(answer), answer)


if __name__ == "__main__":
    unittest.main()
