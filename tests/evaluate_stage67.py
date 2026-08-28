#!/usr/bin/env python3
"""Read-only live qualification for Stage 67 truthful device dialogue."""

from __future__ import annotations

import json
import os
import pwd
import re
import stat
import sys
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import alice_skill_gateway  # noqa: E402
import bounded_ha_agent  # noqa: E402
import ha_entity_query  # noqa: E402
import home_assistant_mcp  # noqa: E402
import home_assistant_read  # noqa: E402
import model_ha_proof  # noqa: E402
import owner_chat  # noqa: E402


def _load_inventory_for_qualification() -> dict[str, Any]:
    """Load the service-owned inventory without weakening runtime readers.

    The deployment driver is intentionally root-owned, while the inventory is
    intentionally private to ``homebutler``.  Qualification accepts only that
    exact owner, a regular non-linked 0600-style file, and the normal size cap.
    All production call paths keep using ``ha_entity_query.load_inventory``.
    """
    if os.geteuid() != 0:
        return ha_entity_query.load_inventory()
    path = ha_entity_query.incident_monitor._state_dir() / "inventory.json"
    try:
        expected_uid = pwd.getpwnam("homebutler").pw_uid
        metadata = path.lstat()
    except (KeyError, OSError) as error:
        raise ha_entity_query.EntityQueryError("inventory unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or metadata.st_mode & 0o077
        or not 0 < metadata.st_size <= ha_entity_query.MAX_INVENTORY_BYTES
    ):
        raise ha_entity_query.EntityQueryError("inventory unavailable")
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            raw = os.read(descriptor, ha_entity_query.MAX_INVENTORY_BYTES + 1)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise ha_entity_query.EntityQueryError("inventory unavailable") from error
    if len(raw) > ha_entity_query.MAX_INVENTORY_BYTES:
        raise ha_entity_query.EntityQueryError("inventory unavailable")
    try:
        document = home_assistant_read.strict_json_loads(raw)
    except home_assistant_read.AdapterError as error:
        raise ha_entity_query.EntityQueryError("inventory unavailable") from error
    if not isinstance(document, dict):
        raise ha_entity_query.EntityQueryError("inventory unavailable")
    return document


def _numbers(value: str) -> set[str]:
    return set(re.findall(r"(?<!\d)\d+(?:[.,]\d+)?(?!\d)", value))


def _allowed_numbers(document: Any) -> set[str]:
    raw = json.dumps(document, ensure_ascii=False)
    return _numbers(raw) | {"0", "1", "2"}


def main() -> int:
    results: list[dict[str, Any]] = []
    snapshot, exit_code = home_assistant_read.execute_safely("snapshot")
    inventory = _load_inventory_for_qualification()
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

    def qualification_agent(
        question: str,
        context: dict[str, Any],
        history: list[dict[str, str]],
        **kwargs: Any,
    ) -> str | None:
        return bounded_ha_agent.maybe_respond(
            question,
            context,
            history,
            inventory_loader=lambda: inventory,
            snapshot_reader=lambda operation: (
                (snapshot, 0)
                if operation == "snapshot"
                else home_assistant_read.execute_safely(operation)
            ),
            **kwargs,
        )

    def qualification_answerer(
        question: str,
        context: dict[str, Any],
        history: list[dict[str, str]],
    ) -> str:
        return owner_chat.answer_natural(
            question,
            context,
            history,
            voice=True,
            runtime_profile=alice_skill_gateway.VOICE_RUNTIME_PROFILE,
            natural_agent=qualification_agent,
            fallback_answerer=alice_skill_gateway.fast_model_answer,
        )

    application = alice_skill_gateway.SkillApplication(
        alice_skill_gateway.GatewayConfig(secret, skill_id, (owner_id,)),
        answerer=qualification_answerer,
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
