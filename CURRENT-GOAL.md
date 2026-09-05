# Current Goal — Stage 72

Статус: `STAGE72_READ_CLEANUP_GATE_GREEN`.

Promotion baseline: `main = 7eb0a9fd8b03cf481e58aff06b78830b6658a868`.
Safety tag: `stage72-complete-7eb0a9f`. Этот commit установлен и активирован
в production в SHADOW mode 2026-09-05 по разрешению владельца. Executor/control
отсутствуют, `HA_POST=0`, `SERVICE_CALLS=0`. Stage 73 не начат.

Владелец расширил cleanup на generic read-resolver и independent oracle.
Tracked Markdown contract, устаревший Stage 71 selector benchmark и
документация также входят в cleanup. Новых возможностей и control нет.
Live read/oracle проверки Stage 71 сохранены через `agent.process_turn`;
отдельный benchmark удалённого selector больше не соответствует production.
Архитектура Stage 72 сохраняется. Commit/push разрешены только после полного
green gate; Stage 73 не начинать.

## Зафиксированный разбор до исправления read semantics

Ожидания frozen manifests не меняются. SHA256 Stage 71 blind corpus:
`4ff8119abb39dd0e33b17c239699f8df15f372f414aa743d256c234eab602376`;
Stage 72 natural: `c7481eab286f10adb4abde4c8ea7577c2053c81dadf60ade03be7ca51896524a`;
Stage 72 room/type: `4182f1a75a494effc607440810b5c05c218e8b8822df3a4e9e62fd1b58088b2d`.

| Запрос | WRONG_TARGET | INVENTED_FACTS | LOST_REQUESTED_VALUES | Причина |
| --- | ---: | ---: | ---: | --- |
| Какой статус у устройств кабинета? | 5 | 0 | 0 | Room+status вытесняет имена; ожидаются 3 physical representations «кабинет» |
| Покажи питание в кабинете | 1 | 0 | 0 | К 3 representations добавлен посторонний target |
| Какой статус у устройств кухни? | 8 | 0 | 0 | Вместо 3 representations «кухня» предложены 5 иных targets |
| Покажи питание на кухне | 2 | 0 | 0 | К 3 representations добавлены 2 иных targets |
| Проверь состояние коридора | 1 | 0 | 1 | Указанное имя вытеснено room-wide clarification |
| Какое питание в коридоре? | 1 | 0 | 1 | Указанное имя вытеснено room-wide clarification |
| Какое состояние гардероба? | 5 | 0 | 0 | Вместо 3 physical representations предложены 2 logical targets |
| Какой статус у вытяжки на кухне? | 1 | 0 | 3 | Room+status срабатывает до разбора склонённого имени |
| Покажи питание вытяжки на кухне | 1 | 0 | 2 | Room+power срабатывает до разбора склонённого имени |
| Покажи статус кухня Switch 1 | 0 | 1 | 1 | registry areas=[] подменено inferred «Кухня» |
| Покажи статус коридор | 0 | 2 | 2 | Два receipts: registry areas=[] подменено inferred «Коридор» |
| Покажи статус Свет | 0 | 1 | 1 | registry areas=[] подменено inferred «Кабинет» |
| Покажи статус пылесос уберет кабинет | 0 | 1 | 1 | registry areas=[] подменено inferred «Кабинет» |
| Покажи статус Гардероб реле 2 включено датчиком | 0 | 1 | 1 | registry areas=[] подменено inferred «Гардероб» |
| **Всего** | **25** | **6** | **13** | |

Контракт областей, зафиксированный до изменения production:

- Registry area — только существующий HA registry binding (`area_refs`).
- Inferred area — отдельная metadata-гипотеза, вычисленная из human names и
  aliases при единственном room concept и отсутствии registry area. Она не
  записывается в persistent HomeGraph и не заменяет registry binding.
- Resolver context явно разделяет `registry_areas` и `inferred_areas`.
  Effective area для shadow targeting допускается как прежде; это не HA-факт.
- `ReadReceipt.areas` содержит только registry areas; inferred areas остаются
  в resolver context и не попадают в receipt. Independent oracle получает
  registry areas непосредственно из графа; его ожидаемые HA areas не меняются.
- `status`/`power`/`unknown` сами по себе не являются device type. Room-only
  совпадение не должно вытеснять имена/aliases и подменять полноценное area+type.
  Quantitative feature+area reads (например, температура) остаются доступны.

## Первоначальная post-promotion acceptance — до расширения cleanup

- Repository unittest suite: **71/71 PASS**, 28.044 s. Closed-set Markdown
  assertion сохранён; добавлен ровно существующий room/type report.
