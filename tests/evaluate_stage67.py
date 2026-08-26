#!/usr/bin/env python3
"""Read-only live qualification for Stage 67 truthful device dialogue."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import alice_skill_gateway  # noqa: E402
import ha_entity_query  # noqa: E402
import home_assistant_mcp  # noqa: E402
import home_assistant_read  # noqa: E402
import model_ha_proof  # noqa: E402
import owner_chat  # noqa: E402


def _numbers(value: str) -> set[str]:
    return set(re.findall(r"(?<!\d)\d+(?:[.,]\d+)?(?!\d)", value))


def _allowed_numbers(document: Any) -> set[str]:
    raw = json.dumps(document, ensure_ascii=False)
    return _numbers(raw) | {"0", "1", "2"}


def main() -> int:
    results: list[dict[str, Any]] = []
    snapshot, exit_code = home_assistant_read.execute_safely("snapshot")
    inventory = ha_entity_query.load_inventory()
    if exit_code != 0:
        raise SystemExit("HA snapshot unavailable")

    for phrase in ("Андрей", "Андрея", "Андрею", "Андреем", "об Андрее"):
        normalized = model_ha_proof.normalize_device_query(phrase)
        found = home_assistant_mcp.find_model_devices(
            inventory, query=normalized, limit=5
        )
        devices = found.get("devices")
        passed = (
            normalized == "Андрей"
            and isinstance(devices, list)
            and len(devices) == 1
            and devices[0].get("display_name") == "Андрей"
        )
        results.append({
            "name": f"resolve:{phrase}",
            "pass": passed,
            "normalized": normalized,
            "matched": len(devices) if isinstance(devices, list) else 0,
        })

    selected = home_assistant_mcp.find_model_devices(
        inventory, query="Андрей", limit=2
    )["devices"]
    if len(selected) != 1:
        raise SystemExit("physical device Андрей is not unique")
    physical_id = selected[0]["physical_device_id"]
    details = home_assistant_mcp.get_model_device_details(
        snapshot, inventory, physical_id
    )
    expected = model_ha_proof.render_device_observation(
        details, "Что с роботом Андреем?"
    )
    secret = "A" * 40
    skill_id = "stage67-skill"
    owner_id = "stage67-owner"
    application = alice_skill_gateway.SkillApplication(
        alice_skill_gateway.GatewayConfig(secret, skill_id, (owner_id,)),
        context={"mode": "stage67_read_only_qualification"},
    )
    try:
        response, route = application.process({
            "version": "1.0",
            "request": {
                "type": "SimpleUtterance",
                "original_utterance": "Что с роботом Андреем?",
                "command": "Что с роботом Андреем?",
            },
            "session": {
                "session_id": "stage67-session",
                "message_id": 1,
                "new": True,
                "skill_id": skill_id,
                "user": {"user_id": owner_id},
            },
        })
        actual = response["response"]["text"]
    finally:
        application.close()
    forbidden = (
        "сброс связи", "зависание модул", "полной мощности", "разных зонах",
    )
    actual_numbers = _numbers(actual)
    results.append({
        "name": "live_grounded_andrey_answer",
        "pass": (
            route not in {"model_starting", "empty", "provisioning"}
            and bool(actual.strip())
            and actual_numbers <= _allowed_numbers(details)
            and not any(value in actual.casefold() for value in forbidden)
            and "entity_id" not in actual
            and physical_id not in actual
        ),
        "expected_renderer": expected,
        "actual": actual,
    })

    all_pass = all(item["pass"] for item in results)
    print(json.dumps({
        "schema_version": 1,
        "stage": 67,
        "mode": "read_only",
        "all_pass": all_pass,
        "results": results,
    }, ensure_ascii=False, indent=2))
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
