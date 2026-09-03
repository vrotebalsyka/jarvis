"""Stage 72 metadata fixtures. Technical identifiers never enter model prompts."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]

import home_assistant_inventory as inventory
import stage71_fixtures as base


AREAS = [
    *base.AREAS,
    {"area_id": "area-toilet", "name": "Туалет", "aliases": ["Санузел"]},
    {"area_id": "area-entry", "name": "Входная зона", "aliases": ["Тамбур"]},
]

DEVICES = [
    *base.DEVICES,
    {"id": "d72-bath-main", "name": "Основной свет ванной", "area_id": "area-bath", "manufacturer": "Fixture", "model": "Light", "config_entries": ["c72-light"]},
    {"id": "d72-bath-mirror", "name": "Зеркало ванной", "area_id": "area-bath", "manufacturer": "Fixture", "model": "Mirror", "config_entries": ["c72-light"]},
    {"id": "d72-office-main", "name": "Основной свет кабинета", "area_id": "area-office", "manufacturer": "Fixture", "model": "Light", "config_entries": ["c72-light"]},
    {"id": "d72-office-desk", "name": "Настольная лампа кабинета", "area_id": "area-office", "manufacturer": "Fixture", "model": "Lamp", "config_entries": ["c72-light"]},
    {"id": "d72-toilet-main", "name": "Основной свет туалета", "area_id": "area-toilet", "manufacturer": "Fixture", "model": "Light", "config_entries": ["c72-light"]},
    {"id": "d72-entry-main", "name": "Основной свет входной зоны", "area_id": "area-entry", "manufacturer": "Fixture", "model": "Light", "config_entries": ["c72-light"]},
    {"id": "d72-kitchen-main", "name": "Основной свет кухни", "area_id": "area-kitchen", "manufacturer": "Fixture", "model": "Light", "config_entries": ["c72-light"]},
    {"id": "d72-hall-night", "name": "Ночник коридора", "area_id": "area-hall", "manufacturer": "Fixture", "model": "Night", "config_entries": ["c72-light"]},
    {"id": "d72-fan-relay", "name": "Реле вентилятора", "area_id": "area-bath", "manufacturer": "Fixture", "model": "Relay", "config_entries": ["c72-switch"]},
    {"id": "d72-desk-relay", "name": "Реле стола", "area_id": "area-office", "manufacturer": "Fixture", "model": "Relay", "config_entries": ["c72-switch"]},
    {"id": "d72-hood", "name": "Вытяжка", "area_id": "area-kitchen", "manufacturer": "Fixture", "model": "Fan", "config_entries": ["c72-fan"]},
    {"id": "d72-button", "name": "Кнопка звонка", "area_id": "area-entry", "manufacturer": "Fixture", "model": "Button", "config_entries": ["c72-button"]},
    {"id": "d72-lock", "name": "Замок входной двери", "area_id": "area-entry", "manufacturer": "Fixture", "model": "Lock", "config_entries": ["c72-lock"]},
    {"id": "d72-climate", "name": "Термостат кухни", "area_id": "area-kitchen", "manufacturer": "Fixture", "model": "Climate", "config_entries": ["c72-climate"]},
]


def e(record: str, entity_id: str, name: str, device: str | None, area: str | None = None) -> dict[str, Any]:
    return base.entity(record, entity_id, name, device=device, area=area, platform="stage72", config="c72")


ENTITIES = [
    *base.ENTITIES,
    e("e72-bath-main", "light.stage72_bath_main", "Основной свет ванной", "d72-bath-main"),
    e("e72-bath-mirror", "light.stage72_bath_mirror", "Зеркало ванной", "d72-bath-mirror"),
    e("e72-office-main", "light.stage72_office_main", "Основной свет кабинета", "d72-office-main"),
    e("e72-office-desk", "light.stage72_office_desk", "Настольная лампа кабинета", "d72-office-desk"),
    e("e72-toilet-main", "light.stage72_toilet_main", "Основной свет туалета", "d72-toilet-main"),
    e("e72-entry-main", "light.stage72_entry_main", "Основной свет входной зоны", "d72-entry-main"),
    e("e72-kitchen-main", "light.stage72_kitchen_main", "Основной свет кухни", "d72-kitchen-main"),
    e("e72-hall-night", "light.stage72_hall_night", "Ночник коридора", "d72-hall-night"),
    e("e72-fan-relay", "switch.stage72_fan_relay", "Реле вентилятора", "d72-fan-relay"),
    e("e72-desk-relay", "switch.stage72_desk_relay", "Реле стола", "d72-desk-relay"),
    e("e72-hood", "fan.stage72_hood", "Вытяжка", "d72-hood"),
    e("e72-button", "button.stage72_doorbell", "Кнопка звонка", "d72-button"),
    e("e72-lock", "lock.stage72_entry", "Замок входной двери", "d72-lock"),
    e("e72-climate", "climate.stage72_kitchen", "Термостат кухни", "d72-climate"),
    e("e72-script", "script.stage72_evening", "Вечерний сценарий", None, "area-office"),
]

VALUES = {
    **base.VALUES,
    **{
        entity["entity_id"]: ("enum", "off", {"supported_features": 0})
        for entity in ENTITIES[len(base.ENTITIES):]
    },
}


def graph() -> dict[str, Any]:
    return inventory.build_inventory(ENTITIES, DEVICES, AREAS, base.raw_states(VALUES))


def target_ref(document: dict[str, Any], display_name: str) -> str:
    matches = [
        node["target_ref"] for node in document["physical_nodes"] + document["logical_nodes"]
        if node["display_name"].casefold() == display_name.casefold()
    ]
    if len(matches) != 1:
        raise AssertionError(f"fixture target is not unique: {display_name}")
    return matches[0]
