from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]

import bounded_ha_agent as agent
import stage71_fixtures as fixtures
import stage71_oracle as oracle


def no_model(*_args: object, **_kwargs: object) -> dict:
    raise AssertionError("model was not expected")


class ReceiptReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = fixtures.graph()
        self.snapshot = fixtures.snapshot()

    def turn(self, question: str, *, snapshot: dict | None = None, context: dict | None = None) -> agent.TurnResult:
        return agent.process_turn(
            question, context or {"session_focus": agent.SessionFocus()}, [],
            inventory_loader=lambda: self.graph,
            snapshot_reader=lambda _command: (snapshot or self.snapshot, 0),
            ollama_call=no_model, trace_sink=None,
        )

    def test_robot_docked_and_cleaning(self) -> None:
        andrew = self.turn("статус Андрей")
        roborock = self.turn("статус Roborock S5 Max")
        self.assertIn("на базе", andrew.answer)
        self.assertIn("убирает", roborock.answer)

    def test_dishwasher_off_running_and_conditional_control(self) -> None:
        power = self.turn("включи питание посудомойки")
        self.assertEqual(power.frame.kind, "action")
        self.assertIsNone(power.action_plan)
        self.assertEqual(power.receipts, ())
        running = self.turn(
            "статус посудомойки",
            snapshot=fixtures.snapshot({"sensor.dishwasher_status": ("enum", "running")}),
        )
        self.assertIn("работает", running.answer)
        self.assertEqual(self.snapshot["service_calls"], 0)
        conditional = self.turn("если посудомойка выключена, включи питание")
        self.assertIsNone(conditional.action_plan)

    def test_alternate_integration_does_not_hide_available_power(self) -> None:
        result = self.turn("питание посудомойки")
        self.assertEqual(len(result.receipts), 1)
        self.assertEqual((result.receipts[0].value_kind, result.receipts[0].value), ("on_off", "off"))

    def test_child_lock_off_never_becomes_on(self) -> None:
        result = self.turn("защита от детей у посудомойки")
        self.assertIn("выключено", result.answer)
        self.assertNotIn("включено", result.answer)

    def test_unknown_error_has_no_invented_explanation(self) -> None:
        result = self.turn("почему ошибка у Андрея?")
        self.assertIn("не сообщает причину", result.answer)
        self.assertIn("значение неизвестно", result.answer)
        self.assertNotRegex(result.answer.casefold(), r"потому что|из-за неисправ")

    def test_rinse_aid_problem_and_camera_enum(self) -> None:
        rinse = self.turn("ошибка посудомойки")
        self.assertIn("есть проблема", rinse.answer)
        camera = self.turn("режим камеры CW700S")
        self.assertIn("continuous", camera.answer)

    def test_unavailable_and_old_inventory_never_become_current(self) -> None:
        mirror_ref = fixtures.target_ref(self.graph, "зеркало", occurrence=1)
        frame = agent.IntentFrame("read", (agent.IntentSelection(mirror_ref, "power"),))
        receipts = agent.make_receipts(frame, self.graph, self.snapshot)
        self.assertEqual(receipts[0].value_kind, "unavailable")
        fresh = copy.deepcopy(self.snapshot)
        fresh["entities"] = [item for item in fresh["entities"] if item["entity_id"] != "sensor.andrew_battery"]
        andrew = fixtures.target_ref(self.graph, "Андрей")
        receipts = agent.make_receipts(
            agent.IntentFrame("read", (agent.IntentSelection(andrew, "battery"),)),
            self.graph, fresh,
        )
        self.assertEqual(receipts[0].value_kind, "unavailable")

    def test_number_unit_and_independent_oracle(self) -> None:
        result = self.turn("заряд у Андрея")
        self.assertIn("73%", result.answer)
        expected = fixtures.target_ref(self.graph, "Андрей")
        report = oracle.evaluate_turn(result, self.graph, self.snapshot, [expected], ["battery"])
        self.assertEqual(report, oracle.OracleResult())

    def test_boolean_binary_sensor_redacted_and_unknown_receipts(self) -> None:
        target = {"display_name": "Датчик", "kind": "logical", "areas": []}
        boolean = agent._receipt({
            "metadata": {
                "entity_ref": "safe-ref", "domain": "binary_sensor",
                "device_class": "motion", "unit": None,
            },
            "fresh_state": {
                "state_kind": "enum", "state_value": "off",
                "source_last_updated_at": "2026-09-02T10:00:00+00:00",
            },
            "observed_at": "2026-09-02T10:00:01+00:00",
        }, target, "target", "status", None)
        self.assertEqual((boolean.value_kind, boolean.value), ("boolean", False))
        self.assertIn("не обнаружено", agent._render_receipt(boolean))
        redacted = agent._receipt({
            "metadata": {"entity_ref": "safe-ref", "domain": "sensor"},
            "fresh_state": {"state_kind": "redacted", "state_value": None},
            "observed_at": "2026-09-02T10:00:01+00:00",
        }, target, "target", "status", None)
        self.assertEqual((redacted.value_kind, redacted.value), ("redacted", None))
        unknown = self.turn("неизвестный параметр у Андрея")
        self.assertEqual((unknown.receipts[0].feature, unknown.receipts[0].value_kind), ("unknown", "unknown"))

    def test_two_requested_features_are_both_preserved(self) -> None:
        result = self.turn("покажи статус и заряд у Андрея")
        self.assertEqual({item.feature for item in result.receipts}, {"status", "battery"})
        expected = fixtures.target_ref(self.graph, "Андрей")
        self.assertEqual(
            oracle.evaluate_turn(result, self.graph, self.snapshot, [expected], ["battery", "status"]),
            oracle.OracleResult(),
        )


class SessionFocusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = fixtures.graph()
        self.snapshot = fixtures.snapshot()
        self.focus = agent.SessionFocus()
        self.context = {"session_focus": self.focus}

    def turn(self, question: str, now: float) -> agent.TurnResult:
        return agent.process_turn(
            question, self.context, [], inventory_loader=lambda: self.graph,
            snapshot_reader=lambda _command: (self.snapshot, 0), ollama_call=no_model,
            clock=lambda: now,
        )

    def test_feature_followup_uses_last_target(self) -> None:
        self.turn("заряд у Андрея", 10.0)
        result = self.turn("а фильтр?", 11.0)
        self.assertIn("ресурс фильтра", result.answer)
        self.assertEqual(result.frame.selections[0].target_ref, fixtures.target_ref(self.graph, "Андрей"))

    def test_pending_clarification_and_correction(self) -> None:
        mirrors = [item for item in self.graph["physical_nodes"] if item["display_name"] == "зеркало"]
        mirrors[0]["aliases"].append("левое")
        mirrors[1]["aliases"].append("правое")
        first = self.turn("покажи зеркало", 20.0)
        self.assertEqual(first.frame.kind, "clarification")
        second = self.turn("правое", 21.0)
        self.assertEqual(second.frame.kind, "read")
        self.assertEqual(second.frame.selections[0].target_ref, mirrors[1]["target_ref"])
        corrected = self.turn("нет, Roborock S5 Max", 22.0)
        self.assertEqual(corrected.frame.selections[0].target_ref, fixtures.target_ref(self.graph, "Roborock S5 Max"))

    def test_correction_preserves_last_requested_feature(self) -> None:
        self.turn("заряд у Андрея", 30.0)
        corrected = self.turn("нет, Roborock S5 Max", 31.0)
        self.assertEqual(corrected.frame.selections[0].feature, "battery")
        self.assertIn("100%", corrected.answer)

    def test_focus_expires_and_sessions_are_isolated(self) -> None:
        self.turn("заряд у Андрея", 0.0)
        expired = self.turn("а фильтр?", agent.FOCUS_TTL_SECONDS + 1)
        self.assertEqual(expired.frame.kind, "clarification")
        other = agent.SessionFocus()
        self.assertIsNot(other, self.focus)
        self.assertEqual(other.last_target_refs, ())


class ModelIntentBoundaryTests(unittest.TestCase):
    def test_model_can_return_only_closed_intent_fields(self) -> None:
        captured: list[dict] = []
        def model(_endpoint: object, _path: str, payload: dict, **_kwargs: object) -> dict:
            captured.append(payload)
            return {"response": '{"a":"on","n":"зеркало","r":"ванная","t":"light"}'}
        fields = agent._parse_model_action_fields(
            "хочу чтобы зеркало в ванной светилось",
            endpoint_loader=lambda: object(), ollama_call=model,
        )
        self.assertEqual(fields, {
            "intent": "action", "action": "turn_on",
            "requested_name": "зеркало", "requested_area": "ванная",
            "requested_type": "light", "requested_feature": "power",
        })
        serialized = json.dumps(captured, ensure_ascii=False)
        self.assertNotRegex(serialized, agent.TECHNICAL_ID_RE)
        self.assertNotIn("CANDIDATES", serialized)
        self.assertNotIn("target_ref", serialized)
        with self.assertRaises(agent.BoundedAgentError):
            agent._parse_model_action_fields(
                "хочу света", endpoint_loader=lambda: object(),
                ollama_call=lambda *_a, **_k: {
                    "response": '{"a":"on","n":"sensor.bad","r":null,"t":"light"}'
                },
            )


if __name__ == "__main__":
    unittest.main()
