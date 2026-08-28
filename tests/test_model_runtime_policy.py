from __future__ import annotations

import dataclasses
import contextlib
import io
import pathlib
import re
import sys
import unittest


SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import model_runtime_policy as policy  # noqa: E402


class ModelRuntimePolicyTests(unittest.TestCase):
    def test_required_profiles_are_complete_and_immutable(self) -> None:
        self.assertEqual(frozenset(policy.PROFILES), policy.REQUIRED_PROFILES)
        with self.assertRaises(TypeError):
            policy.PROFILES["extra"] = policy.get_profile("voice_fast")  # type: ignore[index]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            policy.get_profile("voice_fast").context_window = 64  # type: ignore[misc]

    def test_benchmark_selected_routes_are_canonical(self) -> None:
        expected = {
            "voice_fast": ("qwen3.5:4b-q4_K_M", 8_192),
            "dialogue": ("qwen3.5:4b-q4_K_M", 32_768),
            "diagnostic": ("qwen3.5:4b-q4_K_M", 32_768),
            "structured": ("qwen3.5:4b-q4_K_M", 8_192),
            "summarizer": ("qwen3.5:4b-q4_K_M", 16_384),
        }
        for name, selected in expected.items():
            profile = policy.get_profile(name)
            self.assertEqual((profile.model, profile.context_window), selected)
            self.assertNotEqual(profile.context_window, 65_536)

    def test_bounded_host_projection_prints_only_canonical_context(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(policy.run(["--context-window", "dialogue"]), 0)
        self.assertEqual(
            output.getvalue().strip(),
            str(policy.get_profile("dialogue").context_window),
        )

    def test_voice_worker_can_finish_after_yandex_has_deferred_the_reply(self) -> None:
        voice = policy.get_profile("voice_fast")
        self.assertEqual(voice.latency_budget_seconds, 4.0)
        self.assertGreaterEqual(voice.request_timeout_seconds, 60.0)

    def test_chat_payload_comes_only_from_selected_profile(self) -> None:
        payload = policy.build_chat_payload(
            "structured",
            [{"role": "user", "content": "classify"}],
            response_format={"type": "object"},
            tools=[{"type": "function", "function": {"name": "read"}}],
        )
        profile = policy.get_profile("structured")
        self.assertEqual(payload["model"], profile.model)
        self.assertEqual(payload["options"]["num_ctx"], profile.context_window)
        self.assertEqual(payload["options"]["num_predict"], profile.output_limit)
        self.assertEqual(payload["keep_alive"], profile.keep_alive)
        self.assertEqual(payload["think"], profile.think)

    def test_unknown_profile_and_tools_for_summarizer_fail_closed(self) -> None:
        with self.assertRaises(policy.ModelRuntimePolicyError):
            policy.get_profile("large_unbounded")
        with self.assertRaises(policy.ModelRuntimePolicyError):
            policy.build_chat_payload(
                "summarizer",
                [{"role": "user", "content": "summary"}],
                tools=[{"type": "function"}],
            )

    def test_generate_payload_uses_the_same_policy(self) -> None:
        payload = policy.build_generate_payload(
            "structured",
            "return json",
            response_format={"type": "object"},
        )
        profile = policy.get_profile("structured")
        self.assertEqual(payload["model"], profile.model)
        self.assertEqual(payload["options"]["num_ctx"], profile.context_window)
        self.assertEqual(payload["options"]["num_predict"], profile.output_limit)
        self.assertEqual(payload["format"], {"type": "object"})

    def test_trace_metadata_is_secret_safe_policy_evidence(self) -> None:
        trace = policy.trace_metadata("dialogue")
        self.assertEqual(trace["policy_schema_version"], 1)
        self.assertEqual(trace["name"], "dialogue")
        self.assertNotIn("messages", trace)
        self.assertNotIn("prompt", trace)
        self.assertNotIn("endpoint", trace)
        self.assertNotIn("token", repr(trace).casefold())

    def test_production_call_sites_have_no_manual_context_or_model_route(self) -> None:
        project = SCRIPT_DIR.parent
        call_sites = (
            "owner_chat.py",
            "alice_skill_gateway.py",
            "model_ha_proof.py",
            "model_ha_control.py",
            "recovery_planner.py",
            "health_report.py",
            "ha_full_entity_report.py",
            "ha_model_study.py",
            "home_stress_test.py",
        )
        forbidden_context = re.compile(
            r"num_ctx[\"']?\s*[:=]\s*(?:1_?536|2_?048|3_?072|4_?096|8_?192)"
        )
        for name in call_sites:
            text = (project / "scripts" / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertIsNone(forbidden_context.search(text))
                self.assertNotIn("VOICE_NUM_CTX", text)

    def test_modelfile_and_hermes_projection_match_dialogue_policy(self) -> None:
        project = SCRIPT_DIR.parent
        modelfile = (project / "models" / "home-butler.Modelfile").read_text(
            encoding="utf-8"
        )
        self.assertNotRegex(modelfile, r"(?m)^PARAMETER\s+(?:num_ctx|num_predict)")

        dialogue = policy.get_profile("dialogue")
        hermes = (project / "hermes" / "config.yaml").read_text(encoding="utf-8")
        expected_lines = (
            f'  default: "{dialogue.model}"',
            f"  context_length: {dialogue.context_window}",
            f"  max_tokens: {dialogue.output_limit}",
            f'    default_model: "{dialogue.model}"',
            f"    context_length: {dialogue.context_window}",
            f"    request_timeout_seconds: {int(dialogue.request_timeout_seconds)}",
            f"      {dialogue.model}:",
            f"        context_length: {dialogue.context_window}",
        )
        for line in expected_lines:
            with self.subTest(line=line):
                self.assertIn(line, hermes)


if __name__ == "__main__":
    unittest.main()
