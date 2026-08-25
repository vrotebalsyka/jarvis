#!/usr/bin/env python3
"""Contracts for the PowerShell-free Ubuntu GPU supervisor."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import windows_gpu_supervisor as supervisor  # noqa: E402


class WindowsGpuSupervisorTests(unittest.TestCase):
    def test_process_lock_rejects_a_second_or_symlinked_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "gpu.lock"
            first = supervisor.acquire_lock(lock_path)
            try:
                with self.assertRaises(supervisor.GpuSupervisorAlreadyRunning):
                    supervisor.acquire_lock(lock_path)
            finally:
                supervisor.os.close(first)
            target = Path(directory) / "target"
            target.write_text("", encoding="ascii")
            lock_path.unlink()
            lock_path.symlink_to(target)
            with self.assertRaises((OSError, supervisor.GpuSupervisorError)):
                supervisor.acquire_lock(lock_path)

    def test_ready_endpoint_never_launches_another_process(self) -> None:
        with mock.patch.object(supervisor, "validate_binary"), mock.patch.object(
            supervisor, "current_endpoint", return_value=object()
        ), mock.patch.object(supervisor, "launch") as launcher:
            self.assertEqual(supervisor.supervise(once=True, probe=lambda _e: True), 0)
        launcher.assert_not_called()

    def test_launch_uses_no_shell_and_pinned_environment(self) -> None:
        endpoint = supervisor.ollama_endpoint.OllamaEndpoint(
            "http://172.20.0.1:11434", "172.20.0.1", 11434
        )
        with mock.patch("subprocess.Popen") as popen:
            supervisor.launch(Path("/mnt/h/Ollama/ollama.exe"), endpoint)
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["/mnt/h/Ollama/ollama.exe", "serve"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["cwd"], "/mnt/h/Ollama")
        self.assertEqual(kwargs["env"]["OLLAMA_HOST"], "172.20.0.1:11434")
        self.assertEqual(kwargs["env"]["OLLAMA_NO_CLOUD"], "1")
        self.assertEqual(kwargs["env"]["OLLAMA_VULKAN"], "1")
        self.assertEqual(kwargs["env"]["OLLAMA_LLM_LIBRARY"], "vulkan")
        self.assertEqual(
            kwargs["env"]["OLLAMA_CONTEXT_LENGTH"],
            str(supervisor.model_runtime_policy.get_profile("dialogue").context_window),
        )


if __name__ == "__main__":
    unittest.main()
