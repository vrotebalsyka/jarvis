#!/usr/bin/env python3
"""The single host-only HomeGraph resolver; no MCP transport or model IDs."""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import home_assistant_inventory as inventory_builder  # noqa: E402
import home_assistant_read as adapter  # noqa: E402


MAX_INVENTORY_BYTES = inventory_builder.MAX_INVENTORY_BYTES
FEATURES = frozenset({
    "power", "status", "battery", "filter", "main_brush", "side_brush",
    "humidity", "temperature", "child_lock", "mode", "error",
    "consumables", "unknown",
})
TYPE_CONCEPTS: dict[str, frozenset[str]] = {
    "dishwasher": frozenset({"dishwasher", "посудомойка", "посудомоечная"}),
    "appliance": frozenset({"appliance", "техника", "посудомойка", "стиральная", "сушилка"}),
    "light": frozenset({"light", "свет", "освещение", "лампа", "светильник", "ночник", "зеркало"}),
    "vacuum": frozenset({"vacuum", "robot", "робот", "пылесос"}),
    "switch": frozenset({"switch", "реле", "выключатель", "розетка"}),
    "button": frozenset({"button", "кнопка"}),
    "lock": frozenset({"lock", "замок", "блокировка"}),
    "climate": frozenset({"climate", "климат", "термостат", "кондиционер"}),
    "script": frozenset({"script", "сценарий", "скрипт"}),
    "media_player": frozenset({"media player", "колонка", "станция"}),
    "camera": frozenset({"camera", "камера"}),
    "sensor": frozenset({"sensor", "датчик"}),
    "binary_sensor": frozenset({"binary sensor", "датчик"}),
    "fan": frozenset({"fan", "вентилятор", "вытяжка"}),
    "humidifier": frozenset({"humidifier", "увлажнитель", "мойка воздуха"}),
}
FEATURE_TERMS: dict[str, tuple[str, ...]] = {
    "main_brush": ("основная щетка", "основной щетки", "основной щётки", "main brush"),
    "side_brush": ("боковая щетка", "боковой щетки", "боковой щётки", "side brush"),
    "child_lock": ("детский замок", "защита от детей", "блокировка от детей", "child lock"),
    "battery": ("батарея", "батареи", "заряд", "заряда", "аккумулятор"),
    "filter": ("фильтр", "фильтра"),
    "humidity": ("влажность", "влажности"),
    "temperature": ("температура", "температуры", "градусов"),
    "power": ("питание", "включен", "включена", "включено", "выключен", "выключена"),
    "mode": ("режим", "режима", "mode"),
    "error": ("ошибка", "ошибки", "неисправность", "проблема"),
    "consumables": ("расходники", "расходников", "ресурс щеток", "ресурс щёток"),
    "status": ("статус", "состояние", "работает", "что с"),
    "unknown": ("неизвестный параметр", "неизвестный показатель", "unknown feature"),
}
FEATURE_PUBLIC_LABELS = {
    "power": "питание", "status": "состояние", "battery": "заряд",
    "filter": "ресурс фильтра", "main_brush": "ресурс основной щётки",
    "side_brush": "ресурс боковой щётки", "humidity": "влажность",
    "temperature": "температура", "child_lock": "защита от детей",
    "mode": "режим", "error": "ошибка", "consumables": "расходники",
    "unknown": "неизвестный показатель",
}
TECHNICAL_LABEL_RE = re.compile(
    r"^(?:[a-z][a-z0-9_]*\.[a-z0-9_]+|[a-f0-9]{32,64})$|/api/",
    re.IGNORECASE,
)
QUERY_STOPWORDS = frozenset({
    "а", "без", "бы", "в", "во", "где", "дай", "для", "есть", "за", "и",
    "из", "как", "какая", "какие", "какой", "какое", "ли", "мне", "мой",
    "моя", "моего", "на", "над", "о", "об", "от", "по", "под", "покажи",
    "показать", "проверь", "проверить", "про", "с", "сейчас", "сколько", "со",
    "там", "текущий", "текущее", "у", "что", "пожалуйста", "скажи", "устройство",
    "устройства", "свежие", "свежий", "данные", "текущие", "осталось", "остался",
    "осталась", "включи", "выключи", "переключи", "нажми", "запусти", "останови",
    "включить", "выключить", "зажги", "зажечь", "погаси", "погасить", "вруби",
    "отключи", "активируй", "деактивируй", "разблокируй", "заблокируй",
    "нет", "не", "лучше", "имел", "виду",
    "home", "assistant", "ha", "нужен", "нужна", "нужно", "только", "прочитай",
    "интересует", "уточни", "догадок", "изменяется", "сообщи", "известно",
    "показание", "предположений", "можно", "узнать", "посмотри", "показывает",
    "показывать",
    "почему", "отчего", "причина", "причины", "устройств",
    "ресурс", "ресурса", "процент", "процентов",
})
RU_ENDINGS = (
    "иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими", "остью", "ую",
    "юю", "ая", "яя", "ое", "ее", "ой", "ей", "ов", "ев", "ом", "ем", "ах",
    "ях", "ам", "ям", "ы", "и", "а", "я", "у", "ю", "е",
)


