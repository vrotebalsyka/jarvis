#!/usr/bin/env python3
"""Live Stage 70 GET-only acceptance; emits no entity IDs or credentials."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import bounded_ha_agent as agent  # noqa: E402
import home_assistant_inventory as inventory_builder  # noqa: E402
import home_assistant_mcp as resolver  # noqa: E402
import home_assistant_read as adapter  # noqa: E402
import owner_chat  # noqa: E402


TARGETS = (
    ("Андрей", r"^Андрей$"),
    ("Roborock S5 Max", r"^Roborock S5 Max$"),
    ("обхаркиватель", r"^обхаркиватель$"),
    ("посудомойка", r"^Dishwasher$"),
    ("24G Presence Sensor", r"^24G-Presence Sensor V3$"),
    ("камера CW700S", r"^Xiaomi Outdoor Camera CW700S$"),
    ("BASE", r"^BASE$"),
    ("ночник", r"^ночник$"),
    ("зеркало", r"^зеркало$"),
    ("реле вентилятора", r"^реле вентилятора$"),
    ("кабинет", r"^кабинет$"),
    ("кухня", r"^кухня$"),
    ("коридор", r"^коридор$"),
    ("Ванная туале", r"^Ванная туале$"),
    ("туалет прихожка", r"^туалет прихожка$"),
    ("гардероб", r"^гардероб$"),
    ("вытяжка", r"^вытяжка$"),
    ("Вытяжка на кухне", r"^Вытяжка на кухне$"),
    ("Яндекс Станция Макс", r"^Яндекс Станция Макс PM2B$"),
    ("Станция Мини", r"^Станция Мини new$"),
)
TEMPLATES = (
    "Что сейчас показывает {query}?",
    "Проверь, пожалуйста, {query}.",
    "Какое текущее состояние у {query}?",
    "Есть свежие данные про {query}?",
    "Скажи состояние устройства {query} сейчас.",
)


def fresh_snapshot(config: adapter.AdapterConfig) -> dict[str, Any]:
    status, entities = adapter._states(config)
    return {
        "schema_version": 1, "observed_at": adapter._now_iso(), "status": status,
        "entities": entities, "service_calls": 0,
    }


def run(token_file: Path) -> dict[str, Any]:
    token = token_file.read_text(encoding="ascii").strip()
    if adapter.TOKEN_RE.fullmatch(token) is None:
        raise RuntimeError("credential is invalid")
    config = adapter.AdapterConfig(
        adapter.EXPECTED_SCHEME, adapter.EXPECTED_HOST, adapter.EXPECTED_PORT,
        token, (), True,
    )
    snapshot = fresh_snapshot(config)
    inventory = inventory_builder.collect_inventory(
        config,
        snapshot_reader=lambda _command: (snapshot, 0),
    )
    devices = inventory["physical_devices"]
    audited_hashes: set[str] = set()
    target_rows: list[dict[str, Any]] = []
    answers = 0
    wrong_device = invented = lost = technical_leaks = model_fallbacks = 0

    for query, pattern in TARGETS:
        expected = [item for item in devices if re.search(pattern, str(item.get("display_name")), re.I)]
        found = resolver.find_model_devices(inventory, query=query, limit=32)["devices"]
        expected_hashes = {item["physical_device_hash"] for item in expected}
        found_hashes = {item["physical_device_id"] for item in found}
        if not expected_hashes or not expected_hashes.issubset(found_hashes):
            lost += len(expected_hashes - found_hashes) or 1
        if found_hashes - expected_hashes:
            wrong_device += len(found_hashes - expected_hashes)
        details = [
            resolver.get_model_device_details(snapshot, inventory, item["physical_device_hash"])
            for item in expected
        ]
        for item in expected:
            audited_hashes.add(item["physical_device_hash"])
        row_lost = 0
        for template in TEMPLATES:
            question = template.format(query=query)
            if query == "Андрей" and answers == 0:
                question = "Сколько процентов ресурса основной щётки осталось у Андрея?"
            called_model = False
            def tracked_model(*args: Any, **kwargs: Any) -> dict[str, Any]:
                nonlocal called_model
                called_model = True
                return agent.call_ollama(*args, **kwargs)
            answer = owner_chat.answer_natural(
                question, owner_chat.startup_context(), [],
                natural_agent=lambda q, c, h, **kw: agent.respond(
                    q, c, h,
                    inventory_loader=lambda: inventory,
                    snapshot_reader=lambda _command: (snapshot, 0),
                    ollama_call=tracked_model,
                    **kw,
                ),
            )
            answers += 1
            model_fallbacks += int(called_model)
            if agent.TECHNICAL_ID_RE.search(answer) or agent.SECRET_RE.search(answer):
                technical_leaks += 1
            for detail in details:
                name = agent._safe_text(detail.get("display_name"), fallback="Устройство")
                if name not in answer:
                    row_lost += 1
                for index, feature in enumerate(detail.get("features", []), 1):
                    rendered = agent._render_feature(feature, index)
                    if rendered not in answer:
                        row_lost += 1
            # Any non-static words in this path originate only from selected HA facts.
            expected_answer = agent.render_grounded(details, question)
            if answer != expected_answer:
                invented += 1
        lost += row_lost
        target_rows.append({
            "target": query,
            "physical_devices_read": len(expected),
            "available_features": sum(
                feature["availability"] == "available"
                for detail in details for feature in detail["features"]
            ),
            "unavailable_features": sum(
                feature["availability"] == "unavailable"
                for detail in details for feature in detail["features"]
            ),
            "lost_values": row_lost,
        })

    passed = (
        answers >= 100 and all(row["physical_devices_read"] for row in target_rows)
        and wrong_device == invented == lost == technical_leaks == 0
        and snapshot.get("service_calls") == 0
    )
    return {
        "schema_version": 1,
        "status": "pass" if passed else "fail",
        "read_only": True,
        "ha_service_calls": 0,
        "inventory_entity_count": inventory["entity_count"],
        "inventory_physical_device_count": inventory["physical_device_count"],
        "stage69_audit_rows": 19,
        "target_groups": len(target_rows),
        "physical_devices_read": len(audited_hashes),
        "natural_phrases": answers,
        "wrong_device": wrong_device,
        "invented_facts": invented,
        "lost_available_values": lost,
        "technical_leaks": technical_leaks,
        "model_fallbacks": model_fallbacks,
        "targets": target_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run(args.token_file)
    except Exception:
        print('{"status":"error","read_only":true,"ha_service_calls":0}', file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
