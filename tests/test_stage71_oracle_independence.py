from __future__ import annotations

import ast
import sys
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]

import bounded_ha_agent as agent
import stage71_corpus
import stage71_fixtures as fixtures
import stage71_oracle as oracle


class OracleIndependenceTests(unittest.TestCase):
    def test_oracle_imports_no_production_resolver_or_renderer(self) -> None:
        path = ROOT / "tests" / "stage71_oracle.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        forbidden = {
            "bounded_ha_agent", "home_assistant_mcp", "home_assistant_inventory",
            "home_assistant_read", "owner_chat",
        }
        self.assertFalse(imported & forbidden)

    def test_oracle_detects_wrong_lost_and_invented(self) -> None:
        document = fixtures.graph()
        snapshot = fixtures.snapshot()
        expected = fixtures.target_ref(document, "Андрей")
        wrong = fixtures.target_ref(document, "Roborock S5 Max")
        frame = agent.IntentFrame("read", (agent.IntentSelection(wrong, "battery"),))
        result = agent.TurnResult(frame, agent.make_receipts(frame, document, snapshot), "fixture")
        report = oracle.evaluate_turn(result, document, snapshot, [expected], ["battery"])
        self.assertGreater(report.wrong_target, 0)

    def test_oracle_detects_answer_text_not_derived_from_receipts(self) -> None:
        document = fixtures.graph()
        snapshot = fixtures.snapshot()
        expected = fixtures.target_ref(document, "Андрей")
        frame = agent.IntentFrame("read", (agent.IntentSelection(expected, "battery"),))
        receipts = agent.make_receipts(frame, document, snapshot)
        result = agent.TurnResult(frame, receipts, "Андрей: заряд — 99%")
        report = oracle.evaluate_turn(result, document, snapshot, [expected], ["battery"])
        self.assertGreater(report.invented_facts, 0)

    def test_deterministic_p95_is_below_gate_on_full_raw_corpus(self) -> None:
        document = fixtures.graph()
        snapshot = fixtures.snapshot()
        durations = []
        for row in stage71_corpus.raw_corpus():
            if row["category"] != "raw_direct":
                continue
            started = time.perf_counter()
            agent.process_turn(
                row["utterance"], {"session_focus": agent.SessionFocus()}, [],
                inventory_loader=lambda: document,
                snapshot_reader=lambda _command: (snapshot, 0),
                ollama_call=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("model")),
            )
            durations.append(time.perf_counter() - started)
        durations.sort()
        p95 = durations[max(0, int(len(durations) * .95) - 1)]
        self.assertLessEqual(p95, 1.5)


if __name__ == "__main__":
    unittest.main()
