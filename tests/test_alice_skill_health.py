#!/usr/bin/env python3
"""Contracts for component-isolated Alice health and bounded recovery."""

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


class FakeTime:
    def __init__(self) -> None:
        self.value = 0.0

    def clock(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def failure(code: str):
    def fail() -> None:
        raise health.HealthError(code)

    return fail


class AliceSkillHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        os.chmod(self.directory, 0o700)
        self.state = self.directory / "status.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def ready_probes(self) -> dict[str, object]:
        return {
            "tailscale_probe_runner": lambda: None,
            "model_endpoint_probe_runner": lambda: None,
            "model_loaded_probe_runner": lambda: None,
            "model_turn_probe_runner": lambda: None,
            "ha_read_probe_runner": lambda: None,
            "funnel_config_checker": lambda: None,
            "funnel_reasserter": lambda: None,
        }

    def run_health(self, **overrides):
        arguments = self.ready_probes()
        arguments.update(overrides)
        return health.run_once(CONFIG, state_path=self.state, **arguments)

    def test_transport_and_synthetic_requests_are_exact_owner_requests(self) -> None:
        raw = health.ping_request(CONFIG, session_id="alice-health-test-1234")
        document = json.loads(raw.decode("utf-8"))
        self.assertEqual(document["request"]["command"], "ping")
        self.assertEqual(document["session"]["skill_id"], CONFIG.skill_id)
        self.assertEqual(
            document["session"]["user"]["user_id"], CONFIG.owner_ids[0]
        )
        self.assertNotIn(SECRET.encode("ascii"), raw)
        for command, expected in (
            (gateway.HEALTH_MODEL_COMMAND, gateway.HEALTH_MODEL_TEXT),
            (gateway.HEALTH_HA_READ_COMMAND, gateway.HEALTH_HA_READ_TEXT),
        ):
            request = health.health_request(
                CONFIG,
                session_id="alice-health-test-1234",
                command=command,
            )
            self.assertEqual(json.loads(request)["request"]["command"], command)
            response = gateway.skill_response(expected)
            health.validate_probe_response(
                200,
                json.dumps(response, ensure_ascii=False).encode("utf-8"),
                "model_turn",
                expected,
            )

    def test_healthy_status_has_every_required_component(self) -> None:
        self.assertTrue(
            self.run_health(
                local_probe=lambda: None,
                public_probe_runner=lambda: None,
                wall_clock=lambda: 1000,
            )
        )
        document = health.load_state(self.state)
        self.assertEqual(document["schema_version"], 3)
        self.assertTrue(document["overall_voice_ready"])
        self.assertTrue(document["owner_config_ready"])
        for field in health.COMPONENT_FIELDS:
            self.assertTrue(document[field], field)
        self.assertEqual(document["consecutive_failures"], 0)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(SECRET, self.state.read_text(encoding="ascii"))

    def test_expensive_probes_are_cached_between_fast_transport_checks(self) -> None:
        model_turns: list[int] = []
        ha_reads: list[int] = []

        def run_at(moment: int) -> None:
            self.assertTrue(
                self.run_health(
                    local_probe=lambda: None,
                    public_probe_runner=lambda: None,
                    model_turn_probe_runner=lambda: model_turns.append(moment),
                    ha_read_probe_runner=lambda: ha_reads.append(moment),
                    wall_clock=lambda: moment,
                )
            )

        run_at(1000)
        run_at(1010)
        run_at(1030)
        run_at(1060)
        self.assertEqual(model_turns, [1000, 1060])
        self.assertEqual(ha_reads, [1000, 1030, 1060])

    def test_public_recovery_budget_after_confirmation_is_bounded(self) -> None:
        self.assertLessEqual(health.PUBLIC_RECOVERY_BUDGET_SECONDS, 45)

    def test_one_transient_failure_never_restarts_or_reasserts(self) -> None:
        calls: list[str] = []
        reassertions: list[str] = []
        ready = self.run_health(
            local_probe=failure("local_probe"),
            public_probe_runner=failure("public_probe"),
            restarter=calls.append,
            funnel_reasserter=lambda: reassertions.append("funnel"),
            wall_clock=lambda: 1000,
        )
        self.assertFalse(ready)
        self.assertEqual(calls, [])
        self.assertEqual(reassertions, [])
        self.assertEqual(health.load_state(self.state)["last_action"], "none")

    def test_probe_only_never_recovers_even_after_confirmed_failure(self) -> None:
        calls: list[str] = []
        for moment in (1000, 1010, 1020):
            self.run_health(
                local_probe=lambda: None,
                public_probe_runner=failure("public_probe"),
                restarter=calls.append,
                funnel_reasserter=lambda: calls.append("reassert"),
                wall_clock=lambda moment=moment: moment,
                allow_recovery=False,
            )
        self.assertEqual(calls, [])
        self.assertEqual(health.load_state(self.state)["last_action"], "none")

    def test_confirmed_gateway_failure_restarts_only_skill(self) -> None:
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

        self.assertFalse(
            self.run_health(
                local_probe=local_probe,
                public_probe_runner=public_probe,
                restarter=restart,
                wall_clock=lambda: 1000,
            )
        )
        self.assertTrue(
            self.run_health(
                local_probe=local_probe,
                public_probe_runner=public_probe,
                restarter=restart,
                wall_clock=lambda: 1010,
            )
        )
        self.assertEqual(calls, [health.SKILL_UNIT])
        self.assertEqual(health.load_state(self.state)["last_action"], "restart_skill")

    def test_public_route_reasserts_before_any_tunnel_restart(self) -> None:
        public_up = False
        calls: list[str] = []
        ladder: list[str] = []

        def public_probe() -> None:
            if not public_up:
                raise health.HealthError("public_probe")

        def reassert() -> None:
            nonlocal public_up
            ladder.append("reassert")
            public_up = True

        self.assertFalse(
            self.run_health(
                local_probe=lambda: None,
                public_probe_runner=public_probe,
                restarter=calls.append,
                funnel_config_checker=lambda: ladder.append("inspect"),
                funnel_reasserter=reassert,
                wall_clock=lambda: 1000,
            )
        )
        self.assertTrue(
            self.run_health(
                local_probe=lambda: None,
                public_probe_runner=public_probe,
                restarter=calls.append,
                funnel_config_checker=lambda: ladder.append("inspect"),
                funnel_reasserter=reassert,
                wall_clock=lambda: 1010,
            )
        )
        self.assertEqual(ladder, ["inspect", "reassert"])
        self.assertEqual(calls, [])
        self.assertEqual(health.load_state(self.state)["last_action"], "reassert_funnel")

    def test_public_route_restarts_only_tunnel_after_failed_reassert(self) -> None:
        public_up = False
        calls: list[str] = []
        ladder: list[str] = []

        def public_probe() -> None:
            if not public_up:
                raise health.HealthError("public_probe")

        def restart(unit: str) -> None:
            nonlocal public_up
            calls.append(unit)
            if unit == health.TUNNEL_UNIT:
                public_up = True

        self.run_health(
            local_probe=lambda: None,
            public_probe_runner=public_probe,
            restarter=restart,
            funnel_config_checker=lambda: ladder.append("inspect"),
            funnel_reasserter=lambda: ladder.append("reassert"),
            wall_clock=lambda: 1000,
        )
        self.assertTrue(
            self.run_health(
                local_probe=lambda: None,
                public_probe_runner=public_probe,
                restarter=restart,
                funnel_config_checker=lambda: ladder.append("inspect"),
                funnel_reasserter=lambda: ladder.append("reassert"),
                wall_clock=lambda: 1010,
            )
        )
        self.assertEqual(ladder, ["inspect", "reassert"])
        self.assertEqual(calls, [health.TUNNEL_UNIT])

    def test_tailscale_failure_restarts_tailscale_before_reasserting(self) -> None:
        tailscale_up = False
        public_up = False
        calls: list[str] = []
        ladder: list[str] = []

        def tailscale_probe() -> None:
            if not tailscale_up:
                raise health.HealthError("tailscale")

        def public_probe() -> None:
            if not public_up:
                raise health.HealthError("public_probe")

        def restart(unit: str) -> None:
            nonlocal tailscale_up
            calls.append(unit)
            if unit == health.TAILSCALE_UNIT:
                tailscale_up = True

        def reassert() -> None:
            nonlocal public_up
            ladder.append("reassert")
            public_up = True

        self.run_health(
            local_probe=lambda: None,
            public_probe_runner=public_probe,
            tailscale_probe_runner=tailscale_probe,
            restarter=restart,
            funnel_reasserter=reassert,
            wall_clock=lambda: 1000,
        )
        self.assertTrue(
            self.run_health(
                local_probe=lambda: None,
                public_probe_runner=public_probe,
                tailscale_probe_runner=tailscale_probe,
                restarter=restart,
                funnel_config_checker=lambda: ladder.append("inspect"),
                funnel_reasserter=reassert,
                wall_clock=lambda: 1010,
            )
        )
        self.assertEqual(calls, [health.TAILSCALE_UNIT])
        self.assertEqual(ladder, ["inspect", "reassert"])
        self.assertNotIn(health.SKILL_UNIT, calls)

    def test_model_endpoint_failure_never_restarts_tunnel(self) -> None:
        calls: list[str] = []
        for moment in (1000, 1010):
            self.assertFalse(
                self.run_health(
                    local_probe=lambda: None,
                    public_probe_runner=lambda: None,
                    model_endpoint_probe_runner=failure("model_endpoint"),
                    restarter=calls.append,
                    wall_clock=lambda moment=moment: moment,
                )
            )
        document = health.load_state(self.state)
        self.assertEqual(calls, [])
        self.assertEqual(document["last_action"], "await_model_supervisor")
        self.assertTrue(document["gateway_ready"])
        self.assertTrue(document["public_route_ready"])
        self.assertFalse(document["model_endpoint_ready"])

    def test_model_turn_is_rewarmed_without_restarting_transport(self) -> None:
        model_calls = 0
        restarts: list[str] = []

        def model_turn() -> None:
            nonlocal model_calls
            model_calls += 1
            if model_calls < 3:
                raise health.HealthError("model_turn")

        self.assertFalse(
            self.run_health(
                local_probe=lambda: None,
                public_probe_runner=lambda: None,
                model_turn_probe_runner=model_turn,
                restarter=restarts.append,
                wall_clock=lambda: 1000,
            )
        )
        self.assertTrue(
            self.run_health(
                local_probe=lambda: None,
                public_probe_runner=lambda: None,
                model_turn_probe_runner=model_turn,
                restarter=restarts.append,
                wall_clock=lambda: 1010,
            )
        )
        self.assertGreaterEqual(model_calls, 3)
        self.assertEqual(restarts, [])
        self.assertEqual(health.load_state(self.state)["last_action"], "warm_model")

    def test_ha_failure_keeps_transport_alive_and_restarts_nothing(self) -> None:
        calls: list[str] = []
        for moment in (1000, 1010, 1020):
            self.assertFalse(
                self.run_health(
                    local_probe=lambda: None,
                    public_probe_runner=lambda: None,
                    ha_read_probe_runner=failure("ha_read"),
                    restarter=calls.append,
                    wall_clock=lambda moment=moment: moment,
                )
            )
        document = health.load_state(self.state)
        self.assertTrue(document["gateway_ready"])
        self.assertTrue(document["public_route_ready"])
        self.assertFalse(document["ha_read_ready"])
        self.assertEqual(calls, [])

    def test_failed_recovery_uses_backoff_and_circuit_breaker(self) -> None:
        restarts: list[str] = []
        fake = FakeTime()
        public_failure = failure("public_probe")
        self.run_health(
            local_probe=lambda: None,
            public_probe_runner=public_failure,
            restarter=restarts.append,
            wall_clock=lambda: 1000,
            monotonic_clock=fake.clock,
            sleeper=fake.sleep,
        )
        self.run_health(
            local_probe=lambda: None,
            public_probe_runner=public_failure,
            restarter=restarts.append,
            wall_clock=lambda: 1010,
            monotonic_clock=fake.clock,
            sleeper=fake.sleep,
        )
        self.assertEqual(restarts, [health.TUNNEL_UNIT])
        document = health.load_state(self.state)
        self.assertEqual(document["next_recovery_epoch"]["public_route"], 1040)
        self.run_health(
            local_probe=lambda: None,
            public_probe_runner=public_failure,
            restarter=restarts.append,
            wall_clock=lambda: 1020,
            monotonic_clock=fake.clock,
            sleeper=fake.sleep,
        )
        self.assertEqual(restarts, [health.TUNNEL_UNIT])

        failures = {domain: 0 for domain in health.RECOVERY_DOMAINS}
        next_epochs = {domain: 0 for domain in health.RECOVERY_DOMAINS}
        circuits = {domain: 0 for domain in health.RECOVERY_DOMAINS}
        for index in range(5):
            health._record_recovery(
                failures,
                next_epochs,
                circuits,
                domain="public_route",
                success=False,
                now=2000 + index,
            )
        self.assertEqual(failures["public_route"], 5)
        self.assertGreaterEqual(circuits["public_route"], 2900)
        self.assertGreaterEqual(
            next_epochs["public_route"], circuits["public_route"]
        )

    def test_schema_two_status_migrates_without_claiming_model_ready(self) -> None:
        legacy = {
            "schema_version": 2,
            "observed_epoch": 900,
            "healthy": True,
            "consecutive_failures": 0,
            "local_ready": True,
            "public_ready": True,
            "last_action": "none",
            "last_error_code": "none",
            "last_recovery_epoch": 0,
        }
        self.state.write_text(json.dumps(legacy), encoding="ascii")
        os.chmod(self.state, 0o600)
        migrated = health.load_state(self.state)
        self.assertEqual(migrated["schema_version"], 3)
        self.assertTrue(migrated["gateway_ready"])
        self.assertTrue(migrated["public_route_ready"])
        self.assertFalse(migrated["model_turn_ready"])
        self.assertFalse(migrated["overall_voice_ready"])

    def test_owner_configuration_failure_is_isolated_and_never_recovers(self) -> None:
        health.record_configuration_failure(self.state, wall_clock=lambda: 1000)
        document = health.load_state(self.state)
        self.assertFalse(document["owner_config_ready"])
        self.assertFalse(document["overall_voice_ready"])
        self.assertEqual(document["last_error_code"], "configuration")
        self.assertEqual(document["last_action"], "none")
        self.assertEqual(document["last_recovery_epoch"], 0)

    def test_status_must_be_recent_and_fully_ready(self) -> None:
        self.run_health(
            local_probe=lambda: None,
            public_probe_runner=lambda: None,
            wall_clock=lambda: 1000,
        )
        health.check_status(self.state, clock=lambda: 1034)
        with self.assertRaises(health.HealthError):
            health.check_status(self.state, clock=lambda: 1036)

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
                [
                    health.SYSTEMCTL,
                    "restart",
                    "--no-block",
                    "--",
                    health.SKILL_UNIT,
                ],
            ],
        )


if __name__ == "__main__":
    unittest.main()
