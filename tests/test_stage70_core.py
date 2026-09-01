from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bounded_ha_agent as agent
import home_assistant_mcp as resolver
import owner_chat


def graph() -> dict:
    names = [
        ("Андрей", "vacuum", "Кабинет"),
        ("Roborock S5 Max", "vacuum", "Кухня"),
        ("обхаркиватель", "humidifier", "Кабинет"),
        ("посудомойка", "switch", "Кухня"),
        ("датчик присутствия 24G", "binary_sensor", "Кабинет"),
        ("камера CW700S", "camera", "Коридор"),
        ("BASE", "sensor", "Кабинет"),
        ("ночник", "light", "Спальня"),
        ("зеркало", "light", "Ванная"),
        ("реле вентилятора", "switch", "Туалет"),
        ("свет кабинета", "light", "Кабинет"),
        ("свет кухни", "light", "Кухня"),
        ("свет коридора", "light", "Коридор"),
        ("свет ванной", "light", "Ванная"),
        ("свет туалета", "light", "Туалет"),
        ("свет гардероба", "light", "Гардероб"),
        ("главная вытяжка", "fan", "Туалет"),
        ("вытяжка на кухне", "fan", "Кухня"),
        ("Яндекс Станция Макс", "media_player", "Кабинет"),
        ("Станция Мини", "media_player", "Кухня"),
    ]
    entities, devices = [], []
    for index, (name, domain, area) in enumerate(names, 1):
        physical = f"{index:064x}"
        entity_id = f"{domain}.fixture_{index}"
        entities.append({
            "entity_id": entity_id, "domain": domain, "friendly_name": name,
            "physical_device_hash": physical, "component": "main",
            "semantic_role": "state", "semantic_attributes": {},
        })
        devices.append({
            "physical_device_hash": physical, "display_name": name,
            "aliases": [], "area_names": [area], "area_aliases": [],
            "entity_ids": [entity_id], "available_entity_count": 1,
            "unavailable_entity_count": 0,
        })
    return {
        "schema_version": 4, "observed_at": "2026-09-01T00:00:00+00:00",
        "areas": [], "entities": entities, "physical_devices": devices,
    }


def snapshot(inventory: dict) -> dict:
    return {
        "status": "healthy", "observed_at": "2026-09-01T00:00:01+00:00",
        "service_calls": 0,
        "entities": [{
            "entity_id": item["entity_id"], "state_kind": "enum",
            "state_value": "off", "source_last_updated_at": "2026-09-01T00:00:00+00:00",
        } for item in inventory["entities"]],
    }


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.inventory = graph()
        self.snapshot = snapshot(self.inventory)

    def answer(self, question: str) -> str:
        return owner_chat.answer_natural(
            question, owner_chat.startup_context(), [],
            natural_agent=lambda q, c, h, **kw: agent.respond(
                q, c, h, inventory_loader=lambda: self.inventory,
                snapshot_reader=lambda _command: (self.snapshot, 0),
                ollama_call=lambda *_a, **_k: self.fail("model fallback was not expected"),
                **kw,
            ),
        )

    def test_brush_resource_uses_vacuum_not_computer_resources(self) -> None:
        answer = self.answer("Сколько осталось ресурса основной щётки Андрея?")
        self.assertIn("Андрей", answer)
        self.assertNotIn("компьют", answer.casefold())

    def test_control_phrase_is_read_only(self) -> None:
        answer = self.answer("включи питание посудомойки")
        self.assertTrue(answer.startswith("Управление отключено"))
        self.assertIn("посудомойка", answer)

    def test_ambiguity_does_not_render_arbitrary_device(self) -> None:
        answer = self.answer("включи вытяжку")
        self.assertIn("Уточните устройство", answer)
        self.assertIn("Ничего не менял", answer)

    def test_one_hundred_natural_phrases_use_real_production_path(self) -> None:
        subjects = [
            "Андрей", "Roborock S5 Max", "обхаркиватель", "посудомойка",
            "датчик присутствия 24G", "камера CW700S", "BASE", "ночник",
            "зеркало", "реле вентилятора", "свет кабинета", "свет кухни",
            "свет коридора", "свет ванной", "свет туалета", "свет гардероба",
            "главная вытяжка", "вытяжка на кухне", "Яндекс Станция Макс",
            "Станция Мини",
        ]
        templates = [
            "Что сейчас показывает {name}?", "Проверь, пожалуйста, {name}.",
            "Какое текущее состояние у {name}?", "Есть свежие данные про {name}?",
            "Скажи состояние устройства {name} сейчас.",
        ]
        answers = [self.answer(template.format(name=name)) for name in subjects for template in templates]
        self.assertEqual(len(answers), 100)
        self.assertTrue(all("fixture_" not in answer and "http" not in answer for answer in answers))


if __name__ == "__main__":
    unittest.main()
