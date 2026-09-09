# Architecture — Stage 73 Phase A / Stage 72 production

Stage 73 разрабатывается отдельно; production остаётся на завершённом Stage 72.
Ниже сохранён базовый read/shadow path. Canary расширяет его после sealed shadow
plan: `canary_contract → canary_write_adapter → canary_verifier → ActionReceipt`
и возвращает receipt существующему renderer в `bounded_ha_agent`.

`canary_contract` не разрешает имена: он проверяет точные owner-approved
bindings. Physical shadow target связывается с entity только при единственном
enabled light/switch output во всём physical node, не после allowlist filtering.
Второго HomeGraph/resolver/conversation path нет.

Root-owned `/etc/home-butler/canary.json` содержит оба OFF-by-default флага,
отдельный owner approval и 3–5 private allowlist records. Registry binding либо
точный (`registry_area_ref` + `registry_area`, `owner_area=null`), либо явно
отсутствующий (оба registry поля `null`, отдельный обязательный `owner_area`).
`null` не wildcard: HomeGraph хранит metadata-only `registry_area_unassigned`,
истинный только при явных null в entity и parent registry rows. Missing field,
неизвестная area reference и ошибка чтения не подтверждают отсутствие привязки.
Inferred area никогда не становится registry fact. Owner area используется
только для проверки scope после разрешения target; allowlist не участвует в
resolver и не снимает ambiguity. Sealed plan сохраняет обе provenance отдельно;
`resolved_area` равна registry room либо отдельно подтверждённой owner room.
Любое появление binding, даже в той же комнате, инвалидирует прежний fingerprint.
Write credential имеет отдельное имя
`home-assistant-action.token`; installer не выдаёт его и не включает control.

Один adapter принимает только SealedActionPlan. Он использует fixed service
mapping, fresh metadata/BEFORE read, durable pre-send reservation и один POST
без retry. Verifier независимо делает AFTER/STABILITY GET и registry read.
HTTP response не является доказательством состояния. Внешний acceptance oracle
в `tests/stage73_oracle.py` использует собственный GET и read-only registry
transport; production resolver/executor/verifier/renderer он не импортирует.

`accepted_unverified` / `delivery_unknown` продолжаются через GET-only
`verify_pending`, не через повторное execution. Окно result event — 30 s,
канал ephemeral и отдельный для каждой Web/Alice session. Web получает event
через authenticated GET `/api/action-results`; Alice/CLI — на следующем turn.
Это не scheduler и не unsolicited push. Пока delivery не подтверждён, новый
POST блокируется durable journal. Истёкшее окно не превращает unknown в success.
Подготовленный live runner default STOP: отдельный root-owned Phase B approval
связан с Git/runtime/manifest/allowlist. Сам он control flags не включает.
Prepared runner сверяет owner-reviewed expected room до отправки и затем
сравнивает ответ именно с frozen expected room, не с результатом resolver.
Independent registry oracle отдельно проверяет и точную room, и явное отсутствие
binding. Owner metadata не записывается в HA, HomeGraph или ReadReceipt.

Sticky emergency latch выключает оба effective control flags во всех процессах.
Обычный rollback — новый bounded plan на захваченное BEFORE state, с one-use
key и тем же verifier; после emergency запрет control не обходится. Неуверенная
identity означает отсутствие автоматического rollback и необходимость решения
владельца. Phase A не считается законченной без обязательного owner corpus.

Exact entity на mixed light/switch parent может разрешаться отдельно; имя
parent с несколькими выходами требует clarification. Disabled config child-lock
с явным `translation_key=child_lock` не классифицирует parent как физический
замок; lock-target и прочие опасные domains по-прежнему запрещены. Изменение
этой metadata инвалидирует canary binding. Вопрос «как включить» не является
командой и получает host-ответ без model-generated инструкций о доме.
Whole physical name после морфологии/одной перестановки букв принимается лишь
после host revalidation; это не разрешение выбирать child channel родителя.
Явный room qualifier не превращается в exact physical name удалением предлога.

## Базовый Stage 72 production path

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
path, а исполнительного API в Stage 72 production не существует. Machine-readable trace содержит
intent, безопасных candidates, выбранный label, policy и обязательные
`service_calls=0`, `ha_post=0`.

Planning не вызывает fresh-state adapter. В Stage 72 HA HTTP-функция имеет
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
локальную модель при физически заблокированном HA POST. В production action
executor нет. Stage73 shadow draft и его независимые expectations описаны в
`STAGE-73-RESULT.md`; они ещё не являются owner-reviewed canary acceptance.
