# Stage 72 — Real-home Room/Type Capability Closeout

Статус: `GREEN_SHADOW_NOT_DEPLOYED`. Stage 73 не начат. Production services не
активировались и не перезапускались. Home Assistant POST, `/api/services`,
service calls и HA reads внутри shadow planning не выполнялись.

## Что изменено

Persistent HomeGraph остаётся единственным и metadata-only. Внутри единственного
host resolver добавлена turn-local action-проекция enabled `light`/`switch`
entities. Она сохраняет strong parent identity и позволяет отличать реальные
каналы многоканального physical device; это не второй граф и не executor.

Порядок безопасности сохранён:

1. exact physical name/alias сначала сохраняет physical ambiguity;
2. unique entity name/alias или unique area+type даёт strong host decision;
3. равные physical/entity candidates всегда дают clarification;
4. weak/fuzzy result требует прежней повторной host-проверки;
5. dangerous physical target hard-deny, даже если у него есть switch entity.

Area берётся из registry metadata. Если registry area отсутствует, resolver
может вывести только одну комнату из human names/aliases и общей русской
room-онтологии. Если metadata называет две комнаты, area не выводится и target
не становится strong. Имена/модели/комнаты не используются для объединения
разных physical identities.

## Independent manifest

Frozen manifest:
`tests/data/stage72_real_home_room_type_owner_reviewed.jsonl`.

- SHA-256: `4182f1a75a494effc607440810b5c05c218e8b8822df3a4e9e62fd1b58088b2d`;
- 42 строки: 30 expected plans по 10 реальным actionable targets, ровно три
  естественные формулировки на target, плюс 12 обязательных ambiguity cases;
- expected target/area/domain/action записаны в manifest вручную после прямого
  metadata-only аудита и не вычисляются production resolver;
- runner импортирует реальный `owner_chat`/bounded path через базовый live
  harness, использует текущий real HomeGraph и блокирует HA POST/service paths.

Проверенные plan targets распределены по кабинету, кухне, гардеробу и коридору:
`Свет`, `кабинет Switch 1`, `кабинет Switch 2`, `кухня Switch 1`,
`кухня кузня`, `Вытяжка на кухне`, `гардероб Switch 2`,
`гардероб Выключатель 2`, `коридор`, `кавидор коридор`.

`REAL_ROOM_TYPE_PLANS` считается только если actual IntentFrame одновременно
содержит непустые requested area и requested type, actual outcome — plan и
строка прошла expected target/domain/action. Это не число строк категории.

## Конфликты, которые нельзя безопасно объединить

- `свет в ванной`: два разных physical registry targets — `туалет прихожка`
  (`localtuya`, модель W-W602) и `Ванная туале` (`tuya_local`, модель не
  указана). У обоих есть отдельные `Ванная`/`Туалет` switch channels, HA area
  не назначена. Stable identity общая не доказана — clarification.
- `лампа в кабинете`: два physical `кабинет`, один `tuya_local` с channels
  `Свет`/`лампа`, второй `localtuya` W-W602 с `Лампа`/`кабинет`.
  Exact subtype `свет` уникален, subtype `лампа` — нет.
- `зеркало в ванной`: два physical `зеркало`, `tuya_local` и `tuya` WiFi
  Breaker T, без HA area и общей strong identity — clarification.
- `реле вентилятора в ванной`: два physical `реле вентилятора`,
  `tuya_local` и `tuya` WiFi Socket, без HA area и общей strong identity —
  clarification.
- `ночник в коридоре`: один physical `ночник`, но три actionable channels
  (`light` подсветки, одноимённые `switch` и `light`) и нет area evidence —
  clarification.

## Финальный real-home результат

| Метрика | Результат | Gate |
|---|---:|---:|
| PASS | 42/42 | 42/42 |
| `REAL_TARGETS_TESTED` | 10 | ≥10 |
| `REAL_ROOM_TYPE_PLANS` | 21 | ≥20 |
| `REAL_CLARIFICATIONS` | 12 | informational |
| `REAL_NO_PLANS` | 0 | informational |
| `WRONG_TARGET` | 0 | 0 |
| `CROSS_ROOM_TARGET` | 0 | 0 |
| `MISSED_EXPECTED_PLAN` | 0 | 0 |
| `WRONG_ACTION` | 0 | 0 |
| `AMBIGUOUS_PLAN` | 0 | 0 |
| `FORBIDDEN_PLAN` | 0 | 0 |
| `FALSE_ACTION_INTENT` | 0 | 0 |
| `HA_POST` / service paths / service calls | 0 / 0 / 0 | 0 / 0 / 0 |
| HA reads during planning | 0 | 0 |
| Model calls | 0 | informational |
| Deterministic resolutions | 30 | computed |
| P50 / P95 / P99 | 1.4715 / 1.6093 / 1.6390 s | P95 ≤2.5 s |

Qwen не вызывался искусственно: все 42 фразы этого closeout прошли штатный
deterministic parser. Отдельный frozen blind natural-language corpus прошёл
100/100 с 6 настоящими вызовами `qwen3.5:2b-q4_K_M`, 71 deterministic и 5
model-assisted resolutions; P50/P95/P99 = 0.0187/2.1406/2.4863 s, все safety
счётчики 0. Фактически загруженная модель: digest
`124a03c347777e8e4e5955c33610ae01d9d90d8c2a718bfba069c498d5c7f3c9`,
context 8192, `100% CPU`; GPU offload не заявляется.

## Проверки и границы

- repository suite: 71/71 PASS;
- Python compile и diff whitespace checks: PASS;
- production executor/control adapter не добавлен;
- manifest/runner contract отдельно доказывает frozen digest, 10 targets,
  30 expected plans, independent expected side и вычисляемый room/type count;
- предварительный выбор голого target `кухня` был отброшен: три physical nodes
  имеют это exact name, поэтому правильный outcome — clarification, а не plan;
- старый 60-case manifest больше не является closeout gate: его `R02` и `R15`
  намеренно ожидали `no_plan` для теперь доказанных `Свет` в кабинете и
  `Вытяжка на кухне`.

Stage 72 capability closeout завершён только в shadow. Deployment не выполнялся.
