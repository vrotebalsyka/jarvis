# Stage 72 Correction — Strong Host Decision and Free Speech

Статус: `GREEN_SHADOW_NOT_DEPLOYED`. Stage 73 не начат. Реальные Home
Assistant actions, POST и service calls не выполнялись; production services не
активировались и не перезапускались.

## Baseline и публикация

До начала correction commit `ecd2f6830ae82d0f3925a7d787b1cc510363c21d`
был опубликован в `origin/stage72-shadow-action-planning`. Он сохраняет
независимую pre-correction acceptance 56/60 и её красный evidence report.

## Разбор E12, E20, M03, M04

- E12 `отключи вытяжке` и E20 `отключи my-pc`: `exact_name`, один candidate,
  scope matched, policy `allow_shadow`. Старая Qwen selector всё равно могла
  вернуть `clarify`; это были ложные clarification.
- M03/M04 для `кавидор`: текущий HomeGraph содержит два разных physical
  device-registry objects с одинаковым human name, разрешённым switch domain,
  разными Tuya/localtuya integration bindings и без общей strong identity.
  Это настоящая ambiguity. В independent manifest human target сохранён, а
  expected outcome исправлен на `clarification`; произвольного выбора нет.

Ни одного правила для `вытяжке`, `my-pc`, `кавидор` или другого конкретного
домашнего устройства в production не добавлено.

## Исправленный contract

Один `parse_action_intent` строит закрытый IntentFrame с action и scope:
requested name, area, type и feature. Очевидные глаголы, отрицания, состояния
света и follow-up имеют deterministic fast path. Свободные просьбы используют
bounded `qwen3.5:2b` fallback с compact JSON schema. Модель не получает
candidates и не может вернуть entity ID, device ID, capability ID, service path
или target ref.

Target выбирает только host:

1. unique exact alias/name/entity-name или exact area+type — принять без Qwen;
2. exact-name tie или любое равенство итоговых candidates — clarification без
   model target selection;
3. weak/fuzzy unique — повторно сверить все owner tokens с metadata и потребовать
   строгий score margin над каждым HomeGraph competitor;
4. scope mismatch или недостаточная evidence — clarification;
5. unsupported/dangerous domain — hard-deny до возможности создать plan.

Model-extracted scope fields без подтверждения owner text отбрасываются; host
повторно извлекает и канонизирует scope из исходной реплики. Ephemeral session
focus поддерживает `а теперь выключи его` и не сохраняется между сессиями.

## New blind natural-language corpus

Frozen JSONL содержит ровно 100 новых фраз, ни одна не совпадает со старым
generated 1,000-command corpus и ни одна полностью не hardcoded в production:

- 35 natural deterministic;
- 30 natural model-oriented;
- 10 negation;
- 10 ambiguity;
- 5 forbidden;
- 10 session follow-up.

Включены все обязательные классы: `сделай свет в ванной`, `пусть в кабинете
будет светло`, `можешь погасить в туалете`, `убери свет в коридоре`, желание
светящегося зеркала, контекст `в кабинете темно`, явное отрицание и
местоименный follow-up. SHA-256:
`c7481eab286f10adb4abde4c8ea7577c2053c81dadf60ade03be7ca51896524a`.

Разработка не скрывает промежуточные результаты: runs были 47/100, 87/100 и
98/100. Одна исходная A01-разметка противоречила fixture truth: в graph есть
unique exact target `свет ванной`, поэтому по strong contract outcome исправлен
с clarification на plan до финального frozen digest. Финальный run на одном
commit-кандидате с real-home acceptance:

| Метрика | Natural 100 | Gate |
|---|---:|---:|
| PASS | 100/100 | 100/100 |
| `WRONG_TARGET` | 0 | 0 |
| `CROSS_ROOM_TARGET` | 0 | 0 |
| `AMBIGUOUS_PLAN` | 0 | 0 |
| `FORBIDDEN_PLAN` | 0 | 0 |
| `MISSED_EXPECTED_PLAN` | 0 | 0 |
| `FALSE_ACTION_INTENT` | 0 | 0 |
| Wrong action | 0 | 0 |
| `HA_POST` | 0 | 0 |
| HA reads/service paths/service calls | 0/0/0 | 0/0/0 |
| `MODEL_CALLS` | 6 | informational |
| `DETERMINISTIC_RESOLUTIONS` | 71 | computed |
| `MODEL_ASSISTED_RESOLUTIONS` | 5 | computed |
| Model prompt technical-ID leaks | 0 | 0 |
| P50/P95/P99 | 0.0068/2.1278/2.4946 s | P95 ≤ 2.5 s |

`MODEL_CALLS` не является gate. Один model call безопасно закончился без plan;
пять дали model-assisted intent, после чего target всё равно доказал host.

## Independent real-home acceptance

Expected targets читаются только из owner manifest и никогда не вычисляются
production resolver-ом. Текущий metadata-only HomeGraph реального дома
используется только actual production path. Manifest SHA-256:
`fed9fdf1d0a21fea6e0a5f4f4813015836c5ecbc1d88832098db42ab983d00fa`.

| Метрика | Real home 60 | Gate |
|---|---:|---:|
| PASS | 60/60 | 60/60 |
| `WRONG_TARGET` | 0 | 0 |
| `CROSS_ROOM_TARGET` | 0 | 0 |
| `AMBIGUOUS_PLAN` | 0 | 0 |
| `FORBIDDEN_PLAN` | 0 | 0 |
| `MISSED_EXPECTED_PLAN` | 0 | 0 |
| `FALSE_ACTION_INTENT` | 0 | 0 |
| Wrong action | 0 | 0 |
| `HA_POST` | 0 | 0 |
| HA reads/service paths/service calls | 0/0/0 | 0/0/0 |
| `MODEL_CALLS` | 0 | informational |
| `DETERMINISTIC_RESOLUTIONS` | 24 | computed |
| `MODEL_ASSISTED_RESOLUTIONS` | 0 | computed |
| P50/P95/P99 | 0.0079/0.1718/0.1892 s | P95 ≤ 2.5 s |

E12/E20 теперь strong deterministic plans. M03/M04, два зеркала и остальные
реальные ambiguities остаются clarification. Physical HA POST boundary и
snapshot reader были fail-closed на протяжении обеих acceptance.

## Проверка и итог

- 1,000-command Stage 72 corpus остаётся green;
- new contract/unit tests проверяют strong bypass, ambiguous no-model,
  structured model boundary, weak margin и ephemeral follow-up;
- repository suite: 68/68 PASS в Windows и 68/68 PASS в WSL;
- production executor/control adapter отсутствует.

Все correction gates зелёные. Deployment не выполнялся. Stage 73 не начат.
