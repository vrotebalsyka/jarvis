from __future__ import annotations

import ast
import hashlib
import importlib.util
import sys
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "tests" / "live_stage72_real_home_acceptance.py"
MANIFEST_PATH = ROOT / "tests" / "data" / "stage72_real_home_owner_reviewed.jsonl"
MANIFEST_SHA256 = "52bc316294cdbccad90f191a5d8147c2ea430506e3e98ddafa46a4b0d35c0fe7"


def load_runner():
    spec = importlib.util.spec_from_file_location("stage72_real_home_acceptance", RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("acceptance runner is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Stage72RealHomeAcceptanceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = load_runner()

    def test_manifest_is_frozen_and_has_required_distribution(self) -> None:
        rows, digest = self.runner.load_owner_manifest(MANIFEST_PATH)
        self.assertEqual(digest, MANIFEST_SHA256)
        self.assertEqual(len(rows), 60)
        self.assertEqual(Counter(row["category"] for row in rows), {
            "exact": 20,
            "room_type": 15,
            "morphology_typo": 10,
            "ambiguity_cross_room": 10,
            "forbidden": 5,
        })

    def test_expected_side_has_no_production_resolver_dependency(self) -> None:
        tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertNotIn("home_assistant_mcp", imported)
        source = RUNNER_PATH.read_text(encoding="utf-8")
        manifest_loader = source[source.index("def load_owner_manifest"):source.index("def load_metadata_only_inventory")]
        self.assertNotIn("resolver", manifest_loader.casefold())
        self.assertNotIn("resolve_", manifest_loader.casefold())

    def test_runner_uses_real_model_and_physically_blocks_ha_post(self) -> None:
        source = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertNotIn("SelectingModel", source)
        self.assertNotIn("ScriptedModel", source)
        self.assertIn("agent.call_ollama", source)
        self.assertIn("owner_chat.answer_natural", source)
        self.assertIn("agent.process_turn", source)
        self.assertIn("HTTPConnection.request = guarded_request", source)
        self.assertIn('method_upper == "POST"', source)


if __name__ == "__main__":
    unittest.main()
