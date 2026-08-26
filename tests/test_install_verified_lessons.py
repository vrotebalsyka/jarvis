#!/usr/bin/env python3
"""Contracts for installing reviewed model lessons into the bounded workspace."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import install_verified_lessons as lessons  # noqa: E402
import model_workspace  # noqa: E402


class VerifiedLessonInstallTests(unittest.TestCase):
    def test_install_is_idempotent_secret_free_and_stays_in_workspace(self) -> None:
        training_source = PROJECT_DIR / "training" / "stage67_verified_examples.jsonl"
        self.assertTrue(training_source.is_file())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            with (
                mock.patch.object(model_workspace, "WORKSPACE_ROOT", root),
                mock.patch.object(lessons, "DEFAULT_TRAINING_SOURCE", training_source),
            ):
                first = lessons.install()
                second = lessons.install()
            self.assertEqual(first["status"], "installed")
            self.assertEqual(second["status"], "installed")
            self.assertFalse(first["weights_modified"])
            self.assertGreaterEqual(first["training_example_count"], 20)
            lesson = json.loads((root / lessons.LESSON_PATH).read_text(encoding="utf-8"))
            self.assertEqual(lesson["lesson_set"], "stage67-truthful-home")
            training = (root / lessons.TRAINING_PATH).read_text(encoding="utf-8")
            self.assertIn("accepted_unverified", training)
            self.assertIn("rinse_aid", training)
            self.assertNotIn("Authorization", training)
            self.assertNotIn("192.168.", training)
            self.assertEqual((root / lessons.LESSON_PATH).stat().st_mode & 0o777, 0o600)

    def test_private_or_incomplete_training_corpus_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bad.jsonl"
            source.write_text(
                '{"id":"bad","input":"x","secret":"Authorization: Bearer x"}\n',
                encoding="utf-8",
            )
            source.chmod(0o600)
            with self.assertRaises(lessons.LessonInstallError):
                lessons._read_training(source)


if __name__ == "__main__":
    unittest.main()
