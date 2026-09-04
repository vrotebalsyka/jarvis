from __future__ import annotations

import importlib.util
import sys
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "tests" / "live_stage72_natural_language_acceptance.py"
MANIFEST_PATH = ROOT / "tests" / "data" / "stage72_blind_natural_language_100.jsonl"


def load_runner():
    spec = importlib.util.spec_from_file_location("stage72_natural_acceptance", RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("natural-language runner unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Stage72NaturalLanguageContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = load_runner()

    def test_frozen_blind_corpus_is_new_and_complete(self) -> None:
        rows, digest = self.runner.load_blind_manifest(MANIFEST_PATH)
        self.assertEqual(digest, self.runner.EXPECTED_SHA256)
        self.assertEqual(len(rows), 100)
        self.assertEqual(Counter(row["category"] for row in rows), self.runner.EXPECTED_CATEGORIES)

    def test_required_free_speech_classes_are_present(self) -> None:
        rows, _digest = self.runner.load_blind_manifest(MANIFEST_PATH)
        utterances = {row["utterance"].casefold() for row in rows}
        required = {
            "сделай свет в ванной",
            "пусть в кабинете будет светло",
            "можешь погасить в туалете",
            "убери свет в коридоре",
            "хочу, чтобы зеркало в ванной светилось",
            "в кабинете темно, включи там свет",
            "не надо включать свет",
            "а теперь выключи его",
        }
        self.assertTrue(required <= utterances)

    def test_corpus_is_not_hardcoded_and_runner_uses_real_model_boundary(self) -> None:
        source = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertNotIn("SelectingModel", source)
        self.assertNotIn("ScriptedModel", source)
        self.assertIn("agent.call_ollama", source)
        self.assertIn("owner_chat.answer_natural", source)
        self.assertIn("HTTPConnection.request = guarded_request", source)


if __name__ == "__main__":
    unittest.main()
