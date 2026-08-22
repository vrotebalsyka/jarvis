#!/usr/bin/env python3
"""Contract tests for bounded Alice webhook health recovery."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import alice_skill_gateway as gateway  # noqa: E402
import alice_skill_health as health  # noqa: E402


SECRET = "S" * 40
CONFIG = gateway.GatewayConfig(
    SECRET,
    "skill-health-1234",
    ("owner-health-1234",),
)


class AliceSkillHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        os.chmod(self.directory, 0o700)
        self.state = self.directory / "status.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_ping_is_a_real_owner_request_and_response_is_exact(self) -> None:
        raw = health.ping_request(CONFIG, session_id="alice-health-test-1234")
        document = json.loads(raw.decode("utf-8"))
        self.assertEqual(document["request"]["command"], "ping")
        self.assertEqual(document["session"]["skill_id"], CONFIG.skill_id)
        self.assertEqual(
            document["session"]["user"]["user_id"], CONFIG.owner_ids[0]
        )
        self.assertNotIn(SECRET.encode("ascii"), raw)
        response = gateway.skill_response(health.PING_TEXT)
        health.validate_ping_response(
            200,
            json.dumps(response, ensure_ascii=False).encode("utf-8"),
            "public_probe",
        )
        with self.assertRaises(health.HealthError):
            health.validate_ping_response(500, b"{}", "public_probe")

    def test_healthy_probe_writes_private_secret_free_status(self) -> None:
        ready = health.run_once(
            CONFIG,
            state_path=self.state,
            local_probe=lambda: None,
            public_probe_runner=lambda: None,
            wall_clock=lambda: 1000,
        )
        self.assertTrue(ready)
        document = health.load_state(self.state)
        self.assertTrue(document["healthy"])
        self.assertEqual(document["consecutive_failures"], 0)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(SECRET, self.state.read_text(encoding="ascii"))

    def test_one_failure_is_observed_without_restarting_anything(self) -> None:
        calls: list[str] = []

        def local_failure() -> None:
            raise health.HealthError("local_probe")

        def public_failure() -> None:
            raise health.HealthError("public_probe")

        ready = health.run_once(
            CONFIG,
            state_path=self.state,
            local_probe=local_failure,
            public_probe_runner=public_failure,
            restarter=calls.append,
            wall_clock=lambda: 1000,
        )
        self.assertFalse(ready)
        self.assertEqual(calls, [])
        document = health.load_state(self.state)
        self.assertEqual(document["consecutive_failures"], 1)
        self.assertEqual(document["last_action"], "none")

    def test_second_local_failure_restarts_only_the_skill_and_rechecks_public(self) -> None:
        service_up = False
        calls: list[str] = []

        def local_probe() -> None:
            if not service_up:
                raise health.HealthError("local_probe")

        def public_probe() -> None:
            if not service_up:
                raise health.HealthError("public_probe")

        def restart(unit: str) -> None:
            nonlocal service_up
            calls.append(unit)
            if unit == health.SKILL_UNIT:
                service_up = True

        for expected in (False, False, True):
            ready = health.run_once(
                CONFIG,
                state_path=self.state,
                local_probe=local_probe,
                public_probe_runner=public_probe,
                restarter=restart,
                wall_clock=lambda: 1000,
                monotonic_clock=lambda: 0,
                sleeper=lambda _seconds: None,
            )
            self.assertEqual(ready, expected)
        self.assertEqual(calls, [health.SKILL_UNIT])
        document = health.load_state(self.state)
        self.assertTrue(document["healthy"])
        self.assertEqual(document["last_action"], "restart_skill")

    def test_second_public_failure_restarts_only_the_tunnel(self) -> None:
        tunnel_up = False
        calls: list[str] = []

        def public_probe() -> None:
            if not tunnel_up:
                raise health.HealthError("public_probe")

        def restart(unit: str) -> None:
            nonlocal tunnel_up
            calls.append(unit)
            if unit == health.TUNNEL_UNIT:
                tunnel_up = True

        for expected in (False, False, True):
            ready = health.run_once(
                CONFIG,
                state_path=self.state,
                local_probe=lambda: None,
                public_probe_runner=public_probe,
                restarter=restart,
                wall_clock=lambda: 1000,
                monotonic_clock=lambda: 0,
                sleeper=lambda _seconds: None,
            )
            self.assertEqual(ready, expected)
        self.assertEqual(calls, [health.TUNNEL_UNIT])
        self.assertEqual(
            health.load_state(self.state)["last_action"], "restart_tunnel"
        )

    def test_policy_failure_restarts_tailscale_then_tunnel_once(self) -> None:
        calls: list[str] = []
        recovered = False

        def public_probe() -> None:
            if not recovered:
                raise health.HealthError("tailscale_policy")

        def restart(unit: str) -> None:
            nonlocal recovered
            calls.append(unit)
            if unit == health.TUNNEL_UNIT:
                recovered = True

        for expected in (False, False, True):
            ready = health.run_once(
                CONFIG,
                state_path=self.state,
                local_probe=lambda: None,
                public_probe_runner=public_probe,
                restarter=restart,
                wall_clock=lambda: 1000,
                monotonic_clock=lambda: 0,
                sleeper=lambda _seconds: None,
            )
            self.assertEqual(ready, expected)
        self.assertEqual(calls, [health.TAILSCALE_UNIT, health.TUNNEL_UNIT])

    def test_recovery_cooldown_blocks_restart_storm(self) -> None:
        calls: list[str] = []
        ticks = iter((0, 100))

        def public_failure() -> None:
            raise health.HealthError("public_probe")

        for moment in (1000, 1001, 1002):
            health.run_once(
                CONFIG,
                state_path=self.state,
                local_probe=lambda: None,
                public_probe_runner=public_failure,
                restarter=calls.append,
                wall_clock=lambda moment=moment: moment,
                monotonic_clock=lambda: next(ticks),
                sleeper=lambda _seconds: None,
            )
        self.assertEqual(calls, [health.TUNNEL_UNIT])
        health.run_once(
            CONFIG,
            state_path=self.state,
            local_probe=lambda: None,
            public_probe_runner=public_failure,
            restarter=calls.append,
            wall_clock=lambda: 1100,
            monotonic_clock=lambda: 0,
            sleeper=lambda _seconds: None,
        )
        self.assertEqual(calls, [health.TUNNEL_UNIT])

    def test_status_must_be_recent_and_healthy(self) -> None:
        health.run_once(
            CONFIG,
            state_path=self.state,
            local_probe=lambda: None,
            public_probe_runner=lambda: None,
            wall_clock=lambda: 1000,
        )
        health.check_status(self.state, clock=lambda: 1089)
        with self.assertRaises(health.HealthError):
            health.check_status(self.state, clock=lambda: 1091)

    def test_restart_allowlist_rejects_every_other_unit(self) -> None:
        with self.assertRaises(health.HealthError):
            health.restart_unit("home-assistant.service")

    def test_restart_clears_systemd_rate_limit_before_restart(self) -> None:
        completed = mock.Mock(returncode=0)
        with mock.patch.object(health.subprocess, "run", return_value=completed) as runner:
            health.restart_unit(health.SKILL_UNIT)
        self.assertEqual(
            [call.args[0] for call in runner.call_args_list],
            [
                [health.SYSTEMCTL, "reset-failed", "--", health.SKILL_UNIT],
                [health.SYSTEMCTL, "restart", "--", health.SKILL_UNIT],
            ],
        )


if __name__ == "__main__":
    unittest.main()
