# Phase 66 — benchmark локальных 2B/4B моделей

Дата: 2026-08-23. Benchmark синтетический, локальный и read-only: он не вызывал
Home Assistant tools, не переключал устройства и не загружал новые модели.

> Текущий production override после последующей warm voice qualification:
> все пять profiles используют `qwen3.5:4b-q4_K_M`; voice/structured — 8K,
> dialogue/diagnostic — 32K, summarizer — 16K. Таблица первоначальной
> рекомендации 2B ниже сохранена как исторический результат benchmark, а не
> как текущая конфигурация. Windows GPU supervisor также получает default
> 32K из этого же Model Runtime Policy, без literal 2048.
>
> Разделы с пометкой «исторический» ниже фиксируют состояние на
> момент отдельной миграции. Они не описывают текущий runtime.

## Методика

Сравнивались уже установленные:

- `home-butler:latest`, 2.3B, Q4_K_M;
- `qwen3.5:4b-q4_K_M`, 4.7B, Q4_K_M.

Для каждой модели проверены 8K, 16K, 32K и 64K. Каждый case включал:

1. три коротких русских voice-like turn;
2. strict JSON Schema;
3. выбор единственного read-only tool и извлечение названия физического устройства;
4. семантическую классификацию diagnostic feature;
5. удержание маркера из начала длинного контекста;
6. `/api/ps` evidence по context и GPU offload.

Benchmark implementation: `scripts/benchmark_model_runtime.py`. Hardcoded
context matrix изолирована в benchmark fixture и не является production policy.

## Результаты

| Модель | Context | Full GPU | VRAM bytes | Early fact | Strict JSON | Tool + entity | Semantic diagnostic | Voice P50/P95, s |
|---|---:|---|---:|---|---|---|---|---:|
| 2B | 8K | да | 1 695 432 046 | pass | pass | pass | fail | 0.729 / 4.851 |
| 2B | 16K | да | 1 765 686 638 | pass | pass | pass | fail | 0.798 / 14.397 |
| 2B | 32K | да | 1 906 195 822 | pass | pass | pass | fail | 0.590 / 5.379 |
| 2B | 64K | да | 2 422 126 672 | pass | pass | pass | fail | 0.488 / 5.141 |
| 4B | 8K | да | 3 301 535 906 | pass | pass | pass | fail | 1.168 / 22.236 |
| 4B | 16K | да | 3 460 919 458 | pass | pass | pass | fail | 37.420 / 39.699 |
| 4B | 32K | да | 3 779 686 562 | pass | pass | pass | fail | 1.182 / 6.207 |
| 4B | 64K | да | 4 417 220 770 | pass | pass | pass | fail | 0.711 / 6.806 |

P95 включает cold/context reload и поэтому является более честной границей для
Alice, чем лучший warm turn.

Усиленная отдельная проверка entity resolution во всех восьми случаях получила
`pass`: на естественный вопрос модель выбрала только read-only tool и передала
название именно запрошенной посудомоечной машины, а не произвольную строку.

Короткий tool-call throughput (generation, не полная end-to-end latency):

| Модель | 8K | 16K | 32K | 64K |
|---|---:|---:|---:|---:|
| 2B, token/s | 56.17 | 57.97 | 57.94 | 98.65 |
| 4B, token/s | 28.53 | 28.65 | 28.68 | 56.95 |

На коротком tool-call небольшое число выходных токенов делает 64K token/s
шумным показателем. Решение о context поэтому принято по совокупности full-GPU,
длинного prompt evaluation, P95 и качества, а не по одной этой строке.

Длинный memory prompt:

| Модель | Context | Prompt tokens | Prompt evaluation, s | Retention |
|---|---:|---:|---:|---|
| 2B | 8K | 6 480 | 11.406 | pass |
| 2B | 16K | 12 891 | 31.693 | pass |
| 2B | 32K | 25 714 | 81.942 | pass |
| 2B | 64K | 47 026 | 87.844 | pass |
| 4B | 8K | 6 480 | 23.157 | pass |
| 4B | 16K | 12 891 | 63.227 | pass |
| 4B | 32K | 25 714 | 156.945 | pass |
| 4B | 64K | 47 026 | 197.619 | pass |

