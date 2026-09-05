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

Area provenance: resolver context разделяет `registry_areas` (только HA
`area_refs`) и `inferred_areas` (отдельная гипотеза из human metadata при
отсутствии registry binding и единственном room concept). Effective area для
shadow targeting сохраняется; это не превращает inferred area в HA-факт.
`ReadReceipt.areas` содержит только registry areas. Independent oracle
проверяет их напрямую по registry metadata и не импортирует resolver.

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
с resolved target. Один structured IntentFrame parser имеет deterministic fast
path для очевидных форм и bounded Qwen fallback для свободной речи. Модель
возвращает только action/requested name/area/type/feature, не видит candidates
и не выбирает target.

Strong unique exact name/alias/entity-name или area+type принимается host без
модели. Exact-name ties и остальные равные candidates всегда дают
clarification. Unique weak/fuzzy evidence проходит отдельную повторную проверку
owner tokens и строгого score margin по всему HomeGraph; недостаточная evidence
даёт clarification. Затем host создаёт immutable ActionPlan с
process-local HMAC seal. В плане нет entity/device/capability ID или service
path, а исполнительного API не существует. Machine-readable trace содержит
intent, безопасных candidates, выбранный label, policy и обязательные
`service_calls=0`, `ha_post=0`.

Planning не вызывает fresh-state adapter. Единственная HA HTTP-функция имеет
закрытую сигнатуру без method и сама отправляет только GET к двум allowlisted
paths; instrumented acceptance дополнительно блокирует любой HA non-GET.

Для action resolver строит turn-local проекцию только enabled `light`/`switch`
entities из того же HomeGraph. Parent physical identity не меняется и не
сливается с другими representations. Exact physical name/alias проверяется
раньше entity projection, поэтому physical tie остаётся clarification. Area без
registry binding выводится только из human metadata при ровно одном room
concept; multi-room metadata не превращается в strong evidence.

## Проверка

Stage 71 independent oracle продолжает защищать read path. Stage 72 corpus
содержит ровно 1000 raw команд, frozen owner blind corpus — 40 строк, новый
natural-language corpus — 100 фраз. Отдельные live harnesses проверяют
production parser/resolver, текущий real-home metadata graph и фактическую
локальную модель при физически заблокированном HA POST. Реального action
executor нет.
