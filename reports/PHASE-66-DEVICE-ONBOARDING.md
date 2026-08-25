# Phase 66 / этап 9 — onboarding и настройки поведения

Дата source/runtime-квалификации: 2026-08-25.

Статус: реализовано, offline-квалифицировано и развёрнуто в
`/opt/home-butler`. Source/runtime hash `device_onboarding.py` совпадает;
read-only timer `enabled/active (waiting)`, последний oneshot завершился
`0/SUCCESS`, `pending_count=0`, `proposal_count=0`, `actions_performed=0`.
Private queue имеет owner `homebutler`, mode `0600`. HA config, integrations и
devices не изменялись.

Текущая queue schema — 2. Reader выполняет идемпотентную schema-1 migration и
сохраняет уже собранные items. Новое поле `owner_answers` удерживает проверенные
частичные ответы между репликами, поэтому владелец может назвать комнату,
политику уведомлений и критичность не одним сообщением.

## Что добавлено

- `device_onboarding.py` строит одну приватную очередь поверх существующих
  `home_assistant_inventory.py` и `ha_device_knowledge.py`. Второй DeviceGraph
  не создаётся.
- Без участия LLM собираются manufacturer/model, все observed integration
  paths, features/entities, semantic capabilities, area hints, device classes,
  aliases, обезличенный network identity status, diagnostic features,
  доступные локальные integration paths и safety class.
- Очередь сохраняется owner-only (`0600`) в state directory. Public/model view
  удаляет physical hash, HA device IDs и entity IDs. Names/attributes явно
  маркируются как untrusted facts.
- Новый read-only `ha_get_onboarding_queue` доступен общему MCP и bounded
  dialogue loop. Он не умеет менять HA.
- Новый read-only systemd timer обновляет очередь после knowledge refresh с
  `RestrictAddressFamilies=AF_UNIX` и `IPAddressDeny=any`.

## Вопросы и proposal

Вопрос формируется только для действительно отсутствующего поля:

- human name — если HA оставил generic name;
- area — если canonical area неизвестна;
- preferred integration — если найдено несколько observed/available paths.

Из ответа владельца deterministic builder создаёт proposal с полями:

- human name;
- area;
- aliases;
- criticality;
- notification policy;
- auto-recovery policy;
- preferred integration.

Restricted/unknown device нельзя перевести в automatic R1. Proposal получает
content hash; approval принимается только для этого exact hash и только при
явном подтверждении владельца.

Общий natural facade принимает обычный диалог. Модель видит human name, но не
получает onboarding ID или proposal hash и потому не может исказить opaque
identifier. Deterministic код сам разрешает единственный текущий item и требует
точную фразу `Подтверждаю предложение для <имя>.` Known facts могут подготовить
proposal без выдуманного ответа; невалидная policy не записывается даже как
partial answer.

## Adapter-specific plans

Модели доступны только предложенные opaque plan IDs:

| Plan | Изменение | Secure operator |
|---|---|---|
| `record_owner_profile` | только локальный owner profile | нет |
| `ha_registry_metadata_exact` | exact HA device metadata | нет |
| `local_integration_onboard_exact` | exact local integration plan | обязателен |

До approval adapter вызывается ноль раз. HA-writing plans дополнительно
требуют `live_qualified`; source-default остаётся staged. Private target IDs и
owner-approved values формирует deterministic executor, не модель. Credential
material приходит отдельным secure-operator callback, не сериализуется в
queue/model result/audit. Result обязан содержать closed status и readback;
`delivery_unknown` становится terminal и не повторяется.

## Structured behavior preferences

- Добавлены закрытые инструменты `behavior_get`, `behavior_set` и
  `behavior_reset`. Natural-инструкция сначала классифицируется общим bounded
  dialogue facade, затем модель может выбрать ровно один из этих tools.
- Разрешены только категории: подробность и тон ответа, quiet hours, пороги
  уведомлений, длительность подавления кратких инцидентов, preferred speaker,
  aliases, подробность отчёта и явно выбранные R1 recovery profiles.
- Значение каждой категории проверяется собственной JSON Schema и
  deterministic validator. Не существует поля, которым можно включить root,
  shell, arbitrary HA call, отключить verification/cooldown, раскрыть secret
  или изменить R3 policy.
- Настройки сохраняются как owner-scoped records в существующем SQLite Memory
  Store; повторное значение supersedes предыдущее. Они переживают restart и
  попадают в отдельный ограниченный блок Context Builder.
- `HOME-BUTLER-INSTRUCTIONS.md` сохранён как читаемый справочный файл, но его
  свободный текст больше не вставляется в production system prompt. Это
  устраняет путь обхода safety через редактирование строки поведения.
- Разрешение R1 profile лишь сохраняет предпочтение владельца. Оно не включает
  timer, не квалифицирует playbook и не вызывает adapter.

## Проверки

- onboarding/bounded dialogue/MCP/systemd targeted: 48 — OK;
- known facts are not asked again;
- public view contains no physical/entity/device IDs;
- restricted auto-R1 rejected;
- wrong proposal hash and missing owner confirmation rejected;
- unqualified HA plan makes zero adapter calls;
- qualified fixture calls exactly one fixed adapter and records audit;
- secure material is absent from response/audit;
- delivery-unknown retry rejected;
- private file round-trip remains `0600`.
- behavior/memory/dialogue/service targeted: 143 — OK;
- итоговый полный offline suite: 693 OK, 1 skipped;
- model evaluator: 7/7 PASS; два новых теста доказывают natural onboarding read
  и proposal/approval через фактическую Qwen 4.7B без HA write;
- no-cloud audit и `git diff --check`: PASS.

Production read-only proof: защищённый POST local chat с cookie, допустимым
Origin и CSRF на фразу `Есть новые устройства?` завершился HTTP 200 и честно
сообщил, что ожидающих устройств нет. Ранее отклонённый синтетический POST был
правильной работой CSRF/Origin protection, а не отказом agent path.

## Rollback

Исходная source-квалификация не меняла runtime; последующий Phase 66
deployment установил read-only timer. Rollback: убрать read-only
onboarding unit/timer, MCP read tool и новый module; existing inventory,
knowledge catalog and HA configuration remain untouched.