## Что benchmark опроверг

- Возможность выделить 64K не означает пригодность 64K для каждого turn.
- Больший context не исправил semantic diagnostic.
- Обе модели при слишком коротком system prompt отвечали как generic home
  assistant, а не называли точную роль Home Butler.
- 4B не выдержал установленный 6-секундный P95 voice budget с учётом reload.

Это реальные quality defects. Validator не был ослаблен ради зелёного отчёта.

## Первоначально выбранные profiles — superseded

| Profile | Модель | Context | Output | Tool turns | Latency budget | Fallback |
|---|---|---:|---:|---:|---:|---|
| `voice_fast` | 2B Home Butler alias | 8K | 256 | 2 | 3.2 s | verified deterministic |
| `dialogue` | 4B | 32K | 1024 | 4 | 45 s | `voice_fast` |
| `diagnostic` | 4B | 32K | 2048 | 6 | 180 s | `structured` |
| `structured` | 2B Home Butler alias | 8K | 2048 | 2 | 20 s | verified deterministic |
| `summarizer` | 2B Home Butler alias | 16K | 1024 | 0 | 120 s | none |

Прямой model turn не всегда укладывается в 3.2 секунды voice budget, поэтому
Alice не ждёт его бесконечно: cold/reload miss должен завершаться честным
детерминированным fallback в пределах транспортного deadline. 4B для этого
маршрута отвергнут окончательно.

64K остаётся допустимым benchmark/maintenance режимом, но не production default.
ContextBuilder обязан собирать компактный релевантный prompt даже для 32K
profile.

## Историческая migration note: Runtime Policy schema 1 (до deployment)

Единым источником параметров стал `scripts/model_runtime_policy.py`. На него
переведены local chat, Alice gateway, HA read/control proofs, recovery planner,
health report, entity study/report, stress-test analyzer, model alias builder,
Hermes projection и установленный runtime verifier. Независимые production
`num_ctx`, model names и request timeouts удалены.

Миграция не меняет schema пользовательского состояния, incident DB, inventory,
workspace, reminders или Alice identity. Она поэтому idempotent и не требует
конвертации данных. Rollback состоит в возврате policy/config/call-site patch;
данные владельца удалять или восстанавливать не требуется.

На момент этой migration note Runtime Policy была только в Git
working tree, а live deployment ещё ожидал разрешения. Это историческое
состояние закрыто последующим deployment и readback, описанным ниже.
Benchmark уже доказывал, что выбранные тогда 2B/8K и 4B/32K profiles
полностью помещаются в VRAM.

## Исторический regression evidence Phase 1

- targeted policy/migration suite: 191 tests, pass;
- complete offline suite: 518 tests, pass; 1 штатно skipped;
- no-cloud audit: pass, cloud keys и cloud fallback отсутствуют;
- syntax compilation и `git diff --check`: pass;
- isolated model evaluation: 4/5, без улучшения валидатора;
- benchmark tool selection и entity resolution: pass во всех 8 model/context
  cases;
- semantic diagnostic benchmark: fail во всех 8 cases и остаётся открытым до
  Semantic Entity Catalog/Log Intelligence.

Старый regression contract, вручную требовавший `context_length: 2048`, заменён
проверкой значения из immutable Runtime Policy. Отдельный static test падает,
если production conversational call-site снова вводит собственный context,
model route или устаревший `VOICE_NUM_CTX`.

## Историческая migration note: Persistent Memory schema 1

Local chat и Alice теперь используют единый owner-scoped SQLite store с FTS5.
В schema 1 входят bounded recent turns, компактная структурированная сводка,
semantic owner/device/episodic/procedural memory, несколько независимых active
goals и secret-safe retrieval traces. Исправление владельца явно supersede-ит
старый факт; TTL, revoke и status исключают запись из поиска. Raw transport
session ID заменяется необратимым fingerprint.

