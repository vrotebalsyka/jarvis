#!/usr/bin/env python3
"""Opt-in live regression for collector -> Ollama -> trusted renderer."""

from __future__ import annotations

import copy
import os
import subprocess
import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import health_report  # noqa: E402
import health_report_core as core  # noqa: E402


@unittest.skipUnless(
    os.environ.get("RUN_LIVE_OLLAMA") == "1",
    "set RUN_LIVE_OLLAMA=1 to call the local model",
)
class LiveHealthReportTests(unittest.TestCase):
    def test_live_collector_model_and_renderer_are_deterministic(self) -> None:
        process = subprocess.run(
            [str(SCRIPTS_DIR / "local-health-check.sh")],
            check=True,
            capture_output=True,
            timeout=30,
        )
        snapshot = core.parse_snapshot_bytes(process.stdout)
        core.ensure_snapshot_fresh(snapshot)
        analysis = core.analyze_snapshot(snapshot)
        model = os.environ.get("MODEL", "home-butler")

        reports: list[str] = []
        for _attempt in range(2):
            payload = health_report.build_model_payload(snapshot, analysis, model)
            model_output = health_report.call_ollama(payload)
            selection = core.postvalidate_model_output(model_output, analysis)
            reports.append(
                health_report.render_report(snapshot, analysis, selection)
            )

        self.assertEqual(reports[0].encode("utf-8"), reports[1].encode("utf-8"))
        self.assertRegex(reports[0], r"[А-Яа-яЁё]")
        self.assertNotIn("deployment", reports[0].lower())
        self.assertNotIn("развёртывание", reports[0].lower())
        if analysis.problems:
            self.assertTrue(reports[0].startswith("ТРЕБУЕТСЯ ВНИМАНИЕ\n"))
            self.assertNotIn("HEARTBEAT_OK", reports[0])
        else:
            self.assertTrue(reports[0].startswith("HEARTBEAT_OK\n"))

        clean = copy.deepcopy(snapshot)
        clean["host"]["memory_used_percent"] = min(
            clean["host"]["memory_used_percent"],
            50,
        )
        clean["host"]["swap_used_percent"] = 0
        for disk in clean["disks"]:
            if disk["used_percent"] >= 90:
                disk["used_bytes"] = 0
                disk["available_bytes"] = disk["total_bytes"]
                disk["used_percent"] = 0
        clean["failed_systemd_units"] = []
        clean["probes"]["systemd"] = "ok"
        clean["temperatures"] = []
        clean["probes"]["temperatures"] = "unavailable"
        clean["hermes"] = {
            "installed": True,
            "gateway_configured": False,
            "gateway_running": False,
            "status": "not_configured",
        }
        clean["probes"]["hermes_gateway"] = "not_configured"
        clean["home_assistant"] = {
            "configured": False,
            "status": "not_configured",
        }
        clean = core.validate_snapshot(clean)
        clean_analysis = core.analyze_snapshot(clean)
        self.assertEqual(clean_analysis.problems, ())
        clean_payload = health_report.build_model_payload(
            clean,
            clean_analysis,
            model,
        )
        clean_output = health_report.call_ollama(clean_payload)
        clean_selection = core.postvalidate_model_output(
            clean_output,
            clean_analysis,
        )
        clean_report = health_report.render_report(
            clean,
            clean_analysis,
            clean_selection,
        )
        self.assertTrue(clean_report.startswith("HEARTBEAT_OK\n"))
        self.assertNotIn("Подтверждённые проблемы", clean_report)

        problem = copy.deepcopy(clean)
        problem["failed_systemd_units"] = ["synthetic-health-check.service"]
        problem = core.validate_snapshot(problem)
        problem_analysis = core.analyze_snapshot(problem)
        problem_output = health_report.call_ollama(
            health_report.build_model_payload(problem, problem_analysis, model)
        )
        problem_report = health_report.render_report(
            problem,
            problem_analysis,
            core.postvalidate_model_output(problem_output, problem_analysis),
        )
        self.assertTrue(problem_report.startswith("ТРЕБУЕТСЯ ВНИМАНИЕ\n"))
        self.assertIn("synthetic-health-check.service", problem_report)
        self.assertNotIn("HEARTBEAT_OK", problem_report)


if __name__ == "__main__":
    unittest.main()
