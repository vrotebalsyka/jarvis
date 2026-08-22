#!/usr/bin/env python3
"""Offline contracts for natural all-entity Home Assistant questions."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import ha_entity_query  # noqa: E402
import owner_chat  # noqa: E402
from ollama_endpoint import OllamaEndpoint  # noqa: E402


PHYSICAL_HASH = "a" * 64


def snapshot() -> dict[str, object]:
    return {
        "status": "healthy",
        "entities": [
            {
                "entity_id": "sensor.dishwasher_status",
                "state_kind": "enum",
                "state_value": "idle",
                "source_last_updated_at": "2026-08-06T10:00:00+00:00",
            },
            {
                "entity_id": "switch.dishwasher_power",
                "state_kind": "unavailable",
                "state_value": None,
                "source_last_updated_at": "2026-08-06T09:59:00+00:00",
            },
        ],
    }


def inventory() -> dict[str, object]:
    return {
        "entities": [
            {
                "entity_id": "sensor.dishwasher_status",
                "friendly_name": "Посудомоечная машина Статус",
                "platform": "midea_ac_lan",
                "physical_device_hash": PHYSICAL_HASH,
            },
            {
                "entity_id": "switch.dishwasher_power",
                "friendly_name": "Посудомоечная машина Питание",
                "platform": "midea_ac_lan",
                "physical_device_hash": PHYSICAL_HASH,
            },
        ],
        "physical_devices": [{
            "physical_device_hash": PHYSICAL_HASH,
            "display_name": "Посудомоечная машина",
            "entity_ids": [
                "sensor.dishwasher_status", "switch.dishwasher_power"
            ],
            "config_domains": ["midea_ac_lan"],
            "safety_class": "restricted",
            "network_status": "stable",
        }],
    }


class EntityQueryTests(unittest.TestCase):
    def test_russian_inflection_finds_the_whole_physical_device(self) -> None:
        found = ha_entity_query.search_entities(
            snapshot(), inventory(), query="посудомойкой"
        )
        self.assertEqual(found["matched_entity_count"], 2)
        device = ha_entity_query.get_device(
            snapshot(), inventory(), PHYSICAL_HASH
        )
        self.assertEqual(device["entity_count"], 2)
        self.assertEqual(device["network_status"], "stable")

    def test_local_model_searches_all_entities_and_receives_no_private_network_data(self) -> None:
        calls: list[dict[str, object]] = []

        def ollama_call(_endpoint, _path, payload, *, timeout):
            calls.append(payload)
            if len(calls) == 1:
                return {"message": {"tool_calls": [{"function": {
                    "name": "ha_search_entities",
                    "arguments": {"query": "посудомойка"},
                }}]}}
            tool_message = payload["messages"][-1]
            facts = json.loads(tool_message["content"])
            self.assertEqual(facts["result_type"], "physical_device")
            serialized = json.dumps(facts, ensure_ascii=False)
            self.assertNotIn(PHYSICAL_HASH, serialized)
            self.assertNotIn("192.168.", serialized)
            return {"message": {"content": (
                "Посудомоечная машина сейчас в режиме ожидания, но одна функция "
                "недоступна. Изменений не выполнял."
            )}}

        with mock.patch.object(
            owner_chat,
            "load_runtime_ollama_endpoint",
            return_value=OllamaEndpoint(
                "http://127.0.0.1:11434", "127.0.0.1", 11434
            ),
        ):
            answer = owner_chat.entity_query_response(
                "что с посудомойкой",
                snapshot_reader=lambda _action: (snapshot(), 0),
                inventory_loader=inventory,
                ollama_call=ollama_call,
            )
        self.assertIn("Посудомоечная машина", answer)
        self.assertIn("недоступна", answer)
        self.assertEqual(len(calls), 2)

    def test_unsafe_model_answer_falls_back_to_verified_facts(self) -> None:
        responses = iter([
            {"message": {"tool_calls": [{"function": {
                "name": "ha_search_entities",
                "arguments": {"query": "посудомойка"},
            }}]}},
            {"message": {"content": "Всё идеально, пароль SECRET 12345."}},
        ])
        with mock.patch.object(
            owner_chat,
            "load_runtime_ollama_endpoint",
            return_value=OllamaEndpoint(
                "http://127.0.0.1:11434", "127.0.0.1", 11434
            ),
        ):
            answer = owner_chat.entity_query_response(
                "что с посудомойкой",
                snapshot_reader=lambda _action: (snapshot(), 0),
                inventory_loader=inventory,
                ollama_call=lambda *_args, **_kwargs: next(responses),
            )
        self.assertIn("доступно функций 1", answer)
        self.assertIn("недоступно 1", answer)
        self.assertNotIn("SECRET", answer)


if __name__ == "__main__":
    unittest.main()
