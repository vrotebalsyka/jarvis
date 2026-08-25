#!/usr/bin/env python3
"""Offline contracts for the fixed local Ollama voice model alias."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import ollama_model_alias as alias  # noqa: E402
from ollama_endpoint import OllamaEndpoint  # noqa: E402


DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


def inventory(*names: tuple[str, str]) -> dict[str, object]:
    return {
        "models": [
            {"name": name, "digest": digest}
            for name, digest in names
        ]
    }


def shown(*, voice: bool) -> dict[str, object]:
    parameters = (
        f"temperature 0.1\nnum_ctx {alias.VOICE_PROFILE.context_window}"
        f"\nnum_predict {alias.VOICE_PROFILE.output_limit}"
        if voice
        else "num_ctx 64000\nnum_predict 384"
    )
    return {
        "details": {
            "family": "qwen35",
            "parent_model": alias.SOURCE_MODEL if voice else "",
        },
        "model_info": {"general.architecture": "qwen35"},
        "system": "reviewed system",
        "template": "reviewed template",
        "tensors": [{"name": "token_embd.weight"}],
        "parameters": parameters,
    }


class OllamaModelAliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.endpoint = OllamaEndpoint(
            "http://172.27.192.1:11434", "172.27.192.1", 11434
        )

    def test_missing_profile_is_created_once_and_verified(self) -> None:
        responses = iter(
            [
                inventory((alias.SOURCE_MODEL, DIGEST)),
                inventory(
                    (alias.SOURCE_MODEL, DIGEST),
                    ("home-butler-voice:latest", OTHER_DIGEST),
                ),
            ]
        )
        calls: list[tuple[str, str]] = []
        result = alias.ensure_alias(
            endpoint_loader=lambda: self.endpoint,
            inventory_reader=lambda _endpoint, path: (
                next(responses) if path == "/api/tags" else self.fail("wrong path")
            ),
            create_writer=lambda _endpoint, source, destination: calls.append(
                (source, destination)
            ),
            model_reader=lambda _endpoint, name: shown(
                voice=name == "home-butler-voice"
            ),
        )
        self.assertEqual(calls, [(alias.SOURCE_MODEL, "home-butler-voice")])
        self.assertEqual(result["status"], "created")

    def test_existing_exact_alias_is_idempotent(self) -> None:
        document = inventory(
            (alias.SOURCE_MODEL, DIGEST),
            ("home-butler-voice:latest", OTHER_DIGEST),
        )
        result = alias.ensure_alias(
            endpoint_loader=lambda: self.endpoint,
            inventory_reader=lambda _endpoint, _path: document,
            create_writer=lambda *_args: self.fail("valid profile must not be recreated"),
            model_reader=lambda _endpoint, name: shown(
                voice=name == "home-butler-voice"
            ),
        )
        self.assertEqual(result["status"], "already_present")

    def test_wrong_profile_and_cpu_endpoint_fail_closed(self) -> None:
        document = inventory(
            (alias.SOURCE_MODEL, DIGEST),
            ("home-butler-voice:latest", OTHER_DIGEST),
        )
        with self.assertRaises(alias.AliasError):
            alias.ensure_alias(
                endpoint_loader=lambda: self.endpoint,
                inventory_reader=lambda _endpoint, _path: document,
                model_reader=lambda _endpoint, name: (
                    {
                        **shown(voice=True),
                        "system": "hostile replacement",
                    }
                    if name == "home-butler-voice"
                    else shown(voice=False)
                ),
            )
        with self.assertRaises(alias.AliasError):
            alias.ensure_alias(
                endpoint_loader=lambda: OllamaEndpoint(
                    "http://127.0.0.1:11434", "127.0.0.1", 11434
                )
            )


if __name__ == "__main__":
    unittest.main()
