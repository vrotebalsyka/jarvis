#!/usr/bin/env python3
"""Static safety contract for the clipboard-only Webhook helper."""

from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
HELPER = (PROJECT_DIR / "scripts" / "copy-alice-webhook-url.sh").read_text()


class AliceWebhookClipboardTests(unittest.TestCase):
    def test_helper_validates_private_file_and_never_prints_the_url(self) -> None:
        self.assertIn("set -euo pipefail", HELPER)
        self.assertIn("regular\\ file:0:0:600:1", HELPER)
        self.assertIn("alice-public-origin.txt", HELPER)
        self.assertIn("\\.ts\\.net", HELPER)
        self.assertIn("[A-Za-z0-9_-]{32,128}", HELPER)
        self.assertIn("printf '%s' \"$url\" | \"$CLIP_EXE\"", HELPER)
        self.assertIn("unset origin expected_prefix url secret", HELPER)
        self.assertNotIn("set -x", HELPER)
        self.assertNotIn("echo \"$url\"", HELPER)
        self.assertNotIn("printf '%s\\n' \"$url\"", HELPER)
        self.assertNotIn("ngrok-free.dev", HELPER)


if __name__ == "__main__":
    unittest.main()