`ContextBuilder` извлекает только релевантные блоки с отдельными бюджетами и
общим пределом 4 100 approximate tokens. Memory всегда помечается как untrusted
reference и не разрешает действия. Векторная база не добавлялась: для текущего
объёма достаточно SQLite FTS5/BM25. Одна пара user/assistant и её summary
фиксируются атомарным commit.

Миграция создаёт новый private DB с schema marker при первом старте и не меняет
существующие incident/inventory/workspace данные. Rollback: вернуть gateway,
unit и installer patch; новый DB можно оставить нетронутым для последующего
возврата. Удалять его автоматически запрещено.

Acceptance evidence:

- 100 последовательных exchanges сохраняют только последние 200 turns;
- после повторного открытия DB и смены local-chat на Alice сохраняются alias,
  correction, owner preference и active goal;
- новый goal не уничтожает прежний, а trace хранит IDs/reasons/token counts без
  текста запроса;
- complete offline suite: 528 tests, pass; 1 штатно skipped;
- no-cloud audit: pass; isolated model evaluation остаётся честным 4/5.

На момент этой записи изменения памяти находились только в Git
working tree. Последуюющие deployment и 100-turn/restart/cross-transport tests
закрыли этот исторический gate.

## Историческая migration note: DeviceGraph inventory schema 3

Существующий `home_assistant_inventory.py` расширен без создания второго графа.
Schema 3 добавляет безопасные area/entity aliases, производителя, модель,
версию ПО, integration/domain metadata, semantic role/capability, component,
availability, timestamps и bounded semantic attributes. Registry IDs,
config-entry IDs, raw connections, IP и MAC остаются в private inventory и не
передаются model-facing MCP tools. Schema 1/2 мигрируется в памяти
идемпотентно; отсутствующие факты остаются `None`/пустыми и не выдумываются.

Общий sanitizer ограничивает типы, глубину, количество элементов и размер,
отбрасывает credentials/URL/private network data/control characters и помечает
строковые semantic values как untrusted data. MCP facade сохраняет три старых
tools и добавляет десять read-only tools для compact index, discovery,
details, diagnostics, incidents, logs, history и capabilities. История читает
только одну валидированную entity, максимум за 24 часа и 64 observations, с
`no_attributes`; при недоступности history endpoint честно возвращается только
текущий observation.

Официальные возможности HA отражаются отдельно: core version, endpoint
`/api/mcp`, обнаружение Assist/conversation integrations и registry aliases.
`GetLiveContext` и exposed-entity policy остаются `not_verified` и
`not_imported`, пока не выполнена отдельная live qualification; стабильный
inventory остаётся source of truth для identity/recovery.

Миграция не перезаписывает старый inventory до успешного следующего atomic
collect. Rollback заключается в возврате к старому reader: schema 1/2 данные
не удаляются. Установленные services и Home Assistant не перезапускались.

Acceptance evidence этапа 3:

- targeted semantic inventory/MCP/history/sanitizer compatibility: 89 tests,
  pass;
- старые `ha_get_snapshot`, `ha_search_entities`, `ha_get_device` сохранены;
- MCP discovery показывает 13 tools и не содержит service-call surface;
- fixture с доступным vacuum и недоступным filter feature сохраняет physical
  device как available и выделяет только diagnostic component;
- hostile attributes, foreign history entity и private network values не
  проходят model-facing boundary.
- complete offline suite: 538 tests, pass; 1 штатно skipped;
- no-cloud audit: pass;
- isolated model evaluation: 4/5; safe tool selection остаётся красным и
  переносится в Phase 7 bounded tool loop, а не маскируется regex-ответом.

На момент Phase 3 тогдашний installed runtime после evaluation показал
`context_length=4096` для загруженного 2B alias. Это не новый policy default, а
историческое доказательство ещё неразвёрнутой working-tree policy, а не текущий
production status. Актуальный `/api/ps` readback приведён ниже.

## Историческая migration note: Semantic Entity Catalog schema 2 и Log Intelligence v2

