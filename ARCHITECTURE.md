# Architecture — Stage 71

## Единственный production path

```text
local_chat_gateway ─┐
                    ├→ owner_chat → bounded_ha_agent
alice_skill_gateway ┘                    │
                                         ├→ metadata-only HomeGraph
                                         │        └→ host resolver/candidates
                                         └→ fresh GET /api/states
                                                   ↓
                                              ReadReceipt
                                                   ↓
                                            grounded answer

home_assistant_inventory → HA entity/device/area registries + GET metadata
                         → inventory.json без current values
```

Hermes gateway и optional MCP transport удалены: у реального Web/Alice path не
было caller. `home_assistant_mcp.py` сохранил имя файла, но теперь это только
единственный host-side resolver, без сервера и transport.

## HomeGraph

Один schema v5 graph содержит `physical_nodes`, `logical_nodes`, `area_nodes`,
`integration_nodes` и связывающие их entity metadata. Physical nodes создаются
только по точному HA device-registry identity. Logical entity без `device_id`
получает собственный стабильный node; имя, модель и комната не объединяют
representation.

Persistent graph хранит names/aliases, translation/entity category,
device/state class, unit, supported features, options, min/max/step,
disabled/hidden, platform/integration и area. State, availability, value и
timestamps запрещены validator-ом. Любой текущий факт приходит из нового HA
snapshot внутри текущего read turn.

## Семантический контракт

Host формирует закрытый `IntentFrame` (`conversation`, `read`,
`clarification`) и применяет ordered resolver:

1. exact alias;
2. exact name;
3. exact area + type;
4. entity name/alias;
5. domain/device class;
6. manufacturer/model;
7. morphology/typo как слабый сигнал.

Feature vocabulary закрыт: power, status, battery, filter, main/side brush,
humidity, temperature, child lock, mode, error, consumables и unknown. Модель
видит безопасные candidate labels и только turn-local refs; она не создаёт
Home Assistant IDs.

Сначала создаётся typed `ReadReceipt`, затем единственный renderer формирует
ответ. Поддержаны number+unit, boolean, on/off, binary sensor semantics, enum,
problem, unknown, unavailable и redacted. Причина сообщается только при наличии
causal evidence. Session focus живёт только в памяти transport session и имеет
TTL 20 минут.

## Проверка

Независимый `tests/stage71_oracle.py` не импортирует production resolver,
inventory, read adapter или renderer. Он независимо проверяет target, полный
набор requested values, fresh typed values и соответствие готового ответа
receipts. Live acceptance выполняет только GET и три registry-list команды;
HA service calls отсутствуют.
