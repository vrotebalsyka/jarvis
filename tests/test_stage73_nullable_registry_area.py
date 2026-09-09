"""Synthetic nullable registry bindings; actual HA connections are forbidden."""
from __future__ import annotations

import copy
import http.client
import json
import sys
import tempfile
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "scripts"), str(Path(__file__).resolve().parent)]
import bounded_ha_agent as agent
import canary_contract as contract
import canary_write_adapter as write
import home_assistant_inventory as graph
import owner_chat
import shadow_action_policy as policy
import stage73_fixtures as fixture
import stage73_oracle as oracle
import run_stage73_live_canary as runner


ROOMS = ("Мастерская", "Кладовая", "Мансарда")
NAMES = ("Арка", "Торшер", "Бра")


def document(binding=None, *, entity_override=False, omit_area=False):
    areas = [{"area_id": f"room-{i}", "name": room} for i, room in enumerate(ROOMS)]
    devices = [{"id": f"device-{i}", "name": name, "area_id": None} for i, name in enumerate(NAMES)]
    entities = [{"id": f"entity-{i}", "entity_id": f"light.fixture_{i}", "name": name,
                 "device_id": f"device-{i}", "area_id": None, "platform": "fixture"} for i, name in enumerate(NAMES)]
    (entities if entity_override else devices)[0]["area_id"] = binding
    if omit_area:
        del entities[0]["area_id"]
    return graph.build_inventory(entities, devices, areas,
        [{"entity_id": row["entity_id"], "state": "off", "attributes": {}} for row in entities])


def config(doc, *, enabled=False):
    areas = {area["area_ref"]: area["name"] for area in doc["area_nodes"]}
    records = []
    for entity in sorted(doc["entities"], key=lambda e: NAMES.index(e["display_name"])):
        index = NAMES.index(entity["display_name"])
        records.append(dict(canary_id=f"nullable-{index}", target_ref=entity["entity_ref"],
            entity_ref=entity["entity_ref"], entity_id=entity["entity_id"], physical_identity=entity["target_ref"],
            domain=entity["domain"], registry_area_ref=entity["area_ref"], registry_area=areas.get(entity["area_ref"]),
            owner_area=ROOMS[index] if entity["area_ref"] is None else None,
            allowed_actions=["turn_on", "turn_off"], verification_profile="stable_boolean_state",
            rollback_actions=["turn_on", "turn_off"], allow_noop=True))
    return contract.ControlConfig.parse(dict(CONTROL_ENABLED=enabled, CANARY_LIVE_ENABLED=enabled,
        owner_approval="synthetic-only" if enabled else "", canaries=records))


