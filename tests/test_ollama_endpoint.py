#!/usr/bin/env python3
"""Security and consistency checks for the pinned native Ollama endpoint."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from ollama_endpoint import (  # noqa: E402
    ENV_KEY,
    LOCAL_FALLBACK_URL,
    EndpointConfigError,
    OllamaEndpoint,
    load_ollama_endpoint,
    load_runtime_ollama_endpoint,
    wait_for_runtime_ollama_endpoint,
)


ROUTE_HEADER = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
ROUTE = ROUTE_HEADER + "eth0\t00000000\t01C01BAC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"


class EndpointLoaderTests(unittest.TestCase):
    def _fixtures(self, value: str, *, mode: int = 0o600) -> tuple[Path, Path, tempfile.TemporaryDirectory[str]]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        env_path = root / ".env"
        route_path = root / "route"
        env_path.write_text(f"{ENV_KEY}={value}\n", encoding="utf-8")
        env_path.chmod(mode)
        route_path.write_text(ROUTE, encoding="ascii")
        return env_path, route_path, temporary

    def test_loads_exact_current_private_gateway(self) -> None:
        env_path, route_path, temporary = self._fixtures("http://172.27.192.1:11434")
        with temporary:
            endpoint = load_ollama_endpoint(env_path=env_path, route_path=route_path)
        self.assertEqual((endpoint.host, endpoint.port), ("172.27.192.1", 11434))

    def test_rejects_stale_unsafe_and_ambiguous_values(self) -> None:
        values = (
            "http://172.27.192.2:11434",
            "http://0.0.0.0:11434",
            "http://192.168.1.175:11434",
            "https://172.27.192.1:11434",
            "http://172.27.192.1:11500",
            "http://172.27.192.1:11434/api",
        )
        for value in values:
            env_path, route_path, temporary = self._fixtures(value)
            with self.subTest(value=value), temporary, self.assertRaises(EndpointConfigError):
                load_ollama_endpoint(env_path=env_path, route_path=route_path)

        env_path, route_path, temporary = self._fixtures("http://172.27.192.1:11434", mode=0o644)
        with temporary, self.assertRaises(EndpointConfigError):
            load_ollama_endpoint(env_path=env_path, route_path=route_path)

        env_path, route_path, temporary = self._fixtures("http://172.27.192.1:11434")
        with temporary:
            env_path.write_text(
                f"{ENV_KEY}=http://172.27.192.1:11434\n" * 2,
                encoding="utf-8",
            )
            with self.assertRaises(EndpointConfigError):
                load_ollama_endpoint(env_path=env_path, route_path=route_path)


class EndpointConsumerTests(unittest.TestCase):
    def test_runtime_selection_prefers_gpu_and_falls_back_only_to_loopback(self) -> None:
        primary = OllamaEndpoint("http://172.27.192.1:11434", "172.27.192.1", 11434)
        with mock.patch("ollama_endpoint.load_ollama_endpoint", return_value=primary), mock.patch(
            "ollama_endpoint._probe", side_effect=lambda endpoint: endpoint.host == "127.0.0.1"
        ):
            selected = load_runtime_ollama_endpoint()
        self.assertEqual(selected.base_url, LOCAL_FALLBACK_URL)

        with mock.patch("ollama_endpoint.load_ollama_endpoint", return_value=primary), mock.patch(
            "ollama_endpoint._probe", return_value=True
        ):
            selected = load_runtime_ollama_endpoint()
        self.assertEqual(selected, primary)

    def test_startup_wait_prefers_gpu_before_using_ready_fallback(self) -> None:
        primary = OllamaEndpoint("http://172.27.192.1:11434", "172.27.192.1", 11434)
        now = [0.0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        def probe(endpoint: OllamaEndpoint) -> bool:
            return endpoint.host == "127.0.0.1" or now[0] >= 2.0

        with mock.patch("ollama_endpoint.load_ollama_endpoint", return_value=primary), mock.patch(
            "ollama_endpoint._probe", side_effect=probe
        ):
            selected = wait_for_runtime_ollama_endpoint(
                prefer_primary_seconds=3,
                wait_seconds=5,
                clock=lambda: now[0],
                sleeper=sleep,
            )
        self.assertEqual(selected, primary)
        self.assertEqual(now[0], 2.0)

    def test_startup_wait_uses_only_loopback_after_gpu_preference_window(self) -> None:
        primary = OllamaEndpoint("http://172.27.192.1:11434", "172.27.192.1", 11434)
        now = [0.0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        with mock.patch("ollama_endpoint.load_ollama_endpoint", return_value=primary), mock.patch(
            "ollama_endpoint._probe", side_effect=lambda endpoint: endpoint.host == "127.0.0.1"
        ):
            selected = wait_for_runtime_ollama_endpoint(
                prefer_primary_seconds=3,
                wait_seconds=5,
                clock=lambda: now[0],
                sleeper=sleep,
            )
        self.assertEqual(selected.base_url, LOCAL_FALLBACK_URL)
        self.assertEqual(now[0], 3.0)

    def test_startup_wait_policy_is_bounded(self) -> None:
        for prefer, wait in ((-1, 0), (2, 1), (0, 121)):
            with self.subTest(prefer=prefer, wait=wait), self.assertRaises(EndpointConfigError):
                wait_for_runtime_ollama_endpoint(
                    prefer_primary_seconds=prefer,
                    wait_seconds=wait,
                )

    def test_runtime_consumers_use_the_single_trusted_source(self) -> None:
        config = (PROJECT_DIR / "hermes" / "config.yaml").read_text(encoding="utf-8")
        self.assertEqual(config.count("${HOME_BUTLER_OLLAMA_BASE_URL}/v1"), 2)
        self.assertEqual(config.count("context_length: 2048"), 3)
        self.assertNotIn("context_length: 64000", config)

        collector = (SCRIPTS_DIR / "local-health-check.sh").read_text(encoding="utf-8")
        reporter = (SCRIPTS_DIR / "health_report.py").read_text(encoding="utf-8")
        evaluator = (PROJECT_DIR / "tests" / "evaluate_model.py").read_text(encoding="utf-8")
        self.assertIn("ollama_endpoint.py", collector)
        self.assertIn("load_runtime_ollama_endpoint", reporter)
        self.assertIn("load_ollama_endpoint", evaluator)
        for document in (config, collector, reporter, evaluator):
            self.assertNotIn("172.27.192.1", document)

        endpoint = load_ollama_endpoint()
        self.assertEqual(endpoint.base_url, "http://172.27.192.1:11434")


if __name__ == "__main__":
    unittest.main()
