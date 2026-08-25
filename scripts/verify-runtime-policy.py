#!/usr/bin/env python3
"""Prove that the installed service prompt contains every required policy."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from model_runtime_policy import get_profile


RUNTIME_DIR = Path("/opt/home-butler")
EXPECTED_HOME = "/home/homebutler"
EXPECTED_HERMES_HOME = "/home/homebutler/.hermes"
RUNTIME_PROFILE = get_profile("dialogue")
REQUIRED_MARKERS = (
    "# Home Butler Mission",
    "Единственный допустимый текст health-отчёта",
    "# Разрешённые инструменты",
    "Ты спокойный, аккуратный и неболтливый управляющий",
    "# Diagnose Home Assistant",
    "# Diagnose Internet",
    "# Diagnose MQTT",
    "# Diagnose Zigbee2MQTT",
    "# Home Health Audit",
)


def main() -> int:
    if (
        os.geteuid() == 0
        or os.environ.get("HOME") != EXPECTED_HOME
        or os.environ.get("HERMES_HOME") != EXPECTED_HERMES_HOME
        or Path.cwd() != RUNTIME_DIR
    ):
        print("RUNTIME_POLICY_FAILED", file=sys.stderr)
        return 2
    sys.path.insert(0, str(RUNTIME_DIR / "hermes-agent"))
    try:
        from agent.prompt_builder import build_context_files_prompt

        context = build_context_files_prompt(
            cwd=str(RUNTIME_DIR),
            skip_soul=False,
            context_length=RUNTIME_PROFILE.context_window,
        )
    except Exception:
        print("RUNTIME_POLICY_FAILED", file=sys.stderr)
        return 2
    if not all(marker in context for marker in REQUIRED_MARKERS):
        print("RUNTIME_POLICY_FAILED", file=sys.stderr)
        return 2
    print("RUNTIME_POLICY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