class NullableRegistryAreaTests(unittest.TestCase):
    def setUp(self):
        self.doc = document()
        self.cfg = config(self.doc)
        self.fake = fixture.FakeHA(self.doc)
        self.addCleanup(self.fake.close)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.verifier = fixture.FixtureVerifier(self.fake)
        self.host, self.port = "127.0.0.1", self.fake.server.server_port
        transport = patch.object(write, "_connection", lambda: http.client.HTTPConnection(self.host, self.port, timeout=.1))
        transport.start()
        self.addCleanup(transport.stop)
        original = http.client.HTTPConnection.request
        self.real_attempts = 0
        def guard(connection, method, path, *args, **kwargs):
            if (connection.host, connection.port) != (self.host, self.port):
                self.real_attempts += 1
                raise AssertionError("actual_HA_forbidden")
            return original(connection, method, path, *args, **kwargs)
        network = patch.object(http.client.HTTPConnection, "request", guard)
        network.start()
        self.addCleanup(network.stop)

    def tearDown(self):
        self.assertEqual(self.real_attempts, 0)

    def adapter(self, name="control"):
        return write.CanaryWriteAdapter(self.verifier, config_loader=lambda: self.cfg,
            state_path=self.directory / name, token_loader=lambda: "fixture-action-only")

    def shadow(self, areas=(), requested=()):
        record = self.cfg.records[0]
        return policy.seal_action_plan(target_ref=record.entity_ref, target_label="Арка", areas=areas,
            domain=record.domain, action="turn_on", scope=policy.ActionScope(requested_areas=requested),
            decision=policy.ACTION_POLICY_REGISTRY.evaluate("turn_on", {"domains": {record.domain}}))

    def plan(self, shadow=None):
        return contract.seal_plan(self.shadow() if shadow is None else shadow, "synthetic intent",
            str(time.time_ns()), self.cfg, self.doc)

    def turn(self, utterance, adapter=None):
        results = []
        def core(question, context, history, **kwargs):
            result = agent.process_turn(question, context, history, inventory_loader=lambda: self.doc,
                control_config_loader=lambda: self.cfg, canary_adapter=adapter, trace_sink=None,
                ollama_call=lambda *a, **k: self.fail("exact target must not need a model"))
            results.append(result)
            return result.answer
        context = owner_chat.startup_context()
        context["control_request_key"] = "synthetic-" + str(time.time_ns())
        owner_chat.answer_natural(utterance, context, [], natural_agent=core)
        self.assertEqual(len(results), 1)
        return results[0]

    def test_null_is_paired_and_requires_separate_owner_room(self):
        row = asdict(self.cfg.records[0])
        self.assertIsNone(row["registry_area"])
        self.assertEqual(row["owner_area"], "Мастерская")
        for delta in ({"registry_area": "Мастерская"}, {"registry_area_ref": "a" * 64},
                      {"registry_area": "null"}, {"owner_area": None}, {"owner_area": ""},
                      {"owner_area": "   "}, {"owner_area": 7}):
            with self.subTest(delta=delta), self.assertRaises(contract.CanaryError):
                contract.CanaryRecord.parse({**row, **delta})
        for key in ("registry_area", "registry_area_ref", "owner_area"):
            with self.subTest(missing=key), self.assertRaises(contract.CanaryError):
                contract.CanaryRecord.parse({k: v for k, v in row.items() if k != key})

    def test_metadata_absence_is_positive_evidence_not_failed_lookup(self):
        self.assertTrue(all(e["registry_area_unassigned"] is True for e in self.doc["entities"]))
        for doc in (document("deleted-room"), document(7), document(omit_area=True)):
            entity = next(e for e in doc["entities"] if e["display_name"] == "Арка")
            self.assertIsNone(entity["area_ref"])
            self.assertIs(entity["registry_area_unassigned"], False)
            with self.assertRaises(contract.CanaryError):
                contract.binding_fingerprint(doc, self.cfg.records[0], "turn_on")

    def test_bound_registry_cannot_gain_owner_override_or_lose_binding(self):
        doc = document("room-0")
        cfg = config(doc)
        record = cfg.records[0]
        self.assertIsNone(record.owner_area)
        self.assertEqual(record.registry_area, "Мастерская")
        with self.assertRaises(contract.CanaryError):
            contract.CanaryRecord.parse({**asdict(record), "owner_area": "Мастерская"})
        with self.assertRaises(contract.CanaryError):
            contract.binding_fingerprint(self.doc, record, "turn_on")

    def test_public_shadow_path_seals_null_without_writes_or_registry_mutation(self):
        before = copy.deepcopy(self.doc)
        result = self.turn("включи Арка")
        self.assertIsNotNone(result.canary_plan)
        self.assertEqual(result.action_plan.areas, ())
        self.assertIsNone(result.canary_plan.registry_area)
        self.assertEqual(result.canary_plan.owner_area, "Мастерская")
        self.assertEqual(result.canary_plan.resolved_area, "Мастерская")
        self.assertEqual(result.action_receipt.status, "not_sent")
        self.assertEqual(self.fake.posts, [])
        self.assertEqual(self.doc, before)

    def test_owner_room_cannot_override_conflicting_shadow_evidence(self):
        for areas, requested in ((("Кладовая",), ()), ((), ("Кладовая",)),
                                 (("Мастерская", "Кладовая"), ()), ((), ("Мастерская", "Кладовая"))):
            with self.subTest(areas=areas, requested=requested), self.assertRaises(contract.CanaryError):
                self.plan(self.shadow(areas, requested))
        self.assertTrue(contract.valid_plan(self.plan(self.shadow(("Мастерская",), ("Мастерская",)))))
        self.assertEqual(self.fake.posts, [])

    def test_owner_allowlist_does_not_disambiguate_duplicate_physical_names(self):
        other = next(p for p in self.doc["physical_nodes"] if p["display_name"] == "Торшер")
        other["display_name"] = "Арка"
        other["names"] = ["Арка"]
        result = self.turn("включи Арка")
        self.assertEqual(result.frame.kind, "clarification")
        self.assertIsNone(result.canary_plan)
        self.assertEqual(self.fake.posts, [])

    def test_registry_assignment_or_unknown_metadata_rejects_before_post(self):
        self.cfg = replace(self.cfg, control_enabled=True, canary_live_enabled=True, owner_approval="synthetic-only")
        for i, fresh in enumerate((document("room-0"), document("room-1"), document("room-0", entity_override=True),
                                   document("deleted-room"), document(omit_area=True))):
            plan = self.plan()
            self.fake.document = fresh
            receipt = self.adapter(str(i)).execute(plan)
            self.assertEqual(receipt.status, "rejected")
            self.assertEqual(receipt.service_calls, 0)
            self.assertTrue((self.directory / str(i) / "emergency-stop.json").exists())
        self.assertEqual(self.fake.posts, [])

    def test_missing_absence_evidence_is_not_null_authority(self):
        for field in ("area_ref", "registry_area_unassigned"):
            fresh = copy.deepcopy(self.doc)
            del next(e for e in fresh["entities"] if e["display_name"] == "Арка")[field]
            with self.subTest(field=field), self.assertRaises(contract.CanaryError):
                contract.binding_fingerprint(fresh, self.cfg.records[0], "turn_on")

    def test_area_provenance_and_owner_config_are_sealed(self):
        self.cfg = replace(self.cfg, control_enabled=True, canary_live_enabled=True, owner_approval="synthetic-only")
        plan = self.plan()
        adapter = self.adapter()
        for delta in ({"registry_area": "Мастерская"}, {"owner_area": "Кладовая"}, {"resolved_area": "Кладовая"}):
            with self.subTest(delta=delta):
                self.assertEqual(adapter.execute(replace(plan, **delta)).status, "rejected")
        self.cfg = replace(self.cfg, records=(replace(self.cfg.records[0], owner_area="Кладовая"), *self.cfg.records[1:]))
        self.assertEqual(adapter.execute(plan).status, "rejected")
        self.assertEqual(self.fake.posts, [])

    def test_fake_verified_and_rollback_preserve_null_provenance(self):
        self.cfg = replace(self.cfg, control_enabled=True, canary_live_enabled=True, owner_approval="synthetic-only")
        adapter = self.adapter()
        result = self.turn("включи Арка", adapter)
        self.assertEqual(result.action_receipt.status, "verified")
        rollback = contract.seal_rollback(result.canary_plan, "off", self.cfg, self.doc)
        self.assertIsNone(rollback.registry_area)
        self.assertEqual(rollback.owner_area, "Мастерская")
        self.assertEqual(adapter.execute(rollback).status, "verified")
        self.assertEqual(adapter.execute(rollback).service_calls, 0)
        self.assertEqual(len(self.fake.posts), 2)
        self.assertTrue(all(state == "off" for state in self.fake.states.values()))

    def test_binding_appearing_after_post_never_becomes_verified(self):
        self.cfg = replace(self.cfg, control_enabled=True, canary_live_enabled=True, owner_approval="synthetic-only")
        plan = self.plan()
        adapter = self.adapter()
        with patch.object(self.verifier, "metadata", side_effect=[
                (self.doc, time.monotonic()), (document("room-0"), time.monotonic())]):
            receipt = adapter.execute(plan)
        self.assertEqual(receipt.status, "failed")
        self.assertEqual(receipt.reason, "identity_changed")
        self.assertIsNone(receipt.verified_at)
        self.assertEqual(len(self.fake.posts), 1)
        self.assertTrue((self.directory / "control" / "emergency-stop.json").exists())

    def test_prepared_runner_nullable_fake_cycles_with_frozen_owner_rooms(self):
        self.cfg = replace(self.cfg, control_enabled=True, canary_live_enabled=True, owner_approval="synthetic-only")
        rows = [dict(case_id=f"{i}-{action}", utterance=f"{verb} {name}", expected_outcome="plan",
                     expected_human_target=name, expected_physical_name=name, expected_area=ROOMS[i],
                     expected_domain="light", expected_action=action, owner_reviewed=True)
                for i, name in enumerate(NAMES) for action, verb in (("turn_on", "включи"), ("turn_off", "выключи"))]
        frozen = copy.deepcopy(rows)
        private = {r.canary_id: r.entity_id for r in self.cfg.records}
        adapter = self.adapter()
        def run(commands):
            return runner.run_cycles(commands, self.cfg, adapter,
                runner.Boundary(self.host, self.port, adapter.emergency_stop), lambda: copy.deepcopy(self.doc),
                lambda: oracle.read_states(self.host, self.port, "fixture-oracle", private),
                lambda: {k: "fixture-null-binding" for k in private},
                cycles_per_target=1, authorize=lambda: None, fake=True)
        for wrong_area in (None, "Неверная комната"):
            with self.subTest(area=wrong_area), self.assertRaises(runner.LiveStop):
                run([{**r, "expected_area": wrong_area} for r in rows])
        self.assertEqual(self.fake.posts, [])
        result = run(rows)
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["fake_cycles"], 3)
        self.assertEqual(result["counts"]["LIVE_CYCLES"], 0)
        self.assertEqual(len(self.fake.posts), 6)
        self.assertEqual(rows, frozen)
        self.assertTrue(all(state == "off" for state in self.fake.states.values()))


