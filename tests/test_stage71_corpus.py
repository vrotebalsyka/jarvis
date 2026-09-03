from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]

import bounded_ha_agent as agent
import stage71_corpus
import stage71_fixtures as fixtures
import stage71_oracle as oracle


def fake_model(_endpoint: object, _path: str, payload: dict, **_kwargs: object) -> dict:
    schema = payload.get("format", {})
    choices = schema.get("properties", {}).get("choice", {}).get("enum", []) if isinstance(schema, dict) else []
    if choices:
        return {"response": '{"choice":"clarify"}'}
    return {"message": {"content": "Здравствуйте."}}


class RawRussianCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = fixtures.graph()
        cls.snapshot = fixtures.snapshot()

    def turn(self, utterance: str, context: dict | None = None) -> agent.TurnResult:
        return agent.process_turn(
            utterance, context or {"session_focus": agent.SessionFocus()}, [],
            inventory_loader=lambda: self.graph,
            snapshot_reader=lambda _command: (self.snapshot, 0),
            endpoint_loader=lambda: object(), ollama_call=fake_model, trace_sink=None,
        )

    def test_at_least_five_hundred_raw_utterances_without_frames(self) -> None:
        rows = stage71_corpus.raw_corpus()
        direct = [row for row in rows if row["category"] == "raw_direct"]
        self.assertGreaterEqual(len(rows), 500)
        self.assertTrue({
            "morphology", "typo", "alias", "room_type", "feature_followup",
            "ambiguity", "correction", "general_conversation", "causal", "compound",
        } <= {row["category"] for row in rows})
        self.assertEqual(len({row["utterance"] for row in direct}), len(direct))
        reports = []
        for row in direct:
            result = self.turn(row["utterance"])
            expected = fixtures.target_ref(self.graph, row["target"])
            reports.append(oracle.evaluate_turn(
                result, self.graph, self.snapshot, [expected], [row["feature"]]
            ))
        self.assertEqual(oracle.combine(reports), oracle.OracleResult())

    def test_morphology_typo_alias_room_and_anti_regressions(self) -> None:
        rows = [
            row for row in stage71_corpus.raw_corpus()
            if row["category"] in {"morphology", "typo", "alias", "room_type"}
        ]
        reports = []
        for row in rows:
            result = self.turn(row["utterance"])
            reports.append(oracle.evaluate_turn(
                result, self.graph, self.snapshot,
                [fixtures.target_ref(self.graph, row["target"])], [row["feature"]],
            ))
        self.assertEqual(oracle.combine(reports), oracle.OracleResult())

    def test_compound_general_causal_ambiguity_and_control(self) -> None:
        compound = self.turn("какой заряд у Андрея и какой статус у Roborock S5 Max")
        pairs = {(item.target_ref, item.feature) for item in compound.frame.selections}
        self.assertEqual(pairs, {
            (fixtures.target_ref(self.graph, "Андрей"), "battery"),
            (fixtures.target_ref(self.graph, "Roborock S5 Max"), "status"),
        })
        self.assertEqual(self.turn("привет").frame.kind, "conversation")
        causal = self.turn("почему ошибка у Андрея?")
        self.assertIn("не сообщает причину", causal.answer)
        ambiguous = self.turn("покажи зеркало")
        self.assertEqual(ambiguous.frame.kind, "clarification")
        controlled = self.turn("включи питание посудомойки")
        self.assertEqual(controlled.frame.kind, "action")
        self.assertIsNone(controlled.action_plan)
        self.assertIn("запрещена политикой shadow", controlled.answer)
        self.assertEqual(controlled.receipts, ())

    def test_raw_followup_and_correction_sequences(self) -> None:
        for row in stage71_corpus.raw_corpus():
            if row["category"] not in {"feature_followup", "correction"}:
                continue
            context = {"session_focus": agent.SessionFocus()}
            self.turn(row["prior_utterance"], context)
            result = self.turn(row["utterance"], context)
            self.assertEqual(
                oracle.evaluate_turn(
                    result, self.graph, self.snapshot,
                    [fixtures.target_ref(self.graph, row["target"])], [row["feature"]],
                ),
                oracle.OracleResult(),
            )


if __name__ == "__main__":
    unittest.main()
