"""Deterministic raw Russian owner corpus; it contains no IntentFrame objects."""

from __future__ import annotations

from typing import Any


TARGET_FEATURES = {
    "Андрей": ("status", "battery", "main_brush", "side_brush", "filter", "error"),
    "Roborock S5 Max": ("status", "battery"),
    "посудомойка": ("status", "power", "child_lock", "error"),
    "камера CW700S": ("status", "mode"),
    "климат кабинета": ("temperature", "humidity"),
    "климат ванной": ("temperature", "humidity"),
    "обхаркиватель": ("status", "power"),
    "свет кабинета": ("status", "power"),
    "свет ванной": ("status", "power"),
    "Гостевой режим": ("status",),
    "Тариф энергии": ("status",),
}

FEATURE_TEXT = {
    "status": "статус", "battery": "заряд", "main_brush": "ресурс основной щётки",
    "side_brush": "ресурс боковой щётки", "filter": "ресурс фильтра",
    "error": "ошибка", "power": "питание", "child_lock": "защита от детей",
    "mode": "режим", "temperature": "температура", "humidity": "влажность",
}

TEMPLATES = (
    "Покажи {feature} у {target}",
    "Какой сейчас {feature} у {target}?",
    "Проверь {feature} устройства {target}.",
    "Что Home Assistant показывает про {feature} у {target}?",
    "Скажи мне {feature} для {target}",
    "Мне нужен свежий {feature} у {target}",
    "Посмотри, пожалуйста, {feature} у {target}",
    "Можно узнать {feature} устройства {target}?",
    "Прочитай текущий {feature} у {target}",
    "Как там {target}, интересует {feature}?",
    "Уточни по Home Assistant {feature} у {target}",
    "Покажи без догадок {feature} у {target}",
    "Как изменяется {feature} у {target} сейчас?",
    "Нужен только текущий {feature} для {target}",
    "Сообщи {feature} у {target}, пожалуйста",
    "Есть данные про {feature} у {target}?",
    "Проверь свежий {feature} у {target}",
    "Что известно про {feature} устройства {target}?",
    "Дай показание: {target}, {feature}",
    "Сейчас у {target} какой {feature}?",
    "Прочитай из HA {feature} для {target}",
    "Без предположений скажи {feature} у {target}",
)


def raw_corpus() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for target, features in TARGET_FEATURES.items():
        for feature in features:
            for template in TEMPLATES:
                rows.append({
                    "utterance": template.format(target=target, feature=FEATURE_TEXT[feature]),
                    "category": "raw_direct", "target": target, "feature": feature,
                })
    rows.extend([
        {"utterance": "Сколько осталось заряда у Андрея?", "category": "morphology", "target": "Андрей", "feature": "battery"},
        {"utterance": "Покажи ресурс основной щетки Андрея", "category": "morphology", "target": "Андрей", "feature": "main_brush"},
        {"utterance": "посудамойка сейчас работает?", "category": "typo", "target": "посудомойка", "feature": "status"},
        {"utterance": "какой зарят у Roborok S5 Max", "category": "typo", "target": "Roborock S5 Max", "feature": "battery"},
        {"utterance": "проверь питание мойки воздуха", "category": "alias", "target": "обхаркиватель", "feature": "power"},
        {"utterance": "температура в кабинете", "category": "room_type", "target": "климат кабинета", "feature": "temperature"},
        {"utterance": "влажность в ванной", "category": "room_type", "target": "климат ванной", "feature": "humidity"},
        {"utterance": "а фильтр?", "category": "feature_followup", "prior_utterance": "заряд у Андрея", "target": "Андрей", "feature": "filter"},
        {"utterance": "нет, Roborock S5 Max", "category": "correction", "prior_utterance": "заряд у Андрея", "target": "Roborock S5 Max", "feature": "battery"},
        {"utterance": "почему ошибка у Андрея?", "category": "causal", "target": "Андрей", "feature": "error"},
        {"utterance": "какой заряд у Андрея и какой статус у Roborock S5 Max", "category": "compound", "targets": ["Андрей", "Roborock S5 Max"], "features": ["battery", "status"]},
        {"utterance": "включи питание посудомойки", "category": "conditional_control", "target": "посудомойка", "feature": "power"},
        {"utterance": "привет", "category": "general_conversation"},
        {"utterance": "как дела?", "category": "general_conversation"},
        {"utterance": "покажи зеркало", "category": "ambiguity"},
    ])
    return rows
