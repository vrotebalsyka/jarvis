#!/usr/bin/env python3
"""Security and boundedness contracts for Home Assistant attribute metadata."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import safe_attribute_sanitizer as sanitizer  # noqa: E402


class SafeAttributeSanitizerTests(unittest.TestCase):
    def test_safe_types_are_bounded_and_every_string_is_marked_untrusted(self) -> None:
        result = sanitizer.sanitize_attributes({
            "device_class": "temperature",
            "supported_features": 3,
            "options": ["eco", "normal"],
            "range": {"min": 1.0, "max": 5.0},
        })
        self.assertEqual(
            result["device_class"],
            {"text": "temperature", "trust": "untrusted_data"},
        )
        self.assertEqual(result["supported_features"], 3)
        self.assertEqual(
            sanitizer.untrusted_text(result["options"][0]),
            "eco",
        )
        self.assertEqual(result["range"]["max"], 5.0)

    def test_deny_by_name_and_content_blocks_private_or_secret_values(self) -> None:
        result = sanitizer.sanitize_attributes({
            "friendly_name": "Кухня",
            "local_key": "private",
            "host": "192.168.1.10",
            "notes": "Authorization: Bearer secret",
            "website": "https://example.test/path",
            "mac_address": "AA:BB:CC:DD:EE:FF",
        })
        self.assertEqual(set(result), {"friendly_name"})

    def test_allow_names_is_not_the_only_security_boundary(self) -> None:
        result = sanitizer.sanitize_attributes(
            {
                "options": ["safe", "token=private"],
                "unit_of_measurement": "%",
                "unexpected": "ignored",
            },
            allowed_names={"options", "unit_of_measurement"},
        )
        self.assertNotIn("options", result)
        self.assertNotIn("unexpected", result)
        self.assertEqual(
            sanitizer.untrusted_text(result["unit_of_measurement"]), "%"
        )

    def test_depth_count_control_char_and_nonfinite_number_are_rejected(self) -> None:
        unsafe_values = (
            {"a": {"b": {"c": {"d": "too deep"}}}},
            list(range(sanitizer.MAX_ITEMS + 1)),
            "bad\x00text",
            float("nan"),
        )
        for value in unsafe_values:
            with self.subTest(value=repr(value)), self.assertRaises(
                sanitizer.AttributeSanitizerError
            ):
                sanitizer.sanitize_value(value)


if __name__ == "__main__":
    unittest.main()
