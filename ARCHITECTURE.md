# Architecture — Stage 72

## Единственный production path

```text
local_chat_gateway ─┐
                    ├→ owner_chat → bounded_ha_agent
alice_skill_gateway ┘                    │
                                         └→ metadata-only HomeGraph
                                                  └→ host resolver/candidates
                                                           │
                                      read ────────────────┼──── action
                                       ↓                   ↓
                              fresh GET /api/states  ActionPolicyRegistry
                                       ↓                   ↓
                                  ReadReceipt       sealed ActionPlan
                                       └──────────┬────────┘
                                             grounded answer

home_assistant_inventory → HA entity/device/area registries + GET metadata
                         → inventory.json без current values
```

Hermes gateway и optional MCP transport отсутствуют. `home_assistant_mcp.py`
сохраняет историческое имя файла, но является единственным host-side resolver,
без MCP-сервера и transport.

## HomeGraph и read contract

Один schema v5 graph содержит `physical_nodes`, `logical_nodes`, `area_nodes`,
`integration_nodes` и entity metadata. Physical nodes создаются только по
точному HA device-registry identity. Logical entity без `device_id` получает
собственный стабильный node; имя, модель и комната не объединяют representation.

Persistent graph хранит только безопасную metadata. State, availability,
current value и timestamps запрещены validator-ом. Read turn всегда делает
новый HA snapshot, затем создаёт typed ReadReceipt и grounded answer. Причина
сообщается только при causal evidence. Session focus ephemeral, TTL 20 минут.

Host формирует закрытый `IntentFrame` (`conversation`, `read`, `action`,
`clarification`) с action/value/scope. Ordered resolver остаётся единственным:

1. exact alias;
2. exact name;
3. exact area + type;
4. entity name/alias;
5. domain/device class;
6. manufacturer/model;
7. morphology/typo как слабый сигнал.

## Shadow action planning

Единственный `ActionPolicyRegistry` разрешает создать план только для
`light|switch × turn_on|turn_off`. Vacuum, button, appliance, lock, climate,
script, остальные domains и все прочие actions получают hard-deny. Scope
содержит запрошенные room/type/name/feature; host повторно сверяет каждое поле
с resolved target. Равные кандидаты всегда ведут к clarification до вызова
модели.

Модель получает лишь safe labels и turn-local `rN`, после чего может выбрать
только opaque ref или clarification. Host создаёт immutable ActionPlan с
process-local HMAC seal. В плане нет entity/device/capability ID или service
path, а исполнительного API не существует. Machine-readable trace содержит
intent, безопасных candidates, выбранный label, policy и обязательные
`service_calls=0`, `ha_post=0`.

Planning не вызывает fresh-state adapter. Единственная HA HTTP-функция имеет
закрытую сигнатуру без method и сама отправляет только GET к двум allowlisted
paths; instrumented acceptance дополнительно блокирует любой HA non-GET.

## Проверка

Stage 71 independent oracle продолжает защищать read path. Stage 72 corpus
содержит ровно 1000 raw команд, frozen owner blind corpus — 40 строк. Отдельный
live harness проверяет production parser/resolver и фактическую локальную
модель при физически заблокированном HA POST. Реального action executor нет.
