#!/usr/bin/env python3
"""Static safety contract for the Windows GPU Ollama supervisor."""

from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SUPERVISOR = (PROJECT_DIR / "scripts" / "windows-ollama-supervisor.ps1").read_text()


class WindowsOllamaSupervisorTests(unittest.TestCase):
    def test_uses_exact_wsl_private_bind_and_local_models(self) -> None:
        self.assertIn("Get-NetIPAddress -AddressFamily IPv4", SUPERVISOR)
        self.assertIn("vEthernet (WSL*", SUPERVISOR)
        self.assertIn("$bytes[0] -eq 172", SUPERVISOR)
        self.assertIn("$bytes[1] -ge 16", SUPERVISOR)
        self.assertIn("$bytes[1] -le 31", SUPERVISOR)
        self.assertIn("$env:OLLAMA_HOST = \"$address`:$listenPort\"", SUPERVISOR)
        self.assertIn("$env:OLLAMA_MODELS = $modelRoot", SUPERVISOR)
        self.assertIn("$env:OLLAMA_NO_CLOUD = '1'", SUPERVISOR)
        self.assertNotIn("0.0.0.0", SUPERVISOR)

    def test_validates_binary_and_only_manages_pinned_serve_process(self) -> None:
        self.assertIn("Get-AuthenticodeSignature", SUPERVISOR)
        self.assertIn("$signature.Status -ne 'Valid'", SUPERVISOR)
        self.assertIn("$_.ExecutablePath -eq $ollamaExe", SUPERVISOR)
        self.assertIn("CommandLine -match", SUPERVISOR)
        self.assertIn("Stop-ManagedOllamaServers", SUPERVISOR)
        self.assertIn("Test-ManagedOllamaServerIdentity", SUPERVISOR)
        self.assertGreaterEqual(
            SUPERVISOR.count(
                "Test-ManagedOllamaServerIdentity -ProcessId $server.ProcessId"
            ),
            2,
        )
        self.assertNotIn("Stop-Process -Name", SUPERVISOR)

    def test_rejects_additional_listener_addresses_for_the_managed_pid(self) -> None:
        listener_block = SUPERVISOR.split("function Test-ExactListener", 1)[1].split(
            "function Stop-ManagedOllamaServers", 1
        )[0]
        self.assertIn("$_.OwningProcess -eq $ProcessId", listener_block)
        self.assertNotIn(
            "$_.OwningProcess -eq $ProcessId -and $_.LocalAddress -eq $Address",
            listener_block,
        )
        self.assertIn(
            "$listeners.Count -eq 1 -and $listeners[0].LocalAddress -eq $Address",
            listener_block,
        )

    def test_runtime_limits_gpu_memory_pressure_and_parallelism(self) -> None:
        for setting in (
            "OLLAMA_CONTEXT_LENGTH = '2048'",
            "OLLAMA_FLASH_ATTENTION = '1'",
            "OLLAMA_KV_CACHE_TYPE = 'q8_0'",
            "OLLAMA_NUM_PARALLEL = '1'",
            "OLLAMA_MAX_LOADED_MODELS = '1'",
            "OLLAMA_KEEP_ALIVE = '5m'",
        ):
            self.assertIn(setting, SUPERVISOR)


if __name__ == "__main__":
    unittest.main()
