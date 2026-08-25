#!/usr/bin/env python3
"""Regression checks for the secret-free no-cloud audit."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import no_cloud_audit  # noqa: E402


class NoCloudAuditTests(unittest.TestCase):
    def test_current_configuration_is_local_only(self) -> None:
        with mock.patch.dict(no_cloud_audit.os.environ, {}, clear=True):
            ok, lines = no_cloud_audit.audit()
        self.assertTrue(ok)
        rendered = "\n".join(lines)
        self.assertIn(
            "LOCAL_MODEL: "
            + no_cloud_audit.model_runtime_policy.get_profile("dialogue").model,
            rendered,
        )
        self.assertIn("CLOUD_FALLBACK: absent", rendered)
        self.assertNotIn("eyJ", rendered)

    def test_cloud_process_key_fails_without_printing_value(self) -> None:
        with mock.patch.dict(
            no_cloud_audit.os.environ,
            {"OPENAI_API_KEY": "SECRET_SENTINEL"},
            clear=True,
        ):
            ok, lines = no_cloud_audit.audit()
        self.assertFalse(ok)
        rendered = "\n".join(lines)
        self.assertIn("OPENAI_API_KEY: present", rendered)
        self.assertNotIn("SECRET_SENTINEL", rendered)


if __name__ == "__main__":
    unittest.main()
