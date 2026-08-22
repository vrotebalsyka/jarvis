#!/usr/bin/env python3
"""Real-socket regression for the local model request wall-clock deadline."""

from __future__ import annotations

import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import health_report  # noqa: E402
import health_report_core as core  # noqa: E402


class OllamaDeadlineTests(unittest.TestCase):
    def _exercise_slow_drip(self, *, connection_close: bool) -> float:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        stopped = threading.Event()

        def serve() -> None:
            try:
                connection, _address = server.accept()
                with connection:
                    connection.recv(65536)
                    close_header = b"Connection: close\r\n" if connection_close else b""
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n"
                        b"Content-Type: application/json\r\n" + close_header + b"\r\n"
                    )
                    drip_until = time.monotonic() + 1.0
                    while time.monotonic() < drip_until:
                        connection.sendall(b"x")
                        time.sleep(0.01)
            except OSError:
                pass
            finally:
                server.close()
                stopped.set()

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        started = time.monotonic()
        with (
            mock.patch.object(health_report, "OLLAMA_HOST", "127.0.0.1"),
            mock.patch.object(health_report, "OLLAMA_PORT", port),
            mock.patch.object(health_report, "OLLAMA_TIMEOUT_SECONDS", 0.1),
            self.assertRaises(core.HealthReportError),
        ):
            health_report.call_ollama({"model": "home-butler"})
        elapsed = time.monotonic() - started
        self.assertTrue(stopped.wait(1.0))
        return elapsed

    def test_slow_drip_response_is_interrupted_by_overall_deadline(self) -> None:
        for connection_close in (False, True):
            with self.subTest(connection_close=connection_close):
                elapsed = self._exercise_slow_drip(connection_close=connection_close)
                self.assertLess(elapsed, 0.5)


if __name__ == "__main__":
    unittest.main()
