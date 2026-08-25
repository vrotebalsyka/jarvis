#!/usr/bin/env python3
"""Contracts for real local and public-Alice multi-turn proof."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import alice_skill_gateway  # noqa: E402
import dialogue_qualification as proof  # noqa: E402


BOOT_ID = "01234567-89ab-cdef-0123-456789abcdef"
ANSWERS = [
    "Хорошо, я запомнил кодовое слово Аврора для этого разговора.",
    "Вы просили меня запомнить слово Аврора.",
    "Синий свет сильнее рассеивается в атмосфере, а на закате длинный путь оставляет красные оттенки.",
]


class DialogueQualificationTests(unittest.TestCase):
    def test_proof_uses_the_explicit_free_dialogue_route(self) -> None:
        self.assertTrue(all(prompt.startswith("/модель ") for prompt in proof.PROMPTS))

    def test_deferred_alice_task_and_result_are_parsed_without_answer_leakage(self) -> None:
        self.assertEqual(
            proof._deferred_task_id("Задача a1b2c3d4 сохранена и выполняется."),
            "a1b2c3d4",
        )
        self.assertEqual(
            proof._deferred_result("Задача a1b2c3d4: готовый ответ"),
            "готовый ответ",
        )

    def test_model_turn_timeout_allows_slow_local_gpu_inference(self) -> None:
        self.assertGreaterEqual(proof.MODEL_TURN_TIMEOUT_SECONDS, 90)

    def test_natural_answers_prove_history_and_no_fake_tool_claim(self) -> None:
        result = proof._validate_answers(ANSWERS)
        self.assertTrue(result["history_verified"])
        self.assertTrue(result["free_dialogue_verified"])
        self.assertTrue(result["fake_tool_claim_absent"])

    def test_template_or_missing_history_fails_closed(self) -> None:
        for answers in (
            [ANSWERS[0], "Не помню.", ANSWERS[2]],
            [ANSWERS[0], ANSWERS[1], "Я вызываю snapshot для ответа."],
        ):
            with self.subTest(answers=answers), self.assertRaises(
                proof.DialogueQualificationError
            ):
                proof._validate_answers(answers)

    def test_semantic_failures_have_secret_free_component_codes(self) -> None:
        self.assertEqual(
            proof.SAFE_FAILURE_CODES["public Alice dialogue history proof failed"],
            "alice_history",
        )
        self.assertEqual(
            proof.SAFE_FAILURE_CODES["local dialogue canned response proof failed"],
            "local_canned_response",
        )

    def test_failure_codes_never_include_exception_text(self) -> None:
        self.assertEqual(
            proof.SAFE_FAILURE_CODES["local dialogue proof failed"],
            "local_dialogue",
        )
        self.assertNotIn("answer", proof.SAFE_FAILURE_CODES.values())

    def test_run_once_persists_only_sanitized_current_boot_flags(self) -> None:
        config = alice_skill_gateway.GatewayConfig(
            "A" * 40, "skill-12345678", ("owner-12345678",)
        )
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir(mode=0o700)
            document = proof.run_once(
                config_loader=lambda: config,
                local_runner=lambda: list(ANSWERS),
                public_runner=lambda _config: list(ANSWERS),
                boot_id_reader=lambda: BOOT_ID,
                clock=lambda: 100.0,
                state_dir=state,
            )
            self.assertTrue(document["ready"])
            self.assertNotIn("Аврора", (state / proof.STATUS_NAME).read_text())
            loaded = proof.read_status(state, current_boot_id=BOOT_ID)
            self.assertTrue(loaded["alice_public_ready"])
            with self.assertRaises(proof.DialogueQualificationError):
                proof.read_status(
                    state,
                    current_boot_id="fedcba98-7654-3210-fedc-ba9876543210",
                )


if __name__ == "__main__":
    unittest.main()
