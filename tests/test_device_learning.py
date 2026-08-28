#!/usr/bin/env python3
"""Stage 68 deterministic device-learning contracts."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import device_learning as learning  # noqa: E402
import model_workspace  # noqa: E402


ROBOT = "a" * 64
DISHWASHER = "b" * 64


def raw_feature(
    entity_id: str, human_name: str, component: str, domain: str,
    availability: str, value: object, *, capability: str = "observe",
    semantic_role: str = "state", translation_key: str | None = None,
) -> dict:
    return {
        "entity_id": entity_id,
        "human_name": human_name,
        "component": component,
        "domain": domain,
        "availability": availability,
        "state": {
            "kind": "unavailable" if availability == "unavailable" else (
                "number" if isinstance(value, (int, float)) else "enum"
            ),
            "value": value,
        },
        "capability": capability,
        "semantic_role": semantic_role,
        "translation_key": translation_key,
        "original_name": human_name,
        "semantic_attributes": {},
    }


def robot_details() -> dict:
    return {
        "physical_device_id": ROBOT,
        "display_name": "Андрей",
        "areas": ["Кухня"],
        "manufacturers": ["ijai"],
        "models": ["ijai.vacuum.v17"],
        "physical_availability": "available",
        "features": [
            raw_feature("vacuum.andrey", "Андрей", "main", "vacuum", "available", "docked", capability="control"),
            raw_feature("sensor.andrey_status", "Статус", "status", "sensor", "available", "charging"),
            raw_feature("sensor.andrey_battery", "Заряд", "battery", "sensor", "available", 100),
            raw_feature("sensor.andrey_filter", "Фильтр", "hypa_life", "sensor", "available", 13),
            raw_feature("sensor.andrey_main_brush", "Основная щётка", "main_brush_life", "sensor", "available", 56),
            raw_feature("sensor.andrey_mop", "Швабра", "mop_life", "sensor", "available", 72),
            raw_feature("sensor.andrey_side_brush", "Боковая щётка", "side_brush_life", "sensor", "available", 13),
            raw_feature("button.andrey_mop", "Начало мойки", "start_mop", "button", "unavailable", None, capability="press"),
            raw_feature("button.andrey_sweep_mop", "Начало уборки со шваброй", "start_sweep_mop", "button", "unavailable", None, capability="press"),
        ],
    }


def dishwasher_details() -> dict:
    return {
        "physical_device_id": DISHWASHER,
        "display_name": "Dishwasher",
        "areas": [],
        "manufacturers": ["Midea"],
        "models": ["Dishwasher 760EY174 (0)"],
        "physical_availability": "available",
        "features": [
            raw_feature("switch.dw_power", "Питание", "power", "switch", "available", "off", capability="control"),
            raw_feature("sensor.dw_status", "Статус", "status", "sensor", "available", "Power Off"),
            raw_feature("sensor.dw_progress", "Прогресс", "progress", "sensor", "available", "Idle"),
            raw_feature("sensor.dw_time", "Оставшееся время", "time_remaining", "sensor", "available", 215),
            raw_feature("binary_sensor.dw_door", "Дверь", "Door", "binary_sensor", "available", "on"),
            raw_feature("sensor.dw_error", "Код ошибки", "error_code", "sensor", "available", 0),
            raw_feature("binary_sensor.dw_rinse", "Нехватка ополаскивателя", "rinse_aid", "binary_sensor", "available", "off"),
            raw_feature("binary_sensor.dw_salt", "Соль", "salt", "binary_sensor", "available", "off"),
            raw_feature("select.dw_mode", "Режим", "mode", "select", "unavailable", None, capability="set_value"),
            raw_feature("switch.dw_extra_dry", "Экстра сушка", "extra_dry", "switch", "unavailable", None, capability="control"),
        ],
    }


def inventory() -> dict:
    return {
        "physical_devices": [
            {"physical_device_hash": ROBOT, "config_domains": ["xiaomi_miot"]},
            {"physical_device_hash": DISHWASHER, "config_domains": ["midea_ac_lan"]},
        ]
    }


class DeviceLearningTests(unittest.TestCase):
    def test_andrey_profile_has_exact_grounded_roles_and_partial_failure(self) -> None:
        profile = learning.build_profile(robot_details(), inventory())
        by_component = {item["component"]: item for item in profile["features"]}
        self.assertEqual(profile["device_type"], "robot_vacuum")
        self.assertEqual(by_component["battery"]["current_state"]["value"], 100)
        self.assertEqual(by_component["filter"]["current_state"]["value"], 13)
        self.assertEqual(by_component["main_brush"]["current_state"]["value"], 56)
        self.assertEqual(by_component["mop"]["current_state"]["value"], 72)
        self.assertEqual(by_component["side_brush"]["current_state"]["value"], 13)
        unavailable = [item for item in profile["features"] if item["availability"] == "unavailable"]
        self.assertEqual(len(unavailable), 2)
        self.assertTrue(all("unknown_cause" in item["availability_policy"] for item in unavailable))
        self.assertFalse(profile["learning_policy"]["facts_from_model_answers"])

    def test_dishwasher_power_off_makes_controls_conditional_not_failed(self) -> None:
        profile = learning.build_profile(dishwasher_details(), inventory())
        conditional = {
            item["component"]: item for item in profile["features"]
            if item["availability_policy"] == "conditional"
        }
        self.assertEqual(set(conditional), {"mode", "extra_dry"})
        self.assertTrue(all(item["conditional_on"] == "power:on" for item in conditional.values()))
        power_capability = next(item for item in profile["capabilities"] if item["component"] == "power")
        self.assertIn("readback", power_capability["verification_rule"])

    def test_each_device_gets_fifty_positive_and_twenty_five_negative(self) -> None:
        for details in (robot_details(), dishwasher_details()):
            profile = learning.build_profile(details, inventory())
            generated = learning.generate_examples(profile, "c" * 64)
            accepted, rejected = learning.validate_corpus(generated, profile)
            self.assertEqual(len(generated), 75)
            self.assertEqual(len(accepted), 75, rejected)
            self.assertEqual(sum(item["polarity"] == "positive" for item in accepted), 50)
            self.assertEqual(sum(item["polarity"] == "negative" for item in accepted), 25)
            self.assertEqual(set(item["category"] for item in accepted), set(learning.CATEGORIES))

    def test_validator_rejects_hallucinations_and_private_identifiers(self) -> None:
        profile = learning.build_profile(robot_details(), inventory())
        adversarial = learning.deliberately_rejected_examples(profile, "d" * 64)
        accepted, rejected = learning.validate_corpus(adversarial, profile)
        self.assertEqual(accepted, [])
        reasons = {reason for item in rejected for reason in item["rejection_reasons"]}
        self.assertIn("invented_cause", reasons)
        self.assertIn("entity_id_exposed", reasons)
        self.assertIn("accepted_called_verified", reasons)
        self.assertIn("number_not_in_source_facts", reasons)

    def test_persistence_stays_inside_bounded_workspace(self) -> None:
        profile = learning.build_profile(robot_details(), inventory())
        generated = learning.generate_examples(profile, "e" * 64)
        accepted, rejected = learning.validate_corpus(generated, profile)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir(mode=0o700)
            root.chmod(0o700)
            paths = learning.persist_learning(profile, generated, accepted, rejected, root=root)
            self.assertEqual(json.loads(model_workspace.read_text(paths["profile"], root)["content"])["display_name"], "Андрей")
            self.assertEqual(
                len((root / paths["validated"]).read_text(encoding="utf-8").splitlines()),
                75,
            )
            self.assertFalse(model_workspace.status(root)["active_project_instructions_writable"])

    def test_voice_retrieval_uses_prepared_profile_and_at_most_eight_features(self) -> None:
        details = robot_details()
        profile = learning.build_profile(details, inventory())
        compact = learning.compact_profile(
            profile, details, "А сколько у Андрея батареи?"
        )
        self.assertGreaterEqual(compact["relevant_feature_count"], 3)
        self.assertLessEqual(compact["relevant_feature_count"], 8)
        self.assertEqual(compact["relevant_features"][0]["component"], "battery")
        self.assertNotIn("entity_id", json.dumps(compact, ensure_ascii=False))
        self.assertNotIn("capabilities", compact)
        self.assertEqual(
            learning.render_compact_observation(
                compact, "Сколько у Андрея батареи?"
            ),
            "Андрей: заряд — 100%.",
        )

    def test_compact_fallback_keeps_partial_failure_and_unknown_cause(self) -> None:
        details = robot_details()
        profile = learning.build_profile(details, inventory())
        compact = learning.compact_profile(
            profile, details, "Почему функция недоступна?"
        )
        answer = learning.render_compact_observation(
            compact, "Почему функция недоступна?"
        )
        self.assertIn("Само устройство «Андрей» доступно", answer)
        self.assertIn("Недоступны 2 отдельные функции", answer)
        self.assertIn("причина по текущим данным не подтверждена", answer)
        self.assertIn(
            "unknown_cause_not_disclosed",
            learning.validate_compact_answer(
                compact,
                "Почему функция недоступна?",
                "Само устройство доступно. Недоступны две отдельные функции.",
            ),
        )

    def test_compact_answer_validator_blocks_state_and_value_distortions(self) -> None:
        details = robot_details()
        profile = learning.build_profile(details, inventory())
        general = learning.compact_profile(profile, details, "Что с Андреем?", maximum=3)
        self.assertEqual(
            learning.validate_compact_answer(
                general,
                "Что с Андреем?",
                "Андрей находится на док-станции и заряжается. Заряд 100%.",
            ),
            [],
        )
        self.assertIn(
            "charging_state_distorted",
            learning.validate_compact_answer(
                general, "Что с Андреем?", "Андрей едет и убирает."
            ),
        )
        battery = learning.compact_profile(
            profile, details, "Сколько у Андрея батареи?", maximum=3
        )
        self.assertIn(
            "requested_value_omitted",
            learning.validate_compact_answer(
                battery, "Сколько у Андрея батареи?", "Заряд высокий."
            ),
        )
        maintenance = learning.compact_profile(
            profile, details, "Что с фильтром?", maximum=3
        )
        maintenance_reasons = learning.validate_compact_answer(
            maintenance,
            "Что с фильтром?",
            "Фильтр на 13% заряжен, поэтому фильтрация воздуха приостановлена.",
        )
        self.assertIn("maintenance_semantics_distorted", maintenance_reasons)
        self.assertIn("invented_device_process", maintenance_reasons)
        self.assertEqual(
            learning.render_compact_observation(maintenance, "Что с фильтром?"),
            "Андрей: ресурс фильтра — 13%.",
        )
        self.assertEqual(
            learning.validate_compact_answer(
                maintenance,
                "Что с фильтром?",
                "У фильтра осталось 13% ресурса.",
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