Существующий `ha_model_study.py` преобразован в полный Semantic Entity Catalog,
не создавая второго DeviceGraph. Он классифицирует каждую inventory entity,
использует domain/device class/translation/original name/unit/options/siblings и
текущее наблюдение, а результат принимает только через закрытую schema. Старый
schema 1 мигрируется идемпотентно; перед первой заменой сохраняется private
schema-1 backup. Модель не может добавить entity, изменить её state/evidence или
инициировать action.

`diagnostic_monitor.py` теперь формирует finding конкретной функции физического
устройства: component, issue class, evidence, confidence, first/last observed,
resolution condition и `observe_only`. Поэтому один проблемный feature робота
не превращается в отказ всего прибора. Неизвестный числовой код сообщается без
выдуманной расшифровки.

`system_log_diagnostics.py` v2 сначала нормализует только warning/error,
ограничивает и редактирует URL, auth data, private address/MAC и control data,
после чего передаёт текст модели как `untrusted_data`. Semantic classifier
возвращает только закрытые category/integration/entity/component/confidence/
persistence/evidence/explanation/read-only-checks и всегда
`action_authority=none`. Результат повторно валидируется после classifier
boundary. Доказанная связь с entity/action выполняется отдельно по одному
точному recent service-call; неоднозначный набор не выбирает случайную entity.
Raw log text в incident DB не сохраняется.

Production classifiers больше не содержат специального словаря производителей,
названий расходников или известных vendor error phrases. Общий semantic rubric
описывает типы данных и смысл metadata, а не конкретные устройства.

Acceptance evidence Phase 4:

- fixture vacuum с 26 features: одна diagnostic problem, physical device остаётся
  available;
- новое устройство и новый заменяемый ресурс: specific consumable finding без
  literal production rule;
- неизвестный numeric code: точный код и честное отсутствие расшифровки;
- live synthetic 4B qualification: consumable/error-code/ordinary measurement
  распознаны корректно, actions performed = 0;
- live synthetic log qualification: новые connectivity/device-error categories
  и integration correlation распознаны; hostile instruction осталась unknown,
  action authority = none;
- targeted Phase 4 suite: 18 tests, pass;
- complete offline suite после финальной prompt-коррекции: 549 tests, pass; 1
  штатно skipped;
- no-cloud audit: pass;
- isolated legacy model evaluation: 4/5. Safe tool selection остаётся открытым
  Phase 7 gate; загруженный live alias всё ещё 2.3B/4096 и не считается
  развёрнутой Runtime Policy.

На момент Phase 4 Home Assistant, устройства и Home Butler services не
перезапускались, а catalog/log pipeline был только в Git working tree.
Последующий deployment закрыл этот исторический gate.

## Текущий production readback и интерпретация

После deployment и single-instance исправления GPU supervisor от 2026-08-24:

- production model: `qwen3.5:4b-q4_K_M`, 4.7B, Q4_K_M;
- dialogue-turn: `/api/ps context_length=32768`,
  `size_vram=3779686562`, full GPU offload;
- profiles: voice/structured 8K, dialogue/diagnostic 32K, summarizer 16K;
- 30 warm Alice facade turns: 30/30 completed, P95 2.981 s при budget
  3.2 s;
- isolated evaluator: 7/7, включая natural onboarding и deterministic approval
  без HA write;
- Semantic Entity Catalog acceptance fixtures и live synthetic qualification
  проходят; это доказывает весь bounded pipeline, а не неконтролируемое
  «самообучение» модели;
- полная регрессия: 693 tests OK, 1 skipped; no-cloud PASS.

## Оставшиеся ограничения benchmark

- Первичный model/context benchmark использовал три синтетических voice sample
  на case. Его дополняет последующий 30-turn warm facade test, но
  controlled real Funnel outage O/P всё ещё не выполнялся.
- Исходный raw semantic-diagnostic prompt был красным во всех восьми
  model/context cases. Это не скрыто: production качество даёт последующий
  Semantic Entity Catalog с closed schema, metadata evidence и validator.
- Benchmark не проверяет live device actions.
- Production migration использует `scripts/model_runtime_policy.py`; Modelfile,
  Hermes, Alice, local chat, proofs, reports и planners больше не задают
  независимые context/model/timeouts.
