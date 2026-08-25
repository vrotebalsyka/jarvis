#!/usr/bin/env python3
"""Keep Phase 66 completion claims aligned with actual unclosed live gates."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import model_runtime_policy as policy  # noqa: E402


def report(name: str) -> str:
    return (PROJECT_DIR / "reports" / name).read_text(encoding="utf-8")


class Phase66EvidenceTests(unittest.TestCase):
    def test_acceptance_does_not_hide_scheduler_or_wake_live_gates(self) -> None:
        acceptance = report("PHASE-66-ACCEPTANCE.md")
        result = report("PHASE-66-RESULT.md")
        scheduler = report("PHASE-66-SCHEDULER.md")
        self.assertNotIn("Только O/P требуют", acceptance)
        for phrase in (
            "Реальная доставка local reminder",
            "Физическое wake-from-sleep",
        ):
            self.assertIn(phrase, acceptance)
        self.assertIn("live reminder/daily report delivery", result)
        self.assertIn("фактическое wake-from-sleep", result)
        self.assertIn("не считается live-квалифицированной", scheduler)

    def test_current_benchmark_header_matches_runtime_policy(self) -> None:
        benchmark = report("PHASE-66-MODEL-BENCHMARK.md")
        self.assertIn("Первоначально выбранные profiles — superseded", benchmark)
        self.assertIn(policy.PRODUCTION_MODEL, benchmark)
        self.assertIn(
            f"default\n> {policy.get_profile('dialogue').context_window // 1024}K",
            benchmark,
        )

    def test_benchmark_marks_old_source_only_state_as_history(self) -> None:
        benchmark = report("PHASE-66-MODEL-BENCHMARK.md")
        for phrase in (
            "Runtime Policy пока изменена только в Git working tree",
            "Изменения памяти пока находятся только в Git working tree",
            "Semantic diagnostic намеренно остаётся красным",
        ):
            self.assertNotIn(phrase, benchmark)
        for phrase in (
            "Текущий production readback",
            "`/api/ps context_length=32768`",
            "30/30 completed, P95 2.981 s",
            "isolated evaluator: 7/7",
            "693 tests OK, 1 skipped",
        ):
            self.assertIn(phrase, benchmark)

    def test_result_names_bounded_windows_tasks_and_no_runtime_powershell(self) -> None:
        result = report("PHASE-66-RESULT.md")
        self.assertIn("Home Butler Scheduler Wake Sync", result)
        self.assertIn("Home Butler Scheduler Wake", result)
        self.assertIn("Runtime actions не используют PowerShell", result)
        self.assertIn("`WakeToRun=true`", result)

    def test_deferred_alice_report_distinguishes_source_snapshot_from_runtime(self) -> None:
        deferred = report("PHASE-66-DEFERRED-ALICE-TASKS.md")
        self.assertNotIn("Изменения находятся только в working tree", deferred)
        for phrase in (
            "Текущий runtime readback",
            "`alice_skill_gateway.py`",
            "`owner_chat.py`",
            "`memory_store.py`",
            "693 tests OK, 1 skipped",
            "controlled O/P по-прежнему не выполнялся",
        ):
            self.assertIn(phrase, deferred)

    def test_playbook_report_separates_deployment_from_live_authority(self) -> None:
        playbooks = report("PHASE-66-RECOVERY-PLAYBOOKS.md")
        self.assertNotIn("Новые файлы ещё не\nразвёрнуты", playbooks)
        for phrase in (
            "развёрнуто в\n`/opt/home-butler`",
            "Source/runtime hashes совпадают",
            "action/recovery timers остаются `disabled/inactive`",
            "source-default — `dry_run`",
        ):
            self.assertIn(phrase, playbooks)

    def test_phase_reports_do_not_call_deployed_core_source_only(self) -> None:
        natural = report("PHASE-66-NATURAL-TOOL-LOOP.md")
        onboarding = report("PHASE-66-DEVICE-ONBOARDING.md")
        maintenance = report("PHASE-66-SAFE-MAINTENANCE.md")
        for document in (natural, onboarding, maintenance):
            self.assertIn("`/opt/home-butler`", document)
            self.assertIn("source/runtime hash", document.casefold())
        self.assertNotIn("этап не развёрнут", natural)
        self.assertIn("30/30 model-completed", natural)
        self.assertNotIn("Новый timer и очередь не", onboarding)
        self.assertIn("`pending_count=0`", onboarding)
        self.assertIn("`actions_performed=0`", onboarding)
        self.assertNotIn("Он не\nразвёрнут", maintenance)
        self.assertIn("Автоматического unit/timer нет", maintenance)
        self.assertIn("ручным owner-invoked инструментом", maintenance)

    def test_alice_owner_health_command_uses_fresh_status_not_raw_credentials(self) -> None:
        alice = report("PHASE-66-ALICE-HEALTH.md")
        result = report("PHASE-66-RESULT.md")
        command = "python3 /opt/home-butler/scripts/alice_skill_health.py --check-status"
        self.assertIn(command, alice)
        self.assertIn(command, result)
        self.assertNotIn("python3 scripts/alice_skill_health.py --probe-only", result)
        self.assertIn("systemd `LoadCredential`", alice)
        self.assertIn("controlled live stop/restart tests не выполнялись", alice)

    def test_owner_runtime_commands_use_deployed_service_account_state(self) -> None:
        result = report("PHASE-66-RESULT.md")
        for command in (
            "runuser -u homebutler -- python3 /opt/home-butler/scripts/"
            "persistent_scheduler.py --status",
            "runuser -u homebutler -- python3 /opt/home-butler/scripts/"
            "device_onboarding.py --show",
        ):
            self.assertIn(command, result)
        self.assertNotIn("python3 scripts/persistent_scheduler.py --status", result)
        self.assertNotIn("python3 scripts/device_onboarding.py --show", result)
        self.assertIn("scheduler DB\nи onboarding queue", result)
        self.assertIn("service account", result)

    def test_onboarding_report_proves_natural_flow_without_ha_write(self) -> None:
        onboarding = report("PHASE-66-DEVICE-ONBOARDING.md")
        result = report("PHASE-66-RESULT.md")
        for phrase in (
            "Текущая queue schema — 2",
            "частичные ответы",
            "proposal/approval через фактическую Qwen 4.7B без HA write",
            "Production read-only proof",
        ):
            self.assertIn(phrase, onboarding)
        for phrase in (
            "Подтверждаю предложение для Комнатный датчик.",
            "actions_performed=0",
            "Защищённый production POST",
        ):
            self.assertIn(phrase, result)


if __name__ == "__main__":
    unittest.main()