@dataclass(frozen=True, slots=True)
class Resolution:
    tier: str
    target_refs: tuple[str, ...]
    candidates: tuple[dict[str, Any], ...]
    weak: bool = False


def normalize_text(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise ValueError("invalid owner text")
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise ValueError("invalid owner text")
    if adapter.SENSITIVE_TEXT_RE.search(normalized):
        raise ValueError("invalid owner text")
    return " ".join(re.findall(r"[a-zа-я0-9]+", normalized))


def resolve_feature(value: str) -> str:
    normalized = normalize_text(value)
    # Long, specific phrases win before generic words such as resource/status.
    for feature in (
        "main_brush", "side_brush", "child_lock", "consumables", "battery",
        "filter", "humidity", "temperature", "power", "mode", "error", "unknown", "status",
    ):
        if any(normalize_text(term) in normalized for term in FEATURE_TERMS[feature]):
            return feature
    if "щетк" in normalized or "щеток" in normalized:
        return "consumables"
    tokens = _tokens(normalized)
    for feature in (
        "main_brush", "side_brush", "child_lock", "battery", "filter",
        "humidity", "temperature", "power", "mode", "error", "consumables",
    ):
        if any(
            _weak_word(token, term_token)
            for token in tokens for term in FEATURE_TERMS[feature]
            for term_token in _tokens(normalize_text(term))
        ):
            return feature
    return "status"


def _tokens(value: str) -> list[str]:
    return value.split()


def _stem(token: str) -> str:
    if len(token) < 5 or not re.search(r"[а-я]", token):
        return token
    for ending in RU_ENDINGS:
        if token.endswith(ending) and len(token) - len(ending) >= 4:
            return token[:-len(ending)]
    return token


def _edit_distance(left: str, right: str) -> int:
    if left == right:
        return 0
    if abs(len(left) - len(right)) > 2:
        return 3
    previous = list(range(len(right) + 1))
    for row, a in enumerate(left, 1):
        current = [row]
        for column, b in enumerate(right, 1):
            current.append(min(
                current[-1] + 1, previous[column] + 1,
                previous[column - 1] + (a != b),
            ))
        previous = current
    return previous[-1]


def _weak_word(left: str, right: str) -> bool:
    a, b = _stem(left), _stem(right)
    if a == b or (min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a))):
        return True
    limit = 1 if max(len(a), len(b)) < 8 else 2
    return min(len(a), len(b)) >= 5 and _edit_distance(a, b) <= limit


def _word_quality(left: str, right: str) -> int:
    if left == right:
        return 3
    if _stem(left) == _stem(right):
        return 2
    if min(len(_stem(left)), len(_stem(right))) >= 4 and _edit_distance(_stem(left), _stem(right)) <= 1:
        return 2
    return 1 if _weak_word(left, right) else 0


def _phrase_present(phrase: str, query: str) -> bool:
    phrase_tokens = _tokens(normalize_text(phrase))
    query_tokens = _tokens(query)
    width = len(phrase_tokens)
    return bool(width) and any(query_tokens[index:index + width] == phrase_tokens for index in range(len(query_tokens) - width + 1))


def _feature_words() -> set[str]:
    result: set[str] = set()
    for terms in FEATURE_TERMS.values():
        for term in terms:
            result.update(_tokens(normalize_text(term)))
    return result


FEATURE_WORDS = frozenset(_feature_words())


