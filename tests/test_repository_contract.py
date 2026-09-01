from __future__ import annotations

import ast
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RepositoryContractTests(unittest.TestCase):
    def test_forbidden_subsystems_are_absent(self) -> None:
        forbidden = {
            "device_learning.py", "ha_model_study.py", "install_verified_lessons.py",
            "export_training_dataset.py", "home_assistant_control.py", "model_ha_control.py",
            "model_ha_proof.py", "memory_store.py", "persistent_scheduler.py",
            "device_onboarding.py", "incident_monitor.py", "home_assistant_recovery.py",
        }
        present = {path.name for path in (ROOT / "scripts").glob("*") if path.is_file()}
        self.assertFalse(forbidden & present)

    def test_production_import_graph_has_no_deleted_layers(self) -> None:
        forbidden = {
            "device_learning", "ha_model_study", "model_ha_control", "model_ha_proof",
            "memory_store", "persistent_scheduler", "device_onboarding", "incident_monitor",
            "home_assistant_control", "turn_observability", "context_builder",
        }
        imports = set()
        for path in (ROOT / "scripts").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
        self.assertFalse(imports & forbidden)

    def test_only_expected_systemd_units_remain(self) -> None:
        units = {path.name for path in (ROOT / "config" / "systemd").iterdir()}
        self.assertEqual(len(units), 12)
        self.assertFalse(any("recovery" in name or "scheduler" in name for name in units))

    def test_tracked_markdown_is_closed_set(self) -> None:
        output = subprocess.check_output(["git", "ls-files", "*.md"], cwd=ROOT, text=True)
        tracked = set(output.splitlines())
        self.assertEqual(tracked, {
            "README.md", "AGENTS.md", "SECURITY.md", "SOUL.md", "ARCHITECTURE.md",
            "CURRENT-GOAL.md", "reports/STAGE-69-LIVE-AUDIT-2026-09-01.md",
        })


if __name__ == "__main__":
    unittest.main()
