#!/usr/bin/env python3
"""Offline safety contracts for bounded Home Assistant Core recovery."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import home_assistant_core_recovery as recovery  # noqa: E402
import home_assistant_read as ha_read  # noqa: E402
import incident_monitor as monitor  # noqa: E402


TOKEN = "SECRET_SENTINEL.DO_NOT_LEAK.abcdefghijklmnopqrstuvwxyz"


class FakeResponse:
    status = 200

    def read(self, _amount: int) -> bytes:
        return b"[]"


class FakeConnection:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, bytes | None, dict[str, str]]] = []

    def request(self, method, path, *, body, headers) -> None:
        self.requests.append((method, path, body, headers))

    def getresponse(self) -> FakeResponse:
        return FakeResponse()

    def close(self) -> None:
        pass


def config() -> ha_read.AdapterConfig:
    return ha_read.AdapterConfig("http", "192.168.1.127", 8123, TOKEN, (), True)


class CoreRecoveryTests(unittest.TestCase):
    def _store(self, temporary: str, *, baseline: bool = False) -> monitor.IncidentStore:
        directory = Path(temporary) / "state"
        directory.mkdir(mode=0o700)
        store = monitor.IncidentStore(directory / monitor.DATABASE_NAME)
        store.observe(
            monitor.RESERVED_SUBJECT,
            "system",
            "unreachable",
            100,
            unavailable=True,
            source="startup_snapshot" if baseline else "websocket_watchdog",
        )
        store.confirm_due(160, 60)
        return store

    def test_service_paths_and_bodies_are_fixed(self) -> None:
        connection = FakeConnection()
        recovery._request_json(
            config(), "POST", recovery.CHECK_PATH, body=b"{}",
            connection_factory=lambda _config: connection,
        )
        recovery._request_json(
            config(), "POST", recovery.RESTART_PATH, body=b"{}",
            connection_factory=lambda _config: connection,
        )
        self.assertEqual(
            [(item[0], item[1], item[2]) for item in connection.requests],
            [
                ("POST", "/api/services/homeassistant/check_config", b"{}"),
                ("POST", "/api/services/homeassistant/restart", b"{}"),
            ],
        )
        self.assertEqual(connection.requests[0][3]["Authorization"], "Bearer " + TOKEN)

    def test_confirmed_partial_outage_checks_restarts_once_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            calls: list[str] = []
            try:
                first = recovery.run_once(
                    store,
                    now=460,
                    live=True,
                    config_loader=config,
                    probe=lambda _config: calls.append("probe"),
                    check_caller=lambda _config: calls.append("check"),
                    restart_caller=lambda _config: calls.append("restart"),
                    verifier=lambda _config: "verified",
                )
                second = recovery.run_once(
                    store,
                    now=461,
                    live=True,
                    config_loader=config,
                    probe=lambda _config: self.fail("second run must have no candidate"),
                )
                self.assertEqual(first["check_calls"], 1)
                self.assertEqual(first["restart_calls"], 1)
                self.assertEqual(first["verified"], 1)
                self.assertEqual(second["candidates"], 0)
                self.assertEqual(calls, ["probe", "check", "restart"])
                row = store.connection.execute(
                    "SELECT * FROM core_recovery_actions"
                ).fetchone()
                self.assertEqual(row["status"], "verified")
            finally:
                store.close()

    def test_dead_core_requires_out_of_band_channel_and_is_not_claimed_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary)
            try:
                result = recovery.run_once(
                    store,
                    now=460,
                    live=True,
                    config_loader=config,
                    probe=lambda _config: (_ for _ in ()).throw(
                        recovery.CoreRecoveryError("offline")
                    ),
                    check_caller=lambda _config: self.fail("check must not run"),
                    restart_caller=lambda _config: self.fail("restart must not run"),
                )
                self.assertEqual(result["outcome"], "out_of_band_required")
                self.assertEqual(result["restart_calls"], 0)
                amount = store.connection.execute(
                    "SELECT COUNT(*) FROM core_recovery_actions"
                ).fetchone()[0]
                self.assertEqual(amount, 0)
            finally:
                store.close()

    def test_baseline_core_incident_is_never_restarted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(temporary, baseline=True)
            try:
                result = recovery.run_once(
                    store,
                    now=460,
                    live=True,
                    config_loader=config,
                    probe=lambda _config: self.fail("baseline must not be probed"),
                )
                self.assertEqual(result["candidates"], 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