def normalize_device_query(value: Any, feature: str | None = None) -> str:
    normalized = normalize_text(value)
    del feature
    # Target resolution is independent of how many feature words a compound
    # read contains, so every closed-vocabulary feature phrase is removed.
    removed_features = FEATURES
    removed_words = {
        token for name in removed_features if name in FEATURE_TERMS
        for term in FEATURE_TERMS[name] for token in _tokens(normalize_text(term))
    }
    # These words can identify a light ("основной свет") and are feature
    # modifiers only inside the complete brush phrase.
    removed_words -= {"основная", "основной", "боковая", "боковой"}
    tokens = [
        token for token in _tokens(normalized)
        if token not in QUERY_STOPWORDS
        and token not in removed_words
        and not any(_weak_word(token, feature_word) for feature_word in removed_words)
    ]
    return " ".join(tokens)


def load_inventory(path: Path | None = None) -> dict[str, Any]:
    target = inventory_builder.inventory_path() if path is None else path
    try:
        raw = target.read_bytes()
    except OSError as error:
        raise ValueError("inventory unavailable") from error
    if not raw or len(raw) > MAX_INVENTORY_BYTES:
        raise ValueError("inventory unavailable")
    try:
        document = adapter.strict_json_loads(raw)
    except adapter.AdapterError as error:
        raise ValueError("inventory unavailable") from error
    if not isinstance(document, dict):
        raise ValueError("inventory unavailable")
    try:
        return inventory_builder.validate_inventory_document(document)
    except inventory_builder.InventoryError as error:
        raise ValueError("inventory unavailable") from error