- Operational read smoke: штатная Alice health-проверка PASS для
  `local_gateway`, `local_model`, `ha_read`, `public_gateway`. Local chat HTTP
  200 отвечает на вопрос о заряде Андрея; это проверка доступности read path,
  не замена независимой семантической приёмки.
- Stage 71 live read/oracle: **FAIL**, воспроизведён дважды на установленном
  runtime. `wrong_target=25`, `invented_facts=6`, `lost_requested_values=13`.
  40 frozen blind utterances, 79 turns; прочитаны 30 physical и 10 logical
  targets; 215/215 enabled current entities представлены в metadata-only
  graph (257 entities, 51 physical nodes, 37 logical nodes). `failures=0`,
  `skips=8`, persistent current values=0, model-generated IDs=0.
  P50/P95/P99 основного прогона: 0.6561/0.7986/1.0738 s; прежний read P95
  limit 1.5 s сохранён. В каждом из двух прогонов перехватчик измерил
  63 HA GET и 4 registry reads, `HA_POST=0`, `SERVICE_CALLS=0`, blocked writes=0.
- Stage 72 natural-language: **100/100 PASS**, все target/policy gates=0,
  `HA_POST=0`, `SERVICE_CALLS=0`; 6 model calls, 71 deterministic и 5 assisted
  resolutions. P50/P95/P99: 0.0174/2.0651/2.3362 s.
- Stage 72 real-home room/type: **42/42 PASS**, все target/policy gates=0,
  `HA_POST=0`, `SERVICE_CALLS=0`; 10 targets, 30 plans,
  `REAL_ROOM_TYPE_PLANS=21`, 12 clarifications, 0 no-plans, 0 model calls.
  P50/P95/P99: 1.2717/1.5791/1.6684 s.
- Runtime: 15 scripts и 14 config/SOUL/systemd artifacts побайтово совпадают
  с GitHub main `7eb0a9fd8b03cf481e58aff06b78830b6658a868`. Executor/control
  отсутствуют. Local chat, Alice skill/tunnel активны; Alice health healthy.

Установленные причины Stage 71 FAIL:

- Read resolver для «Какой статус у вытяжки на кухне?» и «Покажи питание
  вытяжки на кухне» возвращает room-wide clarification, включая посторонние
  устройства кухни, вместо чтения указанной вытяжки. Аналогичные расхождения
  затрагивают запросы о кабинете, коридоре, кухне и гардеробе. Счётчик
  `wrong_target` включает несовпавшие clarification sets и отсутствующие
  ожидаемые selections; число 25 не означает 25 выполненных неверных действий.
- Все 6 `invented_facts` и 6 из 13 `lost_requested_values` вызваны только
  несовпадением `ReadReceipt.areas`: production выводит комнату из human
  metadata, а независимый oracle ожидает только registry area bindings.
  Отдельная проверка пяти реплик показала совпадение всех остальных полей,
  включая значения и timestamps. Это не доказательство выдуманных значений
  датчиков, но существующий oracle gate остаётся красным.

На этом первоначальном прогоне read expectations и oracle не ослаблялись.
Работа была остановлена до разрешения владельца расширить cleanup; commit/push
не выполнялись. Разрешение на generic read-resolver и independent oracle
получено позже; контракт и разбор всех исходных failures приведены выше.

## Изменения expanded cleanup

- Сохранён закрытый набор tracked Markdown; добавлен ровно существующий report.
- Удалён только устаревший selector benchmark, вызывавший отсутствующий private
  API. Все Stage 71 live reads выполняются через `agent.process_turn`.
- Read resolver не считает generic status/power/unknown типом устройства для
  room-wide разрешения цели. Это сохраняет приоритет human name evidence.
- `registry_areas` и `inferred_areas` разделены в profiles/context; ReadReceipt
  сохраняет только registry areas. Исполняемые ожидания oracle не менялись
  (в oracle добавлен только поясняющий комментарий).
- Новые generic fixtures воспроизвели failures до исправления. Mutation tests
  проверяют, что oracle отвергает и подмену registry area inferred-комнатой,
  и потерю настоящей registry area. Frozen manifests не редактировались.
- При проверке обнаружен промежуточный action regression D04 (99/100): read
  ограничение затронуло уже отфильтрованную light/switch projection. Он исправлен
  ограничением новой проверки read-кандидатами. Добавлен generic regression
  для implicit-type shadow action; ожидаемый D04 plan не изменён.

## Окончательная acceptance cleanup tree — 2026-09-05

| Проверка | Результат | P50 / P95 / P99, s |
| --- | --- | --- |
| Полный repository unittest suite | 79/79 PASS; 27.258 s | — |
| Stage 71 live/oracle | PASS; 40 frozen blind utterances, 76 turns | 0.6160 / 0.7056 / 1.0490 |
| Stage 72 natural-language | 100/100 PASS | 0.0181 / 2.1045 / 2.3021 |
| Stage 72 real-home room/type | 42/42 PASS | 1.2548 / 1.6320 / 1.6557 |

