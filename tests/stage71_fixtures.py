"""Stage 71 metadata and fresh-state fixtures; IDs never enter owner output."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import home_assistant_inventory as inventory


AREAS = [
    {"area_id": "area-office", "name": "Кабинет", "aliases": ["Рабочая"]},
    {"area_id": "area-bath", "name": "Ванная", "aliases": ["Ванная комната"]},
    {"area_id": "area-kitchen", "name": "Кухня", "aliases": []},
    {"area_id": "area-hall", "name": "Коридор", "aliases": ["Прихожая"]},
]

DEVICES = [
    {"id": "dev-andrew", "name_by_user": "Андрей", "name": "Робот Андрей", "original_name": "Vacuum A", "manufacturer": "Roborock", "model": "S7", "area_id": "area-office", "config_entries": ["cfg-roborock"]},
    {"id": "dev-roborock", "name": "Roborock S5 Max", "manufacturer": "Roborock", "model": "S5 Max", "area_id": "area-kitchen", "config_entries": ["cfg-roborock-alt"]},
    {"id": "dev-dishwasher", "name_by_user": "посудомойка", "name": "Dishwasher", "manufacturer": "Midea", "model": "DW1", "area_id": "area-kitchen", "config_entries": ["cfg-dish"]},
    {"id": "dev-camera", "name": "камера CW700S", "manufacturer": "Xiaomi", "model": "CW700S", "area_id": "area-hall", "config_entries": ["cfg-camera"]},
    {"id": "dev-office-climate", "name": "климат кабинета", "manufacturer": "Aqara", "model": "TH", "area_id": "area-office", "config_entries": ["cfg-zigbee"]},
    {"id": "dev-bath-climate", "name": "климат ванной", "manufacturer": "Aqara", "model": "TH", "area_id": "area-bath", "config_entries": ["cfg-zigbee"]},
    {"id": "dev-mirror-a", "name": "зеркало", "manufacturer": "Tuya", "model": "Mirror", "area_id": "area-bath", "config_entries": ["cfg-tuya-a"]},
    {"id": "dev-mirror-b", "name": "зеркало", "manufacturer": "Tuya", "model": "Mirror", "area_id": "area-bath", "config_entries": ["cfg-tuya-b"]},
    {"id": "dev-humidifier", "name_by_user": "обхаркиватель", "name": "Humidifier", "manufacturer": "Smartmi", "model": "H1", "area_id": "area-office", "config_entries": ["cfg-miot"]},
    {"id": "dev-office-light", "name": "свет кабинета", "manufacturer": "Tuya", "model": "Relay", "area_id": "area-office", "config_entries": ["cfg-tuya-light"]},
    {"id": "dev-bath-light", "name": "свет ванной", "manufacturer": "Tuya", "model": "Relay", "area_id": "area-bath", "config_entries": ["cfg-tuya-light"]},
    {"id": "dev-computer", "name": "Компьютер", "manufacturer": "Generic", "model": "PC", "area_id": "area-office", "config_entries": ["cfg-system"]},
]


def entity(
    record_id: str,
    entity_id: str,
    name: str,
    *,
    device: str | None,
    area: str | None = None,
    platform: str = "fixture",
    config: str = "cfg-fixture",
    aliases: list[str] | None = None,
    translation_key: str | None = None,
    category: str | None = None,
    disabled: bool = False,
    hidden: bool = False,
) -> dict[str, Any]:
    return {
        "id": record_id, "entity_id": entity_id, "name": None,
        "original_name": name, "aliases": aliases or [], "device_id": device,
        "area_id": area, "platform": platform, "config_entry_id": config,
        "translation_key": translation_key, "entity_category": category,
        "disabled_by": "user" if disabled else None,
        "hidden_by": "user" if hidden else None,
    }


ENTITIES = [
    entity("e-andrew-main", "vacuum.andrew", "Андрей", device="dev-andrew", aliases=["Андрюша"]),
    entity("e-andrew-battery", "sensor.andrew_battery", "Андрей Батарея", device="dev-andrew", translation_key="battery", category="diagnostic"),
    entity("e-andrew-main-brush", "sensor.andrew_main_brush", "Андрей Основная щётка", device="dev-andrew", translation_key="main_brush", category="diagnostic"),
    entity("e-andrew-side-brush", "sensor.andrew_side_brush", "Андрей Боковая щётка", device="dev-andrew", translation_key="side_brush", category="diagnostic"),
    entity("e-andrew-filter", "sensor.andrew_filter", "Андрей Фильтр", device="dev-andrew", translation_key="filter", category="diagnostic"),
    entity("e-andrew-error", "sensor.andrew_error", "Андрей Ошибка", device="dev-andrew", translation_key="error", category="diagnostic"),
    entity("e-roborock-main", "vacuum.roborock", "Roborock S5 Max", device="dev-roborock"),
    entity("e-roborock-battery", "sensor.roborock_battery", "Roborock S5 Max Батарея", device="dev-roborock", translation_key="battery"),
    entity("e-dish-power", "switch.dishwasher_power", "посудомойка Питание", device="dev-dishwasher", translation_key="power"),
    entity("e-dish-power-alt", "switch.dishwasher_power_alt", "посудомойка Питание alternate", device="dev-dishwasher", platform="alternate", config="cfg-dish-alt", translation_key="power"),
    entity("e-dish-status", "sensor.dishwasher_status", "посудомойка Статус", device="dev-dishwasher", translation_key="status"),
    entity("e-dish-lock", "lock.dishwasher_child_lock", "посудомойка Блокировка от детей", device="dev-dishwasher", translation_key="child_lock"),
    entity("e-dish-rinse", "binary_sensor.dishwasher_rinse_aid", "посудомойка Нехватка ополаскивателя", device="dev-dishwasher", translation_key="rinse_aid", category="diagnostic"),
    entity("e-camera-mode", "select.camera_recording_mode", "камера CW700S Режим записи", device="dev-camera", translation_key="recording_mode"),
    entity("e-camera-interval", "number.camera_alarm_interval", "камера CW700S Интервал", device="dev-camera", translation_key="alarm_interval"),
    entity("e-camera-main", "camera.camera_main", "камера CW700S", device="dev-camera"),
    entity("e-office-temp", "sensor.office_temperature", "Температура кабинета", device="dev-office-climate", translation_key="temperature"),
    entity("e-office-humidity", "sensor.office_humidity", "Влажность кабинета", device="dev-office-climate", translation_key="humidity"),
    entity("e-bath-temp", "sensor.bath_temperature", "Температура ванной", device="dev-bath-climate", translation_key="temperature"),
    entity("e-bath-humidity", "sensor.bath_humidity", "Влажность ванной", device="dev-bath-climate", translation_key="humidity"),
    entity("e-mirror-a", "light.mirror_a", "зеркало", device="dev-mirror-a"),
    entity("e-mirror-b", "light.mirror_b", "зеркало", device="dev-mirror-b"),
    entity("e-humidifier", "humidifier.office", "обхаркиватель", device="dev-humidifier", aliases=["мойка воздуха"]),
    entity("e-office-light", "light.office", "свет кабинета", device="dev-office-light"),
    entity("e-bath-light", "light.bath", "свет ванной", device="dev-bath-light"),
    entity("e-computer-resource", "sensor.computer_resource", "Компьютер Ресурс", device="dev-computer", translation_key="resource"),
    entity("e-logical-guest", "input_boolean.guest_mode", "Гостевой режим", device=None, area="area-hall", aliases=["режим гостей"]),
    entity("e-logical-tariff", "sensor.power_tariff", "Тариф энергии", device=None, area="area-office"),
    entity("e-disabled", "sensor.disabled_fixture", "Отключённый датчик", device="dev-office-climate", disabled=True),
    entity("e-hidden", "sensor.hidden_fixture", "Скрытый датчик", device="dev-office-climate", hidden=True),
]


VALUES: dict[str, tuple[str, Any, dict[str, Any]]] = {
    "vacuum.andrew": ("enum", "docked", {}),
    "sensor.andrew_battery": ("number", 73.0, {"device_class": "battery", "unit_of_measurement": "%", "state_class": "measurement"}),
    "sensor.andrew_main_brush": ("number", 84.0, {"unit_of_measurement": "%"}),
    "sensor.andrew_side_brush": ("number", 65.0, {"unit_of_measurement": "%"}),
    "sensor.andrew_filter": ("number", 42.0, {"unit_of_measurement": "%"}),
    "sensor.andrew_error": ("unknown", None, {}),
    "vacuum.roborock": ("enum", "cleaning", {}),
    "sensor.roborock_battery": ("number", 100.0, {"device_class": "battery", "unit_of_measurement": "%"}),
    "switch.dishwasher_power": ("enum", "off", {"supported_features": 0}),
    "switch.dishwasher_power_alt": ("unavailable", None, {"supported_features": 0}),
    "sensor.dishwasher_status": ("enum", "idle", {}),
    "lock.dishwasher_child_lock": ("enum", "off", {}),
    "binary_sensor.dishwasher_rinse_aid": ("enum", "on", {"device_class": "problem"}),
    "select.camera_recording_mode": ("enum", "continuous", {"options": ["continuous", "motion"], "supported_features": 0}),
    "number.camera_alarm_interval": ("number", 30.0, {"min": 5, "max": 120, "step": 5}),
    "camera.camera_main": ("enum", "idle", {"supported_features": 3}),
    "sensor.office_temperature": ("number", 22.4, {"device_class": "temperature", "state_class": "measurement", "unit_of_measurement": "°C"}),
    "sensor.office_humidity": ("number", 41.0, {"device_class": "humidity", "state_class": "measurement", "unit_of_measurement": "%"}),
    "sensor.bath_temperature": ("number", 24.1, {"device_class": "temperature", "state_class": "measurement", "unit_of_measurement": "°C"}),
    "sensor.bath_humidity": ("number", 58.0, {"device_class": "humidity", "state_class": "measurement", "unit_of_measurement": "%"}),
    "light.mirror_a": ("enum", "off", {"supported_features": 1}),
    "light.mirror_b": ("unavailable", None, {"supported_features": 1}),
    "humidifier.office": ("enum", "on", {"supported_features": 1}),
    "light.office": ("enum", "on", {"supported_features": 1}),
    "light.bath": ("enum", "off", {"supported_features": 1}),
    "sensor.computer_resource": ("number", 81.0, {"unit_of_measurement": "%"}),
    "input_boolean.guest_mode": ("enum", "off", {}),
    "sensor.power_tariff": ("enum", "day", {"options": ["day", "night"]}),
    "conversation.fixture": ("enum", "idle", {"friendly_name": "Диалоговый агент"}),
}


def raw_states(values: dict[str, tuple[str, Any, dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
    source = VALUES if values is None else values
    registry_names = {item["entity_id"]: item["original_name"] for item in ENTITIES}
    result: list[dict[str, Any]] = []
    for entity_id, (kind, value, attributes) in source.items():
        raw_value = {
            "unknown": "unknown", "unavailable": "unavailable", "redacted": "secret"
        }.get(kind, str(value).lower() if isinstance(value, bool) else str(value))
        result.append({
            "entity_id": entity_id, "state": raw_value,
            "attributes": {"friendly_name": registry_names.get(entity_id, entity_id.split(".", 1)[1].replace("_", " ")), **attributes},
            "last_updated": "2026-09-02T10:00:00+00:00",
        })
    return result


def graph() -> dict[str, Any]:
    return inventory.build_inventory(ENTITIES, DEVICES, AREAS, raw_states())


def snapshot(overrides: dict[str, tuple[str, Any]] | None = None) -> dict[str, Any]:
    updates = overrides or {}
    entities: list[dict[str, Any]] = []
    for entity_id, (kind, value, _attributes) in VALUES.items():
        state_kind, state_value = updates.get(entity_id, (kind, value))
        entities.append({
            "entity_id": entity_id, "state_kind": state_kind, "state_value": state_value,
            "observed_at": "2026-09-02T10:00:01+00:00",
            "source_last_updated_at": "2026-09-02T10:00:00+00:00",
        })
    return {
        "schema_version": 1, "observed_at": "2026-09-02T10:00:01+00:00",
        "status": "healthy", "service_calls": 0, "entities": entities,
    }


def target_ref(document: dict[str, Any], display_name: str, *, occurrence: int = 0) -> str:
    matches = [
        node["target_ref"] for node in document["physical_nodes"] + document["logical_nodes"]
        if node["display_name"].casefold() == display_name.casefold()
    ]
    return matches[occurrence]