def _entities(inventory: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = inventory.get("entities")
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise ValueError("entity inventory unavailable")
    return raw


def _targets(inventory: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key in ("physical_nodes", "logical_nodes"):
        raw = inventory.get(key)
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise ValueError("target inventory unavailable")
        result.extend(raw)
    return result


def _indexes(inventory: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    entities = {item["entity_ref"]: item for item in _entities(inventory) if isinstance(item.get("entity_ref"), str)}
    targets = {item["target_ref"]: item for item in _targets(inventory) if isinstance(item.get("target_ref"), str)}
    areas = {
        item["area_ref"]: item for item in inventory.get("area_nodes", [])
        if isinstance(item, dict) and isinstance(item.get("area_ref"), str)
    }
    integrations = {
        item["integration_ref"]: item for item in inventory.get("integration_nodes", [])
        if isinstance(item, dict) and isinstance(item.get("integration_ref"), str)
    }
    return entities, targets, areas, integrations


def _target_profile(
    target: Mapping[str, Any], entities: Mapping[str, Mapping[str, Any]],
    areas: Mapping[str, Mapping[str, Any]], integrations: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    members = [entities[ref] for ref in target.get("entity_refs", []) if ref in entities]
    enabled = [item for item in members if not item.get("disabled") and not item.get("hidden")]
    names = [str(value) for value in target.get("names", []) if isinstance(value, str)]
    aliases = [str(value) for value in target.get("aliases", []) if isinstance(value, str)]
    entity_names = [
        str(value) for item in enabled
        for key in ("display_name", "name", "original_name")
        for value in [item.get(key)] if isinstance(value, str)
    ]
    entity_aliases = [
        str(value) for item in enabled for value in item.get("aliases", []) if isinstance(value, str)
    ]
    area_names = [
        str(value) for ref in target.get("area_refs", []) if ref in areas
        for key in ("name",) for value in [areas[ref].get(key)] if isinstance(value, str)
    ]
    area_aliases = [
        str(value) for ref in target.get("area_refs", []) if ref in areas
        for value in areas[ref].get("aliases", []) if isinstance(value, str)
    ]
    domains = {str(item.get("domain")) for item in enabled if isinstance(item.get("domain"), str)}
    classes = {str(item.get("device_class")) for item in enabled if isinstance(item.get("device_class"), str)}
    platforms = {
        str(integrations[ref].get("platform")) for item in enabled
        for ref in item.get("integration_refs", []) if ref in integrations
        if isinstance(integrations[ref].get("platform"), str)
    }
    features = {str(item.get("component")) for item in enabled if item.get("component") in FEATURES}
    return {
        "target_ref": target.get("target_ref"), "kind": target.get("kind"),
        "display_name": target.get("display_name") or (entity_names[0] if entity_names else "Устройство"),
        "names": names, "aliases": aliases, "entity_names": entity_names,
        "entity_aliases": entity_aliases, "areas": area_names, "area_aliases": area_aliases,
        "domains": domains, "device_classes": classes, "platforms": platforms,
        "manufacturer_model": [
            str(value) for value in (target.get("manufacturer"), target.get("model")) if isinstance(value, str)
        ],
        "features": features, "enabled_members": enabled,
    }


def _type_concepts(query: str) -> set[str]:
    return {
        concept for concept, words in TYPE_CONCEPTS.items()
        if any(_phrase_present(word, query) for word in words)
    }


def _weak_type_concepts(query: str) -> set[str]:
    query_tokens = _tokens(query)
    return {
        concept for concept, words in TYPE_CONCEPTS.items()
        if any(
            any(
                _weak_word(token, word_token)
                for token in query_tokens for word_token in _tokens(normalize_text(word))
            )
            for word in words
        )
    }


def _profile_has_type(profile: Mapping[str, Any], concepts: set[str]) -> bool:
    domains = profile["domains"]
    classes = profile["device_classes"]
    if concepts & (domains | classes):
        return True
    values = [*profile["names"], *profile["entity_names"]]
    return any(
        _phrase_present(word, normalize_text(value))
        for value in values if value
        for concept in concepts for word in TYPE_CONCEPTS[concept]
    )


def _distinctive_tokens(query: str, concepts: set[str]) -> list[str]:
    type_tokens = [
        token for concept in concepts for word in TYPE_CONCEPTS[concept]
        for token in _tokens(normalize_text(word))
    ]
    return [
        token for token in _tokens(query)
        if not any(_weak_word(token, type_token) for type_token in type_tokens)
    ]


def _exact_any(query: str, values: Iterable[str]) -> bool:
    return any(normalize_text(value) == query for value in values if value)


def _weak_score(query: str, profile: Mapping[str, Any]) -> int:
    query_tokens = [token for token in _tokens(query) if token not in QUERY_STOPWORDS]
    if not query_tokens:
        return 0
    buckets = (
        (profile["aliases"], 80), (profile["names"], 75),
        (profile["entity_aliases"], 65), (profile["entity_names"], 60),
        ([*profile["areas"], *profile["area_aliases"]], 45),
        (profile["manufacturer_model"], 25), (profile["platforms"], 10),
    )
    candidate_tokens: list[tuple[str, int]] = []
    for values, weight in buckets:
        for value in values:
            if value:
                candidate_tokens.extend((token, weight) for token in _tokens(normalize_text(value)))
    matched = [
        max((weight * _word_quality(token, candidate) for candidate, weight in candidate_tokens), default=0)
        for token in query_tokens
    ]
    if not all(matched):
        return 0
    score = sum(matched)
    for value in [*profile["aliases"], *profile["names"]]:
        candidate = _tokens(normalize_text(value)) if value else []
        if len(candidate) == len(query_tokens) and all(_word_quality(a, b) >= 2 for a, b in zip(query_tokens, candidate)):
            score += 250
            break
    return score


def _distinctive_score(tokens: Sequence[str], profile: Mapping[str, Any]) -> int:
    values = [
        *profile["aliases"], *profile["names"], *profile["entity_aliases"],
        *profile["entity_names"], *profile["areas"], *profile["area_aliases"],
        *profile["manufacturer_model"],
    ]
    candidate_tokens = [token for value in values if value for token in _tokens(normalize_text(value))]
    return sum(max((_word_quality(token, candidate) for candidate in candidate_tokens), default=0) for token in tokens)


def resolve_targets(
    inventory: dict[str, Any], utterance: str, feature: str,
    *, allowed_target_refs: Sequence[str] | None = None,
) -> Resolution:
    if feature not in FEATURES:
        raise ValueError("unknown feature")
    full_query = normalize_text(utterance)
    query = normalize_device_query(full_query, feature)
    entities, targets, areas, integrations = _indexes(inventory)
    allowed = set(allowed_target_refs) if allowed_target_refs is not None else None
    profiles = [
        _target_profile(target, entities, areas, integrations)
        for ref, target in targets.items()
        if (allowed is None or ref in allowed)
    ]
    profiles = [profile for profile in profiles if profile["enabled_members"]]
    exact_tiers: list[tuple[str, list[dict[str, Any]]]] = []
    exact_tiers.append(("exact_alias", [profile for profile in profiles if _exact_any(query, profile["aliases"])]))
    exact_tiers.append(("exact_name", [profile for profile in profiles if _exact_any(query, profile["names"])]))
    concepts = _type_concepts(query)
    area_type = [
        profile for profile in profiles
        if any(
            _phrase_present(value, query)
            for value in [*profile["areas"], *profile["area_aliases"]] if value
        )
        and (_profile_has_type(profile, concepts) if concepts else feature in profile["features"])
    ]
    area_distinctive = _distinctive_tokens(query, concepts) if concepts else []
    if len(area_type) > 1 and area_distinctive:
        scored_area = [(_distinctive_score(area_distinctive, profile), profile) for profile in area_type]
        top_area = max(score for score, _profile in scored_area)
        if top_area:
            area_type = [profile for score, profile in scored_area if score == top_area]
    exact_tiers.append(("exact_area_type", area_type))
    exact_tiers.append(("entity_name_alias", [
        profile for profile in profiles
        if _exact_any(query, [*profile["entity_names"], *profile["entity_aliases"]])
    ]))
    for tier, matches in exact_tiers:
        unique = {str(item["target_ref"]): item for item in matches}
        if unique:
            selected = tuple(unique[key] for key in sorted(unique))
            return Resolution(tier, tuple(item["target_ref"] for item in selected), selected)

    type_matches = [profile for profile in profiles if concepts and _profile_has_type(profile, concepts)]
    physical_type_matches = [profile for profile in type_matches if profile["kind"] == "physical"]
    if len(physical_type_matches) == 1:
        selected = tuple(physical_type_matches)
        return Resolution("domain_device_class", tuple(item["target_ref"] for item in selected), selected)
    if len(type_matches) == 1:
        selected = tuple(type_matches)
        return Resolution("domain_device_class", tuple(item["target_ref"] for item in selected), selected)

    manufacturer_pool = type_matches or profiles
    manufacturer_matches = [
        profile for profile in manufacturer_pool
        if any(_phrase_present(value, query) for value in profile["manufacturer_model"] if value)
    ]
    if len(manufacturer_matches) == 1:
        selected = tuple(sorted(manufacturer_matches, key=lambda item: str(item["target_ref"])))
        return Resolution("manufacturer_model", tuple(item["target_ref"] for item in selected), selected)

    weak_concepts = concepts or _weak_type_concepts(query)
    weak_type_matches = [
        profile for profile in profiles
        if weak_concepts and _profile_has_type(profile, weak_concepts)
    ]
    weak_profiles = manufacturer_matches or type_matches or weak_type_matches or profiles
    if weak_profiles is profiles and feature not in {"status", "unknown"}:
        capable = [
            profile for profile in profiles
            if feature in profile["features"]
            or (feature == "consumables" and profile["features"] & {"main_brush", "side_brush", "filter"})
        ]
        if capable:
            weak_profiles = capable
    distinctive = _distinctive_tokens(query, weak_concepts) if weak_concepts else []
    physical_weak = [profile for profile in weak_type_matches if profile["kind"] == "physical"]
    if weak_type_matches and not distinctive and len(physical_weak) == 1:
        selected = tuple(physical_weak)
        return Resolution("morphology_typo", tuple(item["target_ref"] for item in selected), selected, True)
    if distinctive:
        narrowed = [(_distinctive_score(distinctive, profile), profile) for profile in weak_profiles]
        if any(score for score, _profile in narrowed):
            top = max(score for score, _profile in narrowed)
            selected = tuple(sorted(
                (profile for score, profile in narrowed if score == top),
                key=lambda item: str(item["target_ref"]),
            ))
            return Resolution("morphology_typo", tuple(item["target_ref"] for item in selected), selected, True)
    scored = [(score, profile) for profile in weak_profiles if (score := _weak_score(query, profile)) > 0]
    if scored:
        top = max(score for score, _profile in scored)
        selected_profiles = [profile for score, profile in scored if score == top]
        physical = [profile for profile in selected_profiles if profile["kind"] == "physical"]
        if weak_concepts and len(physical) == 1:
            selected_profiles = physical
        selected = tuple(sorted(selected_profiles, key=lambda item: str(item["target_ref"])))
        return Resolution("morphology_typo", tuple(item["target_ref"] for item in selected), selected, True)
    if manufacturer_matches:
        selected = tuple(sorted(manufacturer_matches, key=lambda item: str(item["target_ref"])))
        return Resolution("manufacturer_model", tuple(item["target_ref"] for item in selected), selected)
    if type_matches:
        selected = tuple(sorted(physical_type_matches or type_matches, key=lambda item: str(item["target_ref"])))
        return Resolution("domain_device_class", tuple(item["target_ref"] for item in selected), selected)
    if weak_type_matches:
        physical = [profile for profile in weak_type_matches if profile["kind"] == "physical"]
        selected = tuple(sorted(physical or weak_type_matches, key=lambda item: str(item["target_ref"])))
        return Resolution("morphology_typo", tuple(item["target_ref"] for item in selected), selected, True)
    return Resolution("none", (), ())


def public_candidate(profile: Mapping[str, Any], turn_ref: str) -> dict[str, Any]:
    def safe_label(value: Any, fallback: str) -> str:
        if not isinstance(value, str):
            return fallback
        normalized = " ".join(value.split())
        return normalized if normalized and not TECHNICAL_LABEL_RE.search(normalized) else fallback

    return {
        "ref": turn_ref,
        "label": safe_label(profile.get("display_name"), "Устройство"),
        "areas": [
            safe_label(value, "") for value in list(profile.get("areas", []))[:4]
            if safe_label(value, "")
        ],
        "kind": profile.get("kind"),
        "features": [
            FEATURE_PUBLIC_LABELS[value] for value in sorted(profile.get("features", []))
            if value in FEATURE_PUBLIC_LABELS
        ],
    }


def extract_action_scope(inventory: Mapping[str, Any], utterance: str) -> dict[str, Any]:
    """Extract owner-requested constraints without accepting model claims."""

    full_query = normalize_text(utterance)
    _entities_by_ref, _targets_by_ref, areas, _integrations = _indexes(inventory)
    requested_areas: list[str] = []
    area_tokens: set[str] = set()
    query_tokens = _tokens(full_query)
    for area in areas.values():
        names = [area.get("name"), *area.get("aliases", [])]
        for name in names:
            if not isinstance(name, str):
                continue
            normalized = normalize_text(name)
            tokens = _tokens(normalized)
            matched = _phrase_present(normalized, full_query) or (
                len(tokens) == 1 and any(_weak_word(token, tokens[0]) for token in query_tokens)
            )
            if matched:
                label = str(area.get("name") or name)
                if label not in requested_areas:
                    requested_areas.append(label)
                area_tokens.update(tokens)
                break
    concepts = tuple(sorted(_type_concepts(full_query)))
    removed_type_tokens = {
        token for concept in concepts for word in TYPE_CONCEPTS[concept]
        for token in _tokens(normalize_text(word))
    }
    device_query = normalize_device_query(utterance, "power")
    distinctive = [
        token for token in _tokens(device_query)
        if not any(_weak_word(token, removed) for removed in removed_type_tokens | area_tokens)
    ]
    return {
        "requested_areas": tuple(requested_areas),
        "requested_types": concepts,
        "requested_name": " ".join(distinctive) or None,
        "requested_feature": "power",
    }


def action_scope_matches(
    inventory: Mapping[str, Any], profile: Mapping[str, Any], scope: Mapping[str, Any],
) -> tuple[bool, str]:
    """Recheck every explicit owner constraint against the resolved target."""

    entities, _targets_by_ref, areas, integrations = _indexes(inventory)
    target_ref = profile.get("target_ref")
    targets = {item["target_ref"]: item for item in _targets(inventory)}
    target = targets.get(target_ref)
    if target is None:
        return False, "target_missing"
    canonical = _target_profile(target, entities, areas, integrations)
    candidate_area_names = [*canonical["areas"], *canonical["area_aliases"]]
    for requested in scope.get("requested_areas", ()):
        if not isinstance(requested, str) or not any(
            _weak_word(token, candidate)
            for token in _tokens(normalize_text(requested))
            for value in candidate_area_names
            for candidate in _tokens(normalize_text(value))
        ):
            return False, "area_mismatch"
    requested_types = scope.get("requested_types", ())
    if not isinstance(requested_types, (list, tuple)):
        return False, "type_mismatch"
    for concept in requested_types:
        if concept not in TYPE_CONCEPTS or not _profile_has_type(canonical, {concept}):
            return False, "type_mismatch"
    requested_name = scope.get("requested_name")
    if requested_name:
        if not isinstance(requested_name, str):
            return False, "name_mismatch"
        values = [
            *canonical["names"], *canonical["aliases"], *canonical["entity_names"],
            *canonical["entity_aliases"], *canonical["manufacturer_model"],
        ]
        candidate_tokens = [
            token for value in values for token in _tokens(normalize_text(value))
        ]
        if not all(
            any(_weak_word(token, candidate) for candidate in candidate_tokens)
            for token in _tokens(normalize_text(requested_name))
        ):
            return False, "name_mismatch"
    return True, "matched"


def snapshot_index(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    entities = snapshot.get("entities")
    if not isinstance(entities, list) or len(entities) > adapter.MAX_LISTED_ENTITIES:
        raise ValueError("snapshot unavailable")
    result: dict[str, dict[str, Any]] = {}
    for item in entities:
        if not isinstance(item, dict):
            raise ValueError("snapshot unavailable")
        try:
            entity_id = adapter._validate_entity_id(item.get("entity_id"))
        except adapter.AdapterError as error:
            raise ValueError("snapshot unavailable") from error
        result[entity_id] = item
    return result


def select_feature_entities(inventory: Mapping[str, Any], target_ref: str, feature: str) -> list[dict[str, Any]]:
    if feature not in FEATURES:
        raise ValueError("unknown feature")
    entities, targets, _areas, _integrations = _indexes(inventory)
    target = targets.get(target_ref)
    if target is None:
        raise ValueError("target unavailable")
    members = [
        entities[ref] for ref in target.get("entity_refs", []) if ref in entities
        and not entities[ref].get("disabled") and not entities[ref].get("hidden")
    ]
    if feature == "unknown":
        return []
    if feature == "consumables":
        selected = [item for item in members if item.get("component") in {"main_brush", "side_brush", "filter"}]
    elif feature == "status":
        selected = [item for item in members if item.get("component") == "status"]
        if not selected:
            selected = [item for item in members if item.get("component") in {"power", "mode", "error"}]
        if not selected and members:
            selected = [members[0]]
    else:
        selected = [item for item in members if item.get("component") == feature]
    return sorted(selected, key=lambda item: str(item.get("entity_ref")))


def target_context(inventory: Mapping[str, Any], target_ref: str) -> dict[str, Any]:
    entities, targets, areas, integrations = _indexes(inventory)
    target = targets.get(target_ref)
    if target is None:
        raise ValueError("target unavailable")
    profile = _target_profile(target, entities, areas, integrations)
    return {
        "target_ref": target_ref, "kind": profile["kind"],
        "display_name": profile["display_name"], "areas": list(profile["areas"]),
    }


def fresh_facts(
    snapshot: Mapping[str, Any], inventory: Mapping[str, Any], target_ref: str, feature: str,
) -> list[dict[str, Any]]:
    states = snapshot_index(snapshot)
    observed_at = snapshot.get("observed_at")
    result: list[dict[str, Any]] = []
    for metadata in select_feature_entities(inventory, target_ref, feature):
        entity_id = metadata.get("entity_id")
        result.append({
            "metadata": metadata,
            "fresh_state": states.get(entity_id) if isinstance(entity_id, str) else None,
            "observed_at": observed_at,
        })
    return result


def coverage(inventory: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, int]:
    metadata_ids = {item.get("entity_id") for item in _entities(inventory)}
    current_ids = set(snapshot_index(snapshot))
    enabled_ids = {
        item.get("entity_id") for item in _entities(inventory)
        if not item.get("disabled") and not item.get("hidden")
    }
    represented = current_ids & metadata_ids
    return {
        "current_entities": len(current_ids), "enabled_current_entities": len(current_ids & enabled_ids),
        "represented_current_entities": len(represented), "missing_current_entities": len(current_ids - metadata_ids),
        "physical_nodes": len(inventory.get("physical_nodes", [])),
        "logical_nodes": len(inventory.get("logical_nodes", [])),
    }


def get_model_index(inventory: dict[str, Any]) -> dict[str, Any]:
    """Host diagnostics only; it contains counts, never persistent current facts."""

    inventory_builder.validate_inventory_document(inventory)
    return {
        "schema_version": 2, "source": "Home Assistant registry metadata only",
        "entity_count": inventory["entity_count"],
        "physical_device_count": inventory["physical_device_count"],
        "logical_entity_count": inventory["logical_entity_count"],
        "area_count": inventory["area_count"], "integration_count": inventory["integration_count"],
    }


def dump_safe_candidate_set(candidates: Sequence[Mapping[str, Any]]) -> str:
    """Serialize only the model-safe candidate boundary."""

    return json.dumps(list(candidates), ensure_ascii=False, separators=(",", ":"))