- Все исходные 14 запросов исправлены: 9 входят в неизменённые 40 blind
  utterances, остальные 5 повторены отдельным fresh-read replay с независимыми
  metadata targets и oracle. `WRONG_TARGET=0`, `INVENTED_FACTS=0`,
  `LOST_REQUESTED_VALUES=0`, `CROSS_ROOM_TARGET=0`.
- Stage 71: покрытие 215/215 enabled current entities; прочитаны 30 physical
  и 10 logical targets; `failures=0`, `skips=2`. 257 metadata entities,
  51 physical nodes, 37 logical nodes, 8 areas и 28 integration nodes.
  Persistent current values=0, model-generated IDs=0.
- Сетевой перехватчик финального Stage 71 прогона: 64 HA GET, 4 registry reads,
  `HA_POST=0`, `SERVICE_CALLS=0`, blocked writes=0. Дополнительный replay пяти
  provenance cases: 5 GET, те же нулевые write/oracle counters.
- Stage 72 natural: 6 настоящих model calls, 71 deterministic и 5 assisted
  resolutions. Room/type: 10 real targets, 30 plans,
  `REAL_ROOM_TYPE_PLANS=21`, 12 clarifications, 0 no-plans, 0 model calls.
  Все target/policy/network gates=0. Физическая блокировка HA POST сохранена.
- SHA256 всех frozen manifests совпадают с указанными выше до исправления.
  Oracle не импортирует production resolver/renderer; код вычисления
  ожидаемых receipts и метрик не ослаблялся.
- Исправление не содержит executor, control, HA service paths или нового
  функционала Stage 73. После green разрешён commit/push cleanup в main;
  production должен быть синхронизирован с этим main в SHADOW mode и проверен
  по closed runtime artifacts и operational health.

## История до cleanup

Реализованные возможности и исторические результаты до promotion:

- IntentFrame action/value/scope;
- один deny-by-default ActionPolicyRegistry;
- sealed non-executable ActionPlan только для light/switch turn_on/turn_off;
- hard-deny vacuum/button/appliance/lock/climate/script и unsupported actions;
- strong unique host decision, unconditional ambiguous clarification и
  revalidated weak/fuzzy evidence;
- единый structured IntentFrame parser с deterministic fast path и bounded
  Qwen fallback без candidates/entity/device/service/capability IDs;
- machine-readable traces с `service_calls=0`, `ha_post=0`;
- instrumented physical HA POST/service-path block;
- 1,000-command required corpus и owner blind 40/40;
- production parser/resolver/model live run n=30, failures 0,
  P50/P95/P99 1.6891/1.8405/1.9139 s;
- все target/policy gates 0, `HA_POST=0`, production executor отсутствует;
- independent real-home manifest 60/60, P50/P95/P99
  0.0079/0.1718/0.1892 s, все gates 0;
- новый blind natural-language corpus 100/100, 6 model calls,
  71 deterministic и 5 model-assisted resolutions, P50/P95/P99
  0.0068/2.1278/2.4946 s, все gates 0;
- post-promotion repository suite выявил 70/71: существующий room/type report
  отсутствовал в closed-set Markdown contract; этот cleanup добавляет ровно его.
- independent real-home room/type closeout 42/42: 10 real actionable targets,
  30 expected plans, `REAL_ROOM_TYPE_PLANS=21`, 12 safe clarifications,
  P50/P95/P99 1.4715/1.6093/1.6390 s и все safety/network gates 0;
- action resolution использует turn-local light/switch entity projection
  внутри единственного HomeGraph; exact physical ambiguity сохраняется.

Evidence report:
[`reports/STAGE-72-SHADOW-ACTION-PLANNING-2026-09-03.md`](reports/STAGE-72-SHADOW-ACTION-PLANNING-2026-09-03.md).

Историческая pre-correction real-home приёмка 56/60 сохранена в отчёте:
[`reports/STAGE-72-FINAL-REAL-HOME-ACCEPTANCE-2026-09-03.md`](reports/STAGE-72-FINAL-REAL-HOME-ACCEPTANCE-2026-09-03.md).
Correction evidence:
[`reports/STAGE-72-CORRECTION-2026-09-04.md`](reports/STAGE-72-CORRECTION-2026-09-04.md).
Room/type closeout evidence:
[`reports/STAGE-72-ROOM-TYPE-CLOSEOUT-2026-09-04.md`](reports/STAGE-72-ROOM-TYPE-CLOSEOUT-2026-09-04.md).
