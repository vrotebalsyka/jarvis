from __future__ import annotations

import ast
import importlib.util
import sys
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "tests" / "live_stage72_real_home_room_type_acceptance.py"
MANIFEST_PATH = ROOT / "tests" / "data" / "stage72_real_home_room_type_owner_reviewed.jsonl"


def load_runner():
    spec = importlib.util.spec_from_file_location("stage72_room_type_acceptance", RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("room/type acceptance runner is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Stage72RealHomeRoomTypeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = load_runner()

    def test_manifest_is_frozen_and_has_ten_real_targets(self) -> None:
        rows, digest = self.runner.load_owner_manifest(MANIFEST_PATH)
        self.assertEqual(digest, self.runner.EXPECTED_SHA256)
        self.assertEqual(len(rows), 42)
        self.assertEqual(Counter(row["category"] for row in rows), {
            "room_type_plan": 30, "capability_ambiguity": 12,
        })
        plans = [row for row in rows if row["expected_outcome"] == "plan"]
        self.assertEqual(len(plans), 30)
        self.assertEqual(len({row["expected_human_target"] for row in plans}), 10)

    def test_expected_side_does_not_import_production_resolver(self) -> None:
        tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
        imported = {
            alias.name for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names
        }
        self.assertNotIn("home_assistant_mcp", imported)
        source = RUNNER_PATH.read_text(encoding="utf-8")
        loader = source[source.index("def load_owner_manifest"):source.index("def run(")]
        self.assertNotIn("resolve_", loader.casefold())

    def test_real_path_and_gate_counters_are_required(self) -> None:
        source = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn("base.run(manifest_path, inventory_path)", source)
        self.assertIn('report["ha_post"]', source)
        self.assertIn('report["ha_service_paths"]', source)
        self.assertIn('report["service_calls"]', source)
        self.assertIn('cases[row["case_id"]]["requested_areas"]', source)
        self.assertIn('cases[row["case_id"]]["requested_types"]', source)
        self.assertIn('real_room_type_plans >= 20', source)


if __name__ == "__main__":
    unittest.main()
