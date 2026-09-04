from __future__ import annotations

import dataclasses
import json
import re
import sys
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]

import bounded_ha_agent as agent
import shadow_action_policy as policy
import stage72_corpus
import stage72_fixtures as fixtures


TECHNICAL = re.compile(
    r"(?:alarm_control_panel|binary_sensor|button|camera|climate|fan|light|lock|"
    r"script|sensor|switch|vacuum)\.[a-z0-9_]+|/api/services|target_ref|entity_id",
    re.IGNORECASE,
)


class SelectingModel:
    def __init__(self, content: str = '{"choice":"r1"}') -> None:
        self.content = content
        self.calls = 0
        self.payloads: list[dict] = []

    def __call__(self, _endpoint: object, path: str, payload: dict, **_kwargs: object) -> dict:
        self.calls += 1
        self.payloads.append(payload)
        if path != "/api/generate":
            raise AssertionError("unexpected model endpoint")
        serialized = json.dumps(payload.get("prompt"), ensure_ascii=False)
        if TECHNICAL.search(serialized):
            raise AssertionError("technical identifier crossed model boundary")
        return {"response": self.content}


class Stage72ShadowActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = fixtures.graph()

    def turn(self, utterance: str, model: SelectingModel | None = None) -> agent.TurnResult:
        selector = model or SelectingModel()
        return agent.process_turn(
            utterance, {"session_focus": agent.SessionFocus()}, [],
            inventory_loader=lambda: self.graph,
            snapshot_reader=lambda _command: (_ for _ in ()).throw(
                AssertionError("shadow planning attempted a Home Assistant read")
            ),
            endpoint_loader=lambda: object(), ollama_call=selector, trace_sink=None,
        )

    def test_exactly_one_thousand_raw_commands_and_zero_gates(self) -> None:
        rows = stage72_corpus.raw_corpus()
        self.assertEqual(len(rows), 1_000)
        self.assertEqual(Counter(row["category"] for row in rows), {
            "exact": 300, "area_type": 200, "morphology_typo": 150,
            "ambiguity": 150, "cross_room": 100, "prompt_injection": 50,
            "compound_unsupported": 50,
        })
        counters = Counter({
            "wrong_target": 0, "cross_room_target": 0,
            "ambiguous_side_effect": 0, "unsupported_action_planned": 0,
        })
        for row in rows:
            result = self.turn(row["utterance"])
            trace = json.loads(result.trace_json or "null")
            self.assertEqual(trace["service_calls"], 0)
            self.assertEqual(trace["ha_post"], 0)
            self.assertIsNone(TECHNICAL.search(result.trace_json or ""))
            if row["outcome"] == "plan":
                self.assertIsNotNone(result.action_plan, row["utterance"])
                plan = result.action_plan
                assert plan is not None
                counters["wrong_target"] += plan.target_label != row["target"]
                counters["unsupported_action_planned"] += plan.action != row["action"]
                self.assertTrue(policy.verify_action_plan(plan))
                self.assertEqual(plan.service_calls, 0)
                self.assertEqual(result.frame.action, plan.action)
                self.assertEqual(result.frame.value, plan.value)
                self.assertEqual(result.frame.scope, plan.scope)
            else:
                if result.action_plan is not None:
                    if row["category"] == "ambiguity":
                        counters["ambiguous_side_effect"] += 1
                    elif row["category"] == "cross_room":
                        counters["cross_room_target"] += 1
                    else:
                        counters["unsupported_action_planned"] += 1
        self.assertEqual(counters, Counter({
            "wrong_target": 0, "cross_room_target": 0,
            "ambiguous_side_effect": 0, "unsupported_action_planned": 0,
        }))

    def test_equal_candidates_always_clarify_without_model(self) -> None:
        model = SelectingModel()
        for utterance in (
            "включи зеркало", "выключи основной свет", "включи освещение в ванной",
        ):
            result = self.turn(utterance, model)
            self.assertEqual(result.frame.kind, "clarification")
            self.assertIsNone(result.action_plan)
            self.assertEqual(json.loads(result.trace_json or "null")["policy"]["reason"], "equal_candidates")
        self.assertEqual(model.calls, 0)

    def test_strong_unique_host_evidence_bypasses_model(self) -> None:
        model = SelectingModel('{"a":"none","n":null,"r":null,"t":null}')
        result = self.turn("отключи Реле вентилятора", model)
        self.assertIsNotNone(result.action_plan)
        self.assertEqual(result.action_plan.target_label, "Реле вентилятора")
        self.assertEqual(model.calls, 0)
        trace = json.loads(result.trace_json or "null")
        self.assertEqual(trace["target_decision"]["evidence"], "strong")
        self.assertEqual(trace["intent"]["parser"], "deterministic")

    def test_bounded_model_fallback_parses_fields_but_host_resolves(self) -> None:
        model = SelectingModel(
            '{"a":"on","n":null,"r":"туалет","t":"light"}'
        )
        result = self.turn("хотелось бы света в туалете", model)
        self.assertIsNotNone(result.action_plan)
        self.assertEqual(result.action_plan.target_label, "Основной свет туалета")
        self.assertEqual(model.calls, 1)
        self.assertIsNone(TECHNICAL.search(json.dumps(model.payloads, ensure_ascii=False)))
        trace = json.loads(result.trace_json or "null")
        self.assertEqual(trace["intent"]["parser"], "model")
        self.assertEqual(trace["target_decision"]["evidence"], "strong")

    def test_action_follow_up_uses_only_ephemeral_session_focus(self) -> None:
        context = {"session_focus": agent.SessionFocus()}
        first = agent.process_turn(
            "включи Основной свет туалета", context, [],
            inventory_loader=lambda: self.graph,
            snapshot_reader=lambda _command: (_ for _ in ()).throw(AssertionError("HA read")),
            endpoint_loader=lambda: object(), ollama_call=SelectingModel(), trace_sink=None,
        )
        second = agent.process_turn(
            "а теперь выключи его", context, [],
            inventory_loader=lambda: self.graph,
            snapshot_reader=lambda _command: (_ for _ in ()).throw(AssertionError("HA read")),
            endpoint_loader=lambda: object(), ollama_call=SelectingModel(), trace_sink=None,
        )
        self.assertEqual(first.action_plan.target_label, "Основной свет туалета")
        self.assertEqual(second.action_plan.target_label, "Основной свет туалета")
        self.assertEqual(second.action_plan.action, "turn_off")
        self.assertEqual(json.loads(second.trace_json or "null")["target_decision"]["resolution_tier"], "session_focus")

    def test_owner_blind_corpus_is_one_hundred_percent(self) -> None:
        path = ROOT / "tests" / "data" / "stage72_blind_owner.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        passed = 0
        for row in rows:
            result = self.turn(row["utterance"])
            if row["outcome"] == "plan":
                plan = result.action_plan
                passed += bool(
                    plan is not None and plan.target_label == row["target"]
                    and plan.action == row["action"] and policy.verify_action_plan(plan)
                )
            else:
                passed += result.action_plan is None
        self.assertEqual(len(rows), 40)
        self.assertEqual(passed, len(rows))

    def test_requested_pairs_never_cross_target(self) -> None:
        allowed = {
            "включи Основной свет ванной": "Основной свет ванной",
            "включи Основной свет кабинета": "Основной свет кабинета",
            "включи Основной свет туалета": "Основной свет туалета",
            "включи Ночник коридора": "Ночник коридора",
            "включи Основной свет кухни": "Основной свет кухни",
            "включи Зеркало ванной": "Зеркало ванной",
            "включи Реле вентилятора": "Реле вентилятора",
        }
        for utterance, expected in allowed.items():
            result = self.turn(utterance)
            self.assertIsNotNone(result.action_plan, utterance)
            self.assertEqual(result.action_plan.target_label, expected)
        for utterance in (
            "включи свет ванной в кабинете",
            "включи свет туалета в прихожей",
            "включи свет кухни в коридоре",
            "включи зеркало ванной и основной свет кабинета",
        ):
            self.assertIsNone(self.turn(utterance).action_plan, utterance)

    def test_hard_deny_vacuum_button_appliance_lock_climate_script_and_fan(self) -> None:
        commands = (
            "запусти Андрей", "запусти Roborock S5 Max", "нажми Кнопка звонка",
            "включи посудомойка", "разблокируй Замок входной двери",
            "установи Термостат кухни", "запусти Вечерний сценарий",
            "включи Вытяжка",
        )
        for command in commands:
            result = self.turn(command)
            self.assertIsNone(result.action_plan, command)
            self.assertEqual(json.loads(result.trace_json or "null")["service_calls"], 0)
        self.assertIsNone(self.turn("включи Вытяжка").action_plan)
        self.assertIsNotNone(self.turn("включи Реле вентилятора").action_plan)

    def test_plan_is_immutable_sealed_and_non_executable(self) -> None:
        plan = self.turn("включи Основной свет кухни").action_plan
        self.assertIsNotNone(plan)
        assert plan is not None
        with self.assertRaises(dataclasses.FrozenInstanceError):
            plan.value = False  # type: ignore[misc]
        self.assertFalse(policy.verify_action_plan(dataclasses.replace(plan, value=not plan.value)))
        fields = {field.name for field in dataclasses.fields(plan)}
        self.assertFalse(fields & {"entity_id", "device_id", "service", "service_path", "capability_id"})
        self.assertFalse(any(name.startswith(("execute", "dispatch", "call_service")) for name in dir(policy)))

    def test_model_output_cannot_smuggle_technical_ids(self) -> None:
        model = SelectingModel('{"a":"on","n":"light.kitchen","r":null,"t":"light"}')
        with self.assertRaises(agent.BoundedAgentError):
            self.turn("хотелось бы света на кухне", model)

    def test_one_registry_and_no_ha_network_dependency(self) -> None:
        source = (ROOT / "scripts" / "shadow_action_policy.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("ACTION_POLICY_REGISTRY = ActionPolicyRegistry()"), 1)
        self.assertNotIn("home_assistant_read", source)
        self.assertNotIn("http.client", source)


if __name__ == "__main__":
    unittest.main()
