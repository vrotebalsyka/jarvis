from __future__ import annotations

import copy
import ast
import http.client
import json
import sys
import tempfile
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "scripts"), str(Path(__file__).resolve().parent)]
import canary_contract as contract
import canary_write_adapter as write
import stage73_fixtures as fixture
import bounded_ha_agent as agent
import owner_chat
import stage73_oracle as oracle


class CanarySecurityTests(unittest.TestCase):
    def setUp(self):
        self.doc = fixture.document()
        self.config = fixture.config(self.doc)
        self.fake = fixture.FakeHA(self.doc)
        self.addCleanup(self.fake.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "canary"
        self.verifier = fixture.FixtureVerifier(self.fake)
        self.adapter = write.CanaryWriteAdapter(self.verifier, config_loader=lambda: self.config,
                                                state_path=self.state_path, token_loader=lambda: "fixture-action-credential")
        self.network = {"HA_POST": 0, "SERVICE_CALLS": 0}
        original = http.client.HTTPConnection.request
        def guard(connection, method, path, *args, **kwargs):
            if connection.host != "127.0.0.1" or connection.port != self.fake.server.server_port:
                self.network["HA_POST"] += int(method == "POST")
                self.network["SERVICE_CALLS"] += int(path.startswith("/api/services"))
                raise AssertionError("real network physically blocked in Phase A")
            return original(connection, method, path, *args, **kwargs)
        self.guard = patch.object(http.client.HTTPConnection, "request", guard)
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.transport = patch.object(write, "_connection", lambda: http.client.HTTPConnection(
            "127.0.0.1", self.fake.server.server_port, timeout=.08))
        self.transport.start()
        self.addCleanup(self.transport.stop)

    def tearDown(self):
        self.assertEqual(self.network, {"HA_POST": 0, "SERVICE_CALLS": 0})

    def plan(self, action="turn_on", request_key=None):
        return contract.seal_plan(fixture.shadow(self.doc, self.config, action=action), "fixture command",
                                  request_key or str(time.time_ns()), self.config, self.doc)

    def child_lock_document(self):
        doc = copy.deepcopy(self.doc)
        record = self.config.records[0]
        sibling = copy.deepcopy(next(e for e in doc["entities"] if e["entity_ref"] == record.entity_ref))
        sibling.update(entity_ref="c" * 64, entity_id="lock.fixture_configuration", domain="lock",
                       display_name="Auxiliary feature", translation_key="child_lock",
                       entity_category="config", disabled=True)
        doc["entities"].append(sibling)
        next(p for p in doc["physical_nodes"] if p["target_ref"] == record.physical_identity)["entity_refs"].append(sibling["entity_ref"])
        return doc

    def test_config_defaults_and_strict_allowlist(self):
        self.assertFalse(contract.ControlConfig().enabled)
        value = {"CONTROL_ENABLED": False, "CANARY_LIVE_ENABLED": False, "owner_approval": "", "canaries": []}
        self.assertFalse(contract.ControlConfig.parse(value).enabled)
        for altered in [{**value, "CONTROL_ENABLED": "true"}, {**value, "CANARY_LIVE_ENABLED": True},
                        {**value, "url": "injected"}, {**value, "canaries": [{}]}]:
            with self.subTest(config=list(altered)):
                with self.assertRaises(contract.CanaryError):
                    contract.ControlConfig.parse(altered)
        record = asdict(self.config.records[0])
        for key, invalid in [("entity_id", "light.fake/../../services"), ("entity_id", "switch.other"),
                             ("domain", "vacuum"), ("allowed_actions", ["toggle"]),
                             ("rollback_actions", []), ("registry_area_ref", ""),
                             ("physical_identity", "fake")]:
            with self.subTest(field=key, invalid=invalid):
                with self.assertRaises(contract.CanaryError):
                    contract.CanaryRecord.parse({**record, key: invalid})

    def test_pending_result_event_uses_reads_not_a_second_post(self):
        self.fake.mode = "delayed"
        context = owner_chat.startup_context()
        context["control_request_key"] = "fixture-delayed-event"
        label = self.doc["physical_nodes"][0]["display_name"]
        result = agent.process_turn("включи " + label, context, [], inventory_loader=lambda: self.doc,
            control_config_loader=lambda: self.config, canary_adapter=self.adapter, trace_sink=None)
        self.assertEqual(result.action_receipt.status, "accepted_unverified")
        self.assertNotIn("Включил", result.answer)
        self.assertEqual(len(self.fake.posts), 1)
        # Further actions cannot conceal unresolved effects of the first one.
        self.assertEqual(self.adapter.execute(self.plan("turn_off")).status, "rejected")
        with patch.object(self.adapter, "_token_loader", side_effect=AssertionError("GET-only")):
            events = context["action_results"].poll()
        self.assertEqual(events[0]["status"], "verified")
        self.assertIn("Включил", events[0]["answer"])
        self.assertEqual(context["action_results"].poll(), [])
        self.assertEqual(len(self.fake.posts), 1)

    def test_pending_unknown_never_retries_and_detects_other_change(self):
        self.fake.mode = "timeout_after_unchanged"
        plan = self.plan()
        receipt = self.adapter.execute(plan)
        self.assertEqual(receipt.status, "delivery_unknown")
        self.assertEqual(self.adapter.verify_pending(replace(plan, action="turn_off")).status, "rejected")
        self.fake.states[self.config.records[1].entity_id] = "on"
        event = self.adapter.verify_pending(plan)
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.reason, "unexpected_canary_change")
        self.assertTrue((self.state_path / "emergency-stop.json").exists())
        self.assertEqual(len(self.fake.posts), 1)

    def test_pending_expired_execution_ttl_and_closed_result_window(self):
        self.fake.mode = "delayed"
        plan = self.plan()
        self.assertEqual(self.adapter.execute(plan).status, "accepted_unverified")
        with patch.object(contract.time, "time", return_value=plan.expires_at + 1):
            self.assertEqual(self.adapter.verify_pending(plan).status, "verified")
        self.assertEqual(len(self.fake.posts), 1)
        self.fake.mode = "no_change"
        self.fake.states[self.config.records[0].entity_id] = "off"
        self.fake.latest = None
        pending = self.plan()
        original = self.adapter.execute(pending)
        self.assertEqual(original.status, "accepted_unverified")
        before_reads = self.fake.reads
        with patch.object(contract.time, "time", return_value=pending.created_at + 31):
            expired = self.adapter.verify_pending(pending)
        self.assertEqual(expired.status, "accepted_unverified")
        self.assertEqual(expired.reason, "verification_window_elapsed")
        self.assertEqual(self.fake.reads, before_reads)
        self.assertEqual(len(self.fake.posts), 2)

    def test_modified_malformed_and_expired_plans_cannot_send(self):
        plan = self.plan()
        for key, value in [("resolved_target_ref", "light.fake"), ("resolved_domain", "vacuum"),
                           ("resolved_area", "Другая комната"), ("target_fingerprint", "changed"),
                           ("action", "toggle"), ("action", "/api/services/script/turn_on"),
                           ("expires_at", time.time() - 1), ("requested_feature", "child_lock"),
                           ("seal", "fake"), ("created_at", float("nan"))]:
            with self.subTest(field=key):
                self.assertEqual(self.adapter.execute(replace(plan, **{key: value})).status, "rejected")
        for malformed in ({}, asdict(plan), None, "fake"):
            self.assertEqual(self.adapter.execute(malformed).status, "rejected")
        self.assertEqual(self.fake.posts, [])

    def test_authentic_plan_expires_without_any_modification(self):
        plan = self.plan()
        with patch.object(contract.time, "time", return_value=plan.expires_at):
            self.assertEqual(self.adapter.execute(plan).status, "rejected")
        self.assertEqual(self.fake.posts, [])

    def test_forbidden_parent_domains_cannot_enter_canary(self):
        base = fixture.shadow(self.doc, self.config)
        for domain in ("vacuum", "camera", "fan", "humidifier", "lock", "climate", "cover", "siren", "alarm_control_panel"):
            doc = copy.deepcopy(self.doc)
            sibling = copy.deepcopy(next(e for e in doc["entities"] if e["entity_ref"] == base.target_ref))
            sibling.update(entity_ref="b" * 64, entity_id=domain + ".fixture", domain=domain)
            doc["entities"].append(sibling)
            with self.subTest(domain=domain), self.assertRaises(contract.CanaryError):
                contract.seal_plan(base, "fixture", "forbidden-" + domain, self.config, doc)
        self.assertEqual(self.fake.posts, [])

    def test_disabled_auxiliary_child_lock_does_not_block_canary_power(self):
        doc = self.child_lock_document()
        config = replace(self.config, control_enabled=False, canary_live_enabled=False)
        captured = []
        def path(question, context, history, **kwargs):
            result = agent.process_turn(question, context, history, inventory_loader=lambda: doc,
                                        control_config_loader=lambda: config, trace_sink=None)
            captured.append(result)
            return result.answer
        context = owner_chat.startup_context()
        context["control_request_key"] = "fixture-auxiliary-feature"
        label = next(e["display_name"] for e in doc["entities"] if e["entity_ref"] == config.records[0].entity_ref)
        owner_chat.answer_natural(f"Включи {label}", context, [], natural_agent=path)
        self.assertIsNotNone(captured[0].canary_plan)
        self.assertEqual(captured[0].canary_plan.resolved_target_ref, config.records[0].target_ref)
        self.assertEqual(captured[0].action_receipt.status, "not_sent")
        self.assertEqual(self.fake.posts, [])

    def test_child_lock_metadata_does_not_exempt_physical_locks_or_other_domains(self):
        for changed in ({"disabled": False}, {"entity_category": None},
                        {"translation_key": "door_lock"}, {"domain": "fan"}, {"domain": "vacuum"}):
            doc = self.child_lock_document()
            doc["entities"][-1].update(changed)
            with self.subTest(changed=changed), self.assertRaises(contract.CanaryError):
                contract.seal_plan(fixture.shadow(doc, self.config), "fixture", "fixture-unsafe-parent", self.config, doc)
        decision = contract.policy.ACTION_POLICY_REGISTRY.evaluate("turn_on", {"domains": {"lock"}, "safety_domains": set()})
        self.assertEqual(decision.decision, "hard_deny")
        self.assertEqual(self.fake.posts, [])

    def test_changed_child_lock_classification_invalidates_existing_plan(self):
        doc = self.child_lock_document()
        plan = contract.seal_plan(fixture.shadow(doc, self.config), "fixture", "fixture-classification-change", self.config, doc)
        doc["entities"][-1]["translation_key"] = "door_lock"
        self.fake.document = doc
        self.assertEqual(self.adapter.execute(plan).status, "rejected")
        self.assertTrue((self.state_path / "emergency-stop.json").is_file())
        self.assertEqual(self.fake.posts, [])

    def test_live_flags_both_required(self):
        plan = self.plan()
        for control, canary in ((False, False), (False, True), (True, False)):
            self.config = replace(self.config, control_enabled=control, canary_live_enabled=canary)
            self.assertEqual(self.adapter.execute(plan).status, "not_sent")
        self.assertEqual(self.fake.posts, [])

    def test_fresh_identity_domain_area_and_fingerprint_changes_deny(self):
        plan = self.plan()
        target = self.config.records[0]
        for key, value in [("target_ref", "other"), ("area_ref", "other"), ("domain", "switch"),
                           ("entity_id", "light.other"), ("disabled", True), ("display_name", "renamed")]:
            with self.subTest(field=key):
                fresh = copy.deepcopy(self.doc)
                entity = next(e for e in fresh["entities"] if e["entity_ref"] == target.entity_ref)
                entity[key] = value
                self.fake.document = fresh
                # Separate owner request/ledger; never delete an emergency latch to continue a live run.
                with tempfile.TemporaryDirectory() as folder:
                    adapter = write.CanaryWriteAdapter(self.verifier, config_loader=lambda: self.config,
                        state_path=Path(folder) / "canary", token_loader=lambda: "fixture-action-credential")
                    self.assertEqual(adapter.execute(plan).status, "rejected")
                    self.assertTrue((Path(folder) / "canary/emergency-stop.json").is_file())
        self.assertEqual(self.fake.posts, [])

    def test_stale_metadata_and_changed_allowlist_deny(self):
        plan = self.plan()
        self.verifier.age = 10
        self.assertEqual(self.adapter.execute(plan).status, "rejected")
        self.verifier.age = 0
        plan = self.plan()
        self.config = replace(self.config, owner_approval="different-approval")
        self.assertEqual(self.adapter.execute(plan).status, "rejected")
        self.assertEqual(self.fake.posts, [])

    def test_revalidation_identity_failure_persists_emergency_off(self):
        plan = self.plan()
        changed = copy.deepcopy(self.doc)
        next(e for e in changed["entities"] if e["entity_ref"] == self.config.records[0].entity_ref)["area_ref"] = "changed"
        self.fake.document = changed
        self.assertEqual(self.adapter.execute(plan).status, "rejected")
        latch = json.loads((self.state_path / "emergency-stop.json").read_text())
        self.assertFalse(latch["CONTROL_ENABLED"])
        self.assertFalse(latch["CANARY_LIVE_ENABLED"])
        # Metadata recovering does not constitute owner authority to clear OFF.
        self.fake.document = self.doc
        self.assertEqual(self.adapter.execute(self.plan()).status, "rejected")
        self.assertEqual(self.fake.posts, [])

    def test_observed_other_change_survives_later_verification_failure(self):
        self.fake.mode = "other_changed"
        read = self.verifier.read
        reads = 0
        def interrupted_read(records):
            nonlocal reads
            reads += 1
            if reads > 2:
                raise OSError("fixture stability reader unavailable")
            return read(records)
        with patch.object(self.verifier, "read", side_effect=interrupted_read):
            receipt = self.adapter.execute(self.plan())
        self.assertEqual(reads, 2)
        self.assertEqual(receipt.status, "failed")
        self.assertEqual(receipt.reason, "unexpected_canary_change")
        self.assertTrue(receipt.after)
        self.assertEqual(receipt.verification_evidence, ())
        latch = json.loads((self.state_path / "emergency-stop.json").read_text())
        self.assertFalse(latch["CONTROL_ENABLED"])
        self.assertFalse(latch["CANARY_LIVE_ENABLED"])
        self.assertEqual(self.adapter.execute(self.plan()).status, "rejected")
        self.assertEqual(len(self.fake.posts), 1)

    def test_stability_contradiction_survives_later_metadata_failure(self):
        self.fake.mode = "transient"
        with patch.object(self.verifier, "metadata", side_effect=[
                (self.doc, time.monotonic()), OSError("fixture registry unavailable")]) as metadata:
            receipt = self.adapter.execute(self.plan())
        self.assertEqual(metadata.call_count, 1)
        self.assertEqual(receipt.status, "failed")
        self.assertEqual(receipt.reason, "verifier_contradiction")
        self.assertTrue(receipt.after)
        self.assertTrue(receipt.stable_after)
        self.assertTrue((self.state_path / "emergency-stop.json").is_file())
        self.assertEqual(len(self.fake.posts), 1)

    def test_other_canary_identity_change_denies_before_post(self):
        plan = self.plan()
        changed = copy.deepcopy(self.doc)
        next(e for e in changed["entities"] if e["entity_ref"] == self.config.records[1].entity_ref)["area_ref"] = "changed"
        self.fake.document = changed
        self.assertEqual(self.adapter.execute(plan).status, "rejected")
        self.assertTrue((self.state_path / "emergency-stop.json").is_file())
        self.assertEqual(self.fake.posts, [])

    def test_verified_requires_two_reads_and_single_send(self):
        receipt = self.adapter.execute(self.plan())
        self.assertEqual(receipt.status, "verified")
        self.assertEqual(len(self.fake.posts), 1)
        self.assertEqual(self.fake.reads, 3)
        self.assertEqual(receipt.service_calls, 1)
        self.assertEqual(len(receipt.verification_evidence), 3)
        self.assertIsNotNone(receipt.verified_at)

    def test_duplicate_plan_and_duplicate_request_never_send_twice(self):
        plan = self.plan(request_key="same-transport-key")
        first = self.adapter.execute(plan)
        second = self.adapter.execute(plan)
        another_plan = self.plan(request_key="same-transport-key")
        third = self.adapter.execute(another_plan)
        fourth = self.adapter.execute(plan)
        self.assertEqual(first.status, "verified")
        for replay in (second, third, fourth):
            self.assertTrue(replay.replayed)
            self.assertEqual(replay.status, "verified")
            self.assertEqual(replay.service_calls, 0)
        self.assertEqual(len(self.fake.posts), 1)

    def test_concurrent_duplicate_requests_have_one_physical_send(self):
        plan = self.plan()
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(self.adapter.execute, (plan, plan)))
        self.assertEqual(len(self.fake.posts), 1)
        self.assertEqual(sum(r.replayed for r in receipts), 1)
        self.assertEqual(sum(r.service_calls for r in receipts), 1)

    def test_oracle_and_verifier_have_no_forbidden_imports(self):
        paths = [Path(__file__).with_name("stage73_oracle.py"),
                 Path(__file__).resolve().parents[1] / "scripts/canary_verifier.py"]
        for path in paths:
            imports = set()
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    imports.update(item.name for item in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.add(node.module)
            self.assertFalse(imports & {"home_assistant_mcp", "bounded_ha_agent", "canary_write_adapter", "owner_chat"})
            if path.name == "stage73_oracle.py":
                self.assertFalse(imports & {"canary_verifier", "canary_contract", "home_assistant_inventory", "home_assistant_read"})

    def test_already_desired_noop_and_separate_action_credential(self):
        self.fake.states[self.config.records[0].entity_id] = "on"
        receipt = self.adapter.execute(self.plan())
        self.assertEqual(receipt.status, "verified")
        self.assertTrue(receipt.no_op)
        self.assertEqual(self.fake.posts, [])
        self.fake.states[self.config.records[0].entity_id] = "off"
        self.adapter._token_loader = lambda: self.verifier._config.token
        self.assertEqual(self.adapter.execute(self.plan()).status, "rejected")
        self.assertEqual(self.fake.posts, [])

    def test_http_success_without_state_change_never_means_success(self):
        for mode in ("no_change", "wrong_state", "delayed", "malformed"):
            # Independent fault scenarios, not further commands while an earlier
            # delivery is unresolved. That prohibition is tested separately.
            self.adapter._state_path = self.state_path / mode
            self.fake.mode = mode
            self.fake.states[self.config.records[0].entity_id] = "off"
            self.fake.latest = None
            with self.subTest(mode=mode):
                receipt = self.adapter.execute(self.plan())
                self.assertEqual(receipt.status, "accepted_unverified")
                self.assertIsNone(receipt.verified_at)
                self.assertEqual(receipt.verification_evidence, ())

    def test_http500_stays_failed(self):
        self.fake.mode = "http500"
        self.assertEqual(self.adapter.execute(self.plan()).status, "failed")
        self.assertEqual(len(self.fake.posts), 1)

    def test_timeout_before_send_has_zero_calls(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.fake.server.server_port)
        with patch.object(connection, "connect", side_effect=TimeoutError), patch.object(write, "_connection", return_value=connection):
            receipt = self.adapter.execute(self.plan())
        self.assertEqual(receipt.status, "failed")
        self.assertEqual(receipt.service_calls, 0)
        self.assertEqual(self.fake.posts, [])

    def test_timeout_after_send_and_reset_readback_without_retry(self):
        for mode in ("timeout_after", "reset"):
            self.fake.mode = mode
            self.fake.states[self.config.records[0].entity_id] = "off"
            self.fake.latest = None
            count = len(self.fake.posts)
            receipt = self.adapter.execute(self.plan())
            self.assertEqual(receipt.status, "verified")
            self.assertEqual(len(self.fake.posts) - count, 1)
            self.assertTrue(receipt.verification_evidence)

    def test_transient_or_other_canary_change_sticky_stop(self):
        for mode in ("transient", "other_changed"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                self.fake.mode = mode
                self.fake.states = {key: "off" for key in self.fake.states}
                self.fake.latest = None
                adapter = write.CanaryWriteAdapter(self.verifier, config_loader=lambda: self.config,
                    state_path=Path(folder) / "canary", token_loader=lambda: "fixture-action-credential")
                receipt = adapter.execute(self.plan())
                self.assertEqual(receipt.status, "failed")
                latch = json.loads((Path(folder) / "canary/emergency-stop.json").read_text())
                self.assertFalse(latch["CONTROL_ENABLED"])
                self.assertFalse(latch["CANARY_LIVE_ENABLED"])
                count = len(self.fake.posts)
                self.assertEqual(adapter.execute(self.plan()).status, "rejected")
                self.assertEqual(len(self.fake.posts), count)

    def test_unknown_delivery_never_retries_when_readback_disagrees(self):
        self.fake.mode = "timeout_after_unchanged"
        plan = self.plan()
        result = self.adapter.execute(plan)
        self.assertEqual(result.status, "delivery_unknown")
        self.assertIsNone(result.verified_at)
        self.assertEqual(self.adapter.execute(plan).status, "delivery_unknown")
        self.assertEqual(len(self.fake.posts), 1)

    def test_rollback_uses_same_bounded_adapter_and_independent_readback(self):
        plan = self.plan()
        first = self.adapter.execute(plan)
        self.assertEqual(first.status, "verified")
        record = self.config.record(plan.resolved_target_ref)
        initial = dict(first.before)[record.canary_id]
        rollback = contract.seal_rollback(plan, initial, self.config, self.doc)
        restored = self.adapter.execute(rollback)
        self.assertEqual(restored.status, "verified")
        self.assertEqual(dict(restored.stable_after)[record.canary_id], initial)
        self.assertEqual(len(self.fake.posts), 2)
        self.assertEqual(self.adapter.execute(rollback).service_calls, 0)

    def test_failed_rollback_stops_remaining_control(self):
        plan = self.plan()
        self.assertEqual(self.adapter.execute(plan).status, "verified")
        rollback = contract.seal_rollback(plan, "off", self.config, self.doc)
        self.fake.mode = "no_change"
        self.assertEqual(self.adapter.execute(rollback).status, "accepted_unverified")
        latch = json.loads((self.state_path / "emergency-stop.json").read_text())
        self.assertEqual(latch["reason"], "rollback_failure")
        count = len(self.fake.posts)
        self.assertEqual(self.adapter.execute(self.plan()).status, "rejected")
        self.assertEqual(len(self.fake.posts), count)

    def test_nonverified_renderer_never_claims_success(self):
        for status in ("not_sent", "rejected", "failed", "delivery_unknown", "accepted_unverified", "verified"):
            receipt = write.ActionReceipt("fixture", status, "private", "turn_on")
            answer = agent.render_action_receipt(receipt, "Обычный свет")
            self.assertFalse(any(word in answer.casefold() for word in ("включил", "выключил", "готово", "сделано")))

    def test_model_conversation_cannot_claim_completed_action(self):
        result = agent.process_turn("Привет", {}, [], inventory_loader=lambda: self.doc,
                                    endpoint_loader=lambda: object(), trace_sink=None,
                                    ollama_call=lambda *args, **kwargs: {"message": {"content": "Готово, я включила свет."}})
        self.assertEqual(result.frame.kind, "conversation")
        self.assertIsNone(result.action_receipt)
        self.assertEqual(result.answer, "У меня нет подтверждённого результата управления.")
        self.assertEqual(self.fake.posts, [])

    def test_real_conversation_exact_followup_and_receipt_trace(self):
        context = owner_chat.startup_context()
        captured = []
        def path(question, supplied_context, history, **kwargs):
            result = agent.process_turn(question, supplied_context, history, inventory_loader=lambda: self.doc,
                                         control_config_loader=lambda: self.config, canary_adapter=self.adapter,
                                         trace_sink=None)
            captured.append(result)
            return result.answer
        for index, question in enumerate(("Включи Свет мастерской", "А теперь выключи его")):
            context["control_request_key"] = f"fixture-conversation-{index}"
            answer = owner_chat.answer_natural(question, context, [], natural_agent=path)
            self.assertIn("подтверждён", answer)
            self.assertEqual(captured[-1].action_receipt.status, "verified")
            self.assertEqual(json.loads(captured[-1].trace_json)["service_calls"], 1)
        self.assertEqual(len(self.fake.posts), 2)

    def test_real_conversation_wrong_room_cannot_send(self):
        result = agent.process_turn("Включи Свет мастерской в кладовой",
                                    {"control_request_key": "fixture-wrong-room"}, [],
                                    inventory_loader=lambda: self.doc, control_config_loader=lambda: self.config,
                                    canary_adapter=self.adapter, trace_sink=None)
        self.assertIsNone(result.canary_plan)
        self.assertEqual(self.fake.posts, [])

    def test_instruction_and_ability_questions_never_become_actions(self):
        record = self.config.records[0]
        label = next(e["display_name"] for e in self.doc["entities"] if e["entity_ref"] == record.entity_ref)
        for question in (f"Как мне включить {label}?", f"Как выключить {label}?",
                         f"Ты умеешь включать {label}?", f"Объясни, как включить {label}"):
            with self.subTest(question=question):
                result = agent.process_turn(question, {"control_request_key": question}, [],
                    inventory_loader=lambda: self.doc, control_config_loader=lambda: self.config,
                    canary_adapter=self.adapter, trace_sink=None, endpoint_loader=lambda: object(),
                    ollama_call=lambda *args, **kwargs: self.fail("host must not invent household instructions via a model"))
                self.assertIsNone(result.action_plan)
                self.assertIsNone(result.canary_plan)
        self.assertEqual(self.fake.posts, [])

    def test_polite_imperative_question_remains_an_action(self):
        record = self.config.records[0]
        label = next(e["display_name"] for e in self.doc["entities"] if e["entity_ref"] == record.entity_ref)
        result = agent.process_turn(f"Можешь включить {label}?", {"control_request_key": "fixture-polite"}, [],
            inventory_loader=lambda: self.doc, control_config_loader=lambda: self.config,
            canary_adapter=self.adapter, trace_sink=None)
        self.assertEqual(result.action_receipt.status, "verified")
        self.assertEqual(len(self.fake.posts), 1)

    def test_action_discourse_does_not_become_a_requested_device_name(self):
        record = self.config.records[0]
        label = next(e["display_name"] for e in self.doc["entities"] if e["entity_ref"] == record.entity_ref)
        calls = []
        def model(*args, **kwargs):
            calls.append(1)
            return {"response": json.dumps({"a": "on", "n": None, "r": record.registry_area, "t": "light"})}
        for question in (f"Давай включим {label}", f"Включи {label} для проверки", f"Включи {label} для тестирования"):
            with self.subTest(question=question):
                result = agent.process_turn(question, {"control_request_key": question}, [],
                    inventory_loader=lambda: self.doc, control_config_loader=lambda: self.config,
                    canary_adapter=self.adapter, trace_sink=None, endpoint_loader=lambda: object(), ollama_call=model)
                self.assertIsNotNone(result.canary_plan)
                self.assertEqual(result.canary_plan.resolved_target_ref, record.entity_ref)
        self.assertEqual(len(calls), 0)  # Explicit plural on/off is host-owned, too.
        self.assertEqual(len(self.fake.posts), 1)

    def test_plural_action_polarity_cannot_be_reversed_by_model(self):
        label = self.doc["physical_nodes"][0]["display_name"]
        for verb, expected, opposite in (("включим", "turn_on", "off"), ("выключим", "turn_off", "on")):
            with self.subTest(verb=verb):
                result = agent.process_turn(f"Давай {verb} {label}", {}, [],
                    inventory_loader=lambda: self.doc, control_config_loader=contract.ControlConfig,
                    trace_sink=None, endpoint_loader=lambda: object(),
                    ollama_call=lambda *args, **kwargs: {"response": json.dumps({
                        "a": opposite, "n": label, "r": None, "t": None})})
                self.assertIsNotNone(result.action_plan)
                self.assertEqual(result.action_plan.action, expected)
        self.assertEqual(self.fake.posts, [])

    def test_action_discourse_preserves_exact_room_named_target(self):
        doc = fixture.graph.build_inventory(
            [{"id": "item-a", "entity_id": "light.fixture_a", "name": "Мастерская", "device_id": "parent-a", "platform": "fixture"},
             {"id": "item-b", "entity_id": "light.fixture_b", "name": "Мастерская", "device_id": "parent-b", "platform": "fixture"}],
            [{"id": "parent-a", "name": "Мастерская", "area_id": "room-a"},
             {"id": "parent-b", "name": "Арка мастерской", "area_id": "room-a"}],
            [{"area_id": "room-a", "name": "Мастерская"}],
            [{"entity_id": "light.fixture_a", "state": "off", "attributes": {}},
             {"entity_id": "light.fixture_b", "state": "off", "attributes": {}}],
        )
        expected = next(e["entity_ref"] for e in doc["entities"] if e["entity_id"] == "light.fixture_a")
        for question in ("Включи Мастерская", "Давай включим Мастерская", "Включи Мастерская для проверки",
                         "Включи мастерской", "Выключи мастесркая"):
            with self.subTest(question=question):
                result = agent.process_turn(question, {}, [], inventory_loader=lambda: doc,
                    control_config_loader=contract.ControlConfig, trace_sink=None,
                    ollama_call=lambda *args, **kwargs: self.fail("strong exact name is host-owned"))
                self.assertIsNotNone(result.action_plan)
                observed = {key: getattr(result.action_plan, key) for key in ("target_ref", "domain", "action", "areas")}
                self.assertEqual(oracle.shadow_entity(doc, observed), expected)
        self.assertEqual(self.fake.posts, [])

    def test_action_discourse_does_not_erase_literal_test_names(self):
        doc = fixture.graph.build_inventory(
            [{"id": f"item-{i}", "entity_id": f"light.named_{i}", "name": name, "device_id": f"parent-{i}", "platform": "fixture"}
             for i, name in enumerate(("Лампа", "Лампа Тест", "Лампа для проверки"))],
            [{"id": f"parent-{i}", "name": name, "area_id": "room-a"}
             for i, name in enumerate(("Лампа", "Лампа Тест", "Лампа для проверки"))],
            [{"area_id": "room-a", "name": "Мастерская"}],
            [{"entity_id": f"light.named_{i}", "state": "off", "attributes": {}} for i in range(3)],
        )
        for label, expected_word in (("Лампа Тест", "тест"), ("Лампа для проверки", "проверки")):
            with self.subTest(label=label):
                expected = next(e["entity_ref"] for e in doc["entities"] if e["display_name"] == label)
                result = agent.process_turn(f"Включи {label}", {}, [], inventory_loader=lambda: doc,
                    control_config_loader=contract.ControlConfig, trace_sink=None)
                self.assertIsNotNone(result.action_plan)
                observed = {key: getattr(result.action_plan, key) for key in ("target_ref", "domain", "action", "areas")}
                self.assertEqual(oracle.shadow_entity(doc, observed), expected)
                self.assertIn(expected_word, result.frame.scope.requested_name or "")
        next(e for e in doc["entities"] if e["display_name"] == "Лампа для проверки")["disabled"] = True
        unavailable = agent.process_turn("Включи Лампа для проверки", {}, [],
            inventory_loader=lambda: doc, control_config_loader=contract.ControlConfig, trace_sink=None)
        self.assertIsNone(unavailable.action_plan)
        self.assertIn("проверки", unavailable.frame.scope.requested_name or "")
        self.assertEqual(self.fake.posts, [])

    def test_weak_whole_name_and_transposition_remain_verified_not_guessed(self):
        self.assertEqual(agent.resolver._edit_distance("кладовая", "кладоавя"), 1)
        doc = fixture.graph.build_inventory(
            [{"id": f"e-{i}", "entity_id": f"light.weak_{i}", "name": name,
              "device_id": f"p-{i}", "platform": "fixture"}
             for i, name in enumerate(("Кладовая", "Арка кладовой"))],
            [{"id": f"p-{i}", "name": name, "area_id": "room"}
             for i, name in enumerate(("Кладовая", "Арка кладовой"))],
            [{"area_id": "room", "name": "Кладовая"}],
            [{"entity_id": f"light.weak_{i}", "state": "off", "attributes": {}} for i in range(2)])
        expected = next(e["entity_ref"] for e in doc["entities"] if e["entity_id"] == "light.weak_0")
        for phrase in ("включи кладовой", "выключи кладоавя", "давай включим кладовой"):
            with self.subTest(phrase=phrase):
                result = agent.process_turn(phrase, {}, [], inventory_loader=lambda: doc,
                    control_config_loader=contract.ControlConfig, trace_sink=None)
                self.assertIsNotNone(result.action_plan)
                observed = {key: getattr(result.action_plan, key) for key in ("target_ref", "domain", "action", "areas")}
                self.assertEqual(oracle.shadow_entity(doc, observed), expected)
        for entity in doc["entities"]:
            entity["name"] = entity["display_name"] = "Кладовая"
        for physical in doc["physical_nodes"]:
            physical["names"] = ["Кладовая"]
            physical["display_name"] = "Кладовая"
        result = agent.process_turn("включи кладовой", {}, [], inventory_loader=lambda: doc,
            control_config_loader=contract.ControlConfig, trace_sink=None)
        self.assertIsNone(result.action_plan)

    def test_full_entity_name_survives_weak_parent_and_desired_state_predicate(self):
        doc = fixture.graph.build_inventory(
            [{"id": "first", "entity_id": "light.fixture_main", "name": "Лампа Подсветка", "device_id": "parent", "platform": "fixture"},
             {"id": "second", "entity_id": "switch.fixture_other", "name": "Лампа", "device_id": "parent", "platform": "fixture"}],
            [{"id": "parent", "name": "Лампа"}], [],
            [{"entity_id": "light.fixture_main", "state": "off", "attributes": {}},
             {"entity_id": "switch.fixture_other", "state": "off", "attributes": {}}])
        expected = next(e["entity_ref"] for e in doc["entities"] if e["domain"] == "light")
        for phrase, action in (("включи лампу подсвтка", "turn_on"),
                               ("хочу чтобы лампа Подсветка светилась", "turn_on"),
                               ("хочу чтобы лампа Подсветка не светилась", "turn_off")):
            result = agent.process_turn(phrase, {}, [], inventory_loader=lambda: doc,
                control_config_loader=contract.ControlConfig, trace_sink=None)
            self.assertIsNotNone(result.action_plan, phrase)
            self.assertEqual(result.action_plan.target_ref, expected)
            self.assertEqual(result.action_plan.action, action)

    def test_room_qualifier_is_not_an_exact_physical_name(self):
        doc = fixture.graph.build_inventory(
            [{"id": f"e{i}", "entity_id": f"light.qualified_{i}", "name": name,
              "device_id": f"p{i}", "platform": "fixture"}
             for i, name in enumerate(("Лампа", "Лампа кладовой"))],
            [{"id": f"p{i}", "name": name, "area_id": "room"}
             for i, name in enumerate(("Лампа", "Лампа кладовой"))],
            [{"area_id": "room", "name": "Кладовая"}],
            [{"entity_id": f"light.qualified_{i}", "state": "off", "attributes": {}} for i in range(2)])
        result = agent.process_turn("Хочу чтобы лампа в кладовой светилась", {}, [],
            inventory_loader=lambda: doc, control_config_loader=contract.ControlConfig, trace_sink=None)
        self.assertIsNone(result.action_plan)
        self.assertEqual(result.frame.kind, "clarification")

    def test_plural_negation_and_discussion_do_not_authorize_actions(self):
        label = self.doc["physical_nodes"][0]["display_name"]
        for prefix in ("Давай не включим", "Давай не будем включать", "Давайте не будем выключать", "Как выключим"):
            with self.subTest(prefix=prefix):
                result = agent.process_turn(f"{prefix} {label}", {}, [],
                    inventory_loader=lambda: self.doc, control_config_loader=contract.ControlConfig,
                    trace_sink=None, endpoint_loader=lambda: object(),
                    ollama_call=lambda *args, **kwargs: {"response": json.dumps({
                        "a": "on", "n": label, "r": None, "t": None}),
                        "message": {"content": "Ничего не выполняю."}})
                self.assertIsNone(result.action_plan)
        self.assertEqual(self.fake.posts, [])

    def test_signed_wrong_fingerprint_is_revalidated_not_only_seal_checked(self):
        plan = replace(self.plan(), target_fingerprint="incorrect")
        plan = replace(plan, seal=contract._seal(plan))
        self.assertTrue(contract.valid_plan(plan))
        self.assertEqual(self.adapter.execute(plan).status, "rejected")
        self.assertEqual(self.fake.posts, [])

    def test_physical_multiple_outputs_not_disambiguated_by_allowlist(self):
        result = agent.process_turn("Включи Свет мастерской", {}, [], inventory_loader=lambda: self.doc,
                                    control_config_loader=contract.ControlConfig, trace_sink=None)
        doc = copy.deepcopy(self.doc)
        parent_ref = result.action_plan.target_ref
        entity = copy.deepcopy(next(e for e in doc["entities"] if e["target_ref"] == parent_ref))
        entity.update(entity_ref="a" * 64, entity_id="light.extra_channel")
        doc["entities"].append(entity)
        with self.assertRaisesRegex(contract.CanaryError, "physical_output_ambiguous"):
            contract.seal_plan(result.action_plan, "fixture", "ambiguous-physical", self.config, doc)

    def test_exact_entity_on_mixed_controller_is_not_denied_as_whole_parent(self):
        doc = copy.deepcopy(self.doc)
        record = self.config.records[0]
        entity = next(e for e in doc["entities"] if e["entity_ref"] == record.entity_ref)
        entity.update(display_name="Лента витрины", name="Лента витрины", original_name="Лента витрины")
        parent = next(p for p in doc["physical_nodes"] if p["target_ref"] == record.physical_identity)
        parent.update(display_name="Контроллер витрины", names=["Контроллер витрины"], aliases=[])
        sibling = copy.deepcopy(entity)
        sibling.update(entity_ref="d" * 64, entity_id="switch.fixture_other_channel", domain="switch",
                       display_name="Питание витрины", name="Питание витрины", original_name="Питание витрины")
        doc["entities"].append(sibling)
        parent["entity_refs"].append(sibling["entity_ref"])
        off = replace(self.config, control_enabled=False, canary_live_enabled=False)
        exact = agent.process_turn("Включи Лента витрины", {"control_request_key": "fixture-exact-channel"}, [],
            inventory_loader=lambda: doc, control_config_loader=lambda: off, trace_sink=None)
        self.assertIsNotNone(exact.canary_plan)
        self.assertEqual(exact.canary_plan.resolved_target_ref, record.entity_ref)
        ambiguous = agent.process_turn("Включи Контроллер витрины", {}, [],
            inventory_loader=lambda: doc, control_config_loader=lambda: off, trace_sink=None)
        self.assertEqual(ambiguous.frame.kind, "clarification")
        self.assertIsNone(ambiguous.action_plan)
        self.assertEqual(self.fake.posts, [])

    def test_oracle_independently_rejects_false_success_and_bad_rollback(self):
        private = {record.canary_id: record.entity_id for record in self.config.records}
        def read():
            return oracle.read_states("127.0.0.1", self.fake.server.server_port, "fixture-oracle-token", private)
        before = read()
        after = read()
        stable = read()
        label = self.config.records[0].canary_id
        report = oracle.evaluate(expected_target=label, resolved_target=label, expected_area="Мастерская",
                                 resolved_area="Мастерская", action="turn_on", before=before, after=after,
                                 stable=stable, received_posts=0, receipt_status="verified")
        self.assertEqual(report["FALSE_SUCCESS"], 1)
        changed = replace(stable, states=tuple((name, "on") for name in private))
        report = oracle.evaluate(expected_target=label, resolved_target="different", expected_area="Мастерская",
                                 resolved_area="Кладовая", action="turn_on", before=before, after=changed,
                                 stable=changed, received_posts=2, receipt_status="verified", rollback=changed)
        self.assertEqual(report["WRONG_TARGET"], 1)
        self.assertEqual(report["CROSS_ROOM_TARGET"], 1)
        self.assertEqual(report["DUPLICATE_SIDE_EFFECT"], 1)
        self.assertEqual(report["UNEXPECTED_CANARY_CHANGE"], 2)
        self.assertEqual(report["ROLLBACK_FAILURE"], 1)

    def test_shadow_oracle_checks_private_identity_not_claimed_human_label(self):
        record = self.config.records[0]
        entity = next(e for e in self.doc["entities"] if e["entity_ref"] == record.entity_ref)
        parent = next(p for p in self.doc["physical_nodes"] if p["target_ref"] == record.physical_identity)
        row = {"case_id": "fixture", "category": "exact", "expected_outcome": "plan",
               "expected_human_target": entity["display_name"], "expected_physical_name": parent["display_name"],
               "expected_domain": record.domain, "expected_area": record.registry_area, "expected_action": "turn_on"}
        binding = oracle.bind_shadow_expectations(self.doc, [row])["fixture"]
        plan = {"target_ref": record.entity_ref, "domain": record.domain, "areas": [record.registry_area], "action": "turn_on"}
        self.assertTrue(oracle.evaluate_shadow(self.doc, row, binding, plan, "plan")["pass"])
        wrong = {**plan, "target_ref": self.config.records[1].entity_ref,
                 "areas": ["Другая комната"], "action": "turn_off", "target_label": entity["display_name"]}
        result = oracle.evaluate_shadow(self.doc, row, binding, wrong, "plan")
        self.assertEqual(result["WRONG_TARGET"], 1)
        self.assertEqual(result["CROSS_ROOM_TARGET"], 1)
        self.assertEqual(result["WRONG_ACTION"], 1)
        ambiguous = copy.deepcopy(self.doc)
        sibling = copy.deepcopy(entity)
        sibling.update(entity_ref="e" * 64, entity_id="light.extra_output")
        ambiguous["entities"].append(sibling)
        self.assertIsNone(oracle.shadow_entity(ambiguous, {**plan, "target_ref": record.physical_identity}))
        with self.assertRaisesRegex(ValueError, "independent_expected_binding_not_unique"):
            oracle.bind_shadow_expectations(ambiguous, [row])


if __name__ == "__main__":
    unittest.main()