class IndependentNullableRegistryOracleTests(unittest.TestCase):
    def read(self, entity_area=None, parent_area=None, *, expected=None, omit_entity=False, omit_parent=False):
        entity = dict(id="fixture-registry", entity_id="light.fixture", device_id="fixture-parent", area_id=entity_area)
        parent = dict(id="fixture-parent", area_id=parent_area)
        if omit_entity:
            del entity["area_id"]
        if omit_parent:
            del parent["area_id"]
        messages = [{"type": "auth_required"}, {"type": "auth_ok"}]
        messages += [dict(id=i, success=True, result=rows) for i, rows in enumerate(
            ([entity], [parent], [dict(area_id="room", name="Мастерская")]), 1)]
        from unittest.mock import Mock
        socket = Mock()
        socket.recv.side_effect = [json.dumps(m) for m in messages]
        with patch("websocket.create_connection", return_value=socket):
            result = oracle.read_bindings("fixture.invalid", 8123, "fixture-only", {"lamp": "light.fixture"},
                                          {"lamp": expected})
        self.assertTrue(socket.close.called)
        return result

    def test_explicit_null_is_stable_but_not_unknown(self):
        first = self.read()
        self.assertEqual(first, self.read())
        for kwargs in (dict(omit_entity=True), dict(omit_parent=True), dict(entity_area="deleted"),
                       dict(parent_area="room"), dict(entity_area="room"), dict(parent_area=""), dict(entity_area=7)):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.read(**kwargs)

    def test_bound_registry_still_requires_exact_room(self):
        self.assertNotEqual(self.read(), self.read(parent_area="room", expected="Мастерская"))
        for kwargs in (dict(expected="Мастерская"), dict(parent_area="deleted", expected="Мастерская"),
                       dict(parent_area="room", expected="Кладовая")):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.read(**kwargs)


if __name__ == "__main__":
    unittest.main()
