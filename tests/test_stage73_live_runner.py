from __future__ import annotations

import copy
import http.client
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "scripts"), str(Path(__file__).resolve().parent)]
import stage73_fixtures as fixture
import stage73_oracle as oracle
import run_stage73_live_canary as runner
import canary_write_adapter as write


class PreparedLiveRunnerTests(unittest.TestCase):
    def setUp(self):
        self.doc = fixture.document()
        self.config = fixture.config(self.doc)
        self.fake = fixture.FakeHA(self.doc)
        self.addCleanup(self.fake.close)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.adapter = write.CanaryWriteAdapter(fixture.FixtureVerifier(self.fake),
            config_loader=lambda: self.config, state_path=Path(temp.name) / "control",
            token_loader=lambda: "fixture-action-only")
        self.host, self.port = "127.0.0.1", self.fake.server.server_port
        transport = patch.object(write, "_connection", lambda: http.client.HTTPConnection(self.host, self.port, timeout=.1))
        transport.start()
        self.addCleanup(transport.stop)
        original = http.client.HTTPConnection.request
        def guard(connection, method, path, *args, **kwargs):
            if connection.host != self.host or connection.port != self.port:
                raise AssertionError("actual HA/network forbidden")
            return original(connection, method, path, *args, **kwargs)
        network = patch.object(http.client.HTTPConnection, "request", guard)
        network.start()
        self.addCleanup(network.stop)
        self.rows = []
        for record in self.config.records:
            name = next(e["display_name"] for e in self.doc["entities"] if e["entity_ref"] == record.entity_ref)
            for action, verb in (("turn_on", "включи"), ("turn_off", "выключи")):
                self.rows.append(dict(case_id=f"{record.canary_id}-{action}", utterance=f"{verb} {name}",
                    expected_outcome="plan", expected_human_target=name, expected_physical_name=name,
                    expected_area=record.registry_area, expected_domain=record.domain,
                    expected_action=action, owner_reviewed=True))

    def run_fake(self, rows=None):
        private = {r.canary_id: r.entity_id for r in self.config.records}
        return runner.run_cycles(self.rows if rows is None else rows, self.config, self.adapter,
            runner.Boundary(self.host, self.port, self.adapter.emergency_stop),
            lambda: copy.deepcopy(self.doc),
            lambda: oracle.read_states(self.host, self.port, "fixture-oracle", private),
            lambda: {key: "fixture-stable-binding" for key in private},
            cycles_per_target=1, authorize=lambda: None, fake=True)

    def test_full_runner_fake_cycles_duplicate_noop_rollback(self):
        result = self.run_fake()
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["fake_cycles"], 3)
        self.assertEqual(result["counts"]["LIVE_CYCLES"], 0)
        self.assertEqual(len(self.fake.posts), 6)  # 3 primary + 3 rollback; no-op/duplicate zero
        self.assertTrue(all(state == "off" for state in self.fake.states.values()))

    def test_no_owner_review_never_sends(self):
        with self.assertRaises(runner.LiveStop):
            self.run_fake([{**row, "owner_reviewed": False} for row in self.rows])
        self.assertEqual(self.fake.posts, [])

    def test_delayed_result_event_then_unverified_rollback_stops(self):
        self.fake.mode = "delayed"
        result = self.run_fake()
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["counts"]["ROLLBACK_FAILURE"], 1)
        self.assertEqual(result["cases"][0]["result_events"][0]["status"], "verified")
        self.assertEqual(len(self.fake.posts), 2)

    def test_independent_side_effect_stops_remaining_cycles(self):
        self.fake.mode = "other_changed"
        result = self.run_fake()
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(len(self.fake.posts), 1)
        self.assertGreater(result["counts"]["UNEXPECTED_CANARY_CHANGE"], 0)

    def test_independent_target_boundary_stops_wrong_target_before_send(self):
        boundary = runner.Boundary(self.host, self.port, self.adapter.emergency_stop)
        with boundary.installed(), boundary.permit(self.config.records[0], "turn_on"):
            with self.assertRaises(runner.LiveStop):
                http.client.HTTPConnection(self.host, self.port).request("POST", "/api/services/light/turn_on",
                    body='{"entity_id":"light.fake_other"}')
        self.assertEqual(self.fake.posts, [])
        self.assertEqual(boundary.metrics["WRONG_TARGET"], 1)
