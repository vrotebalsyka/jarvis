# Current Goal — Stage 73 Phase A

## Owner pause request — 2026-09-10

Владелец запросил сохранить весь проект на GitHub, затем полностью остановить
Jarvis/Ollama и запретить автозапуск. Это не Stage73 completion и не Phase B.
Рабочий снимок: ветка `stage73-canary-live-control`, code `5b35903`, evidence
`4d6e0e8`. Для продолжения клонировать именно эту ветку, не main:

```bash
git clone --branch stage73-canary-live-control https://github.com/vrotebalsyka/jarvis.git
```

В Git сохранены source, tests, frozen manifests, config templates и reports.
Tokens, private inventory, owner keys и веса Ollama намеренно не публикуются.
Они остаются на текущем компьютере; для другого компьютера секреты необходимо
передать отдельно безопасным способом либо перевыпустить. Модель и digest
зафиксированы в STAGE-73-RESULT.md. Никакие ignored secrets не добавлять в Git.

План остановки: отключить пять Windows задач Home Butler и остановить их;
остановить/disable/mask 12 точных systemd units Home Butler/Ollama, сохранив
локальные unit definitions в root-only `/var/lib/home-butler/owner-pause-20260910/units`.
Не останавливать HA, не переключать устройства и не выключать весь WSL,
поскольку в нём могут работать другие проекты.

**Итог: OWNER_PAUSED, остановка подтверждена 2026-09-10.**
Все 12 units `masked`, неактивны; все 5 Windows tasks `Disabled` после UAC.
Ollama/llama processes отсутствуют, MainPID=0 у Ollama/Web/Alice/inventory;
порты 11434/8780/8765 не слушаются. Дополнительных Python workers Jarvis
не обнаружено. Windows Ollama processes и matching Run entries не обнаружены.
12 исходных unit definitions сохранены в указанном root-only backup; definitions
не удалены. Ошибочный inventory failed-state очищен, service не запускался.
Код и модельные веса не удалялись, HA не затрагивался, service calls=0.
WSL и общий Tailscale daemon не выключались, чтобы не затронуть другие проекты;
отсутствие процессов модели не означает, что сама VM WSL не использует память.
Runtime chat/Alice теперь намеренно offline, прежние health PASS исторические.
Операционные scripts сохранены в reports/pause-local-runtime-2026-09-10.sh и
reports/pause-windows-tasks-2026-09-10.ps1; Linux script одноразовый и повторно
на masked units не запускается. Никакие скрипты восстановления автоматически
не выполняются. Stage73 остаётся NOT_READY, а не COMPLETE.

Возобновление допускается только по новому решению владельца: сначала review
CURRENT-GOAL/STAGE-73-RESULT и сохранённых failures, затем восстановить unit
definitions из локального backup, снять masks и включить только необходимые
units/tasks. Не запускать installer --activate автоматически и не включать
CONTROL_ENABLED/CANARY_LIVE_ENABLED. Phase B по-прежнему требует отдельного approval.

Статус Phase A: `FAIL / NOT_READY`.
Live status: `INCOMPLETE_LIVE_APPROVAL_REQUIRED`. Stage74 не начат.
Ветка `stage73-canary-live-control`; base и последний проверенный main:
`8bc3b480a9fa2414dfe8680233db5b23ee0b54fe`.
Владелец 2026-09-09 отдельно запросил сохранение и GitHub-публикацию текущей
незавершённой работы и отчётов. Это не green gate, promotion или Phase B approval.
Сводка и разбор четырёх скриншотов: reports/WORK-REPORT-2026-09-09.md.
Первоначальная публикация выполнена и remote SHA проверен: stage73-canary-live-control =
`284b99a4619ec4153cb0b7c5a4381b07cec1b7bc`. GitHub main остался
`8bc3b480a9fa2414dfe8680233db5b23ee0b54fe`. Прежний approval blocker снялся;
публикация не означает deploy или прохождение Phase A.

## Последнее изменение: nullable registry area, 2026-09-09

Владелец отдельно разрешил хранить `registry_area=null`. Code commit:
`5b359032ab80c97b3417a6b01cfca16684ca63bb` (parent `aded689`).
Контракт принимает только explicit absence с отдельной `owner_area`; missing
metadata не становится null. Fresh назначение комнаты инвалидирует прежний plan.
Registry не менялся, allowlist не участвует в resolver и не снимает ambiguity.
Две provenance запечатаны в plan, независимый oracle проверяет raw registry nulls.

- Repository 159/159 PASS; новые nullable tests 14/14; fake security 49/49.
- Fresh Stage71 live/oracle PASS, wrong/invented/lost=0, 215/215 represented.
- Fresh Stage72 natural 100/100; room/type 42/42. Прежний N04 timeout сохранён
  в историческом evidence, его первопричина не объявлена исправленной.
- Nullable diagnostic: 5/5 owner-selected private bindings валидны; 4 sealed
  plans + `not_sent`, relay clarification. P95 2.7837 s — не latency PASS.
  Private allowlist только в памяти; installed records=0, оба flags OFF.
- Stage73 shadow повторно 170/205: 35 настоящих relay clarifications против
  старых expected plans. Frozen expectations не переписаны.
- Owner draft повторно 24/25: R20 «включи вытяжку» остаётся clarification.
  Все wrong-target/room/ambiguous-plan/forbidden-plan/false-action counters=0.
  Fresh-suite network: 68 GET / 13 registry reads, никаких POST/service calls.

Null-contract blocker снят, но Phase A остаётся FAIL / NOT_READY: нет полностью
reviewed и green canary corpus. Phase B не разрешена; HA_POST=SERVICE_CALLS=0,
live cycles=0, control/config/action credential не установлены. Main не меняется.
Полное evidence и последние percentiles: STAGE-73-RESULT.md, nullable checkpoint.

## Предыдущая свежая проверка 2026-09-09, 10:05–10:14 UTC

HA снова доступен: TCP из Windows/WSL, authenticated GET и три registry-list
команды прошли. Ничего в HA/registry/сети не менялось и не перезапускалось.
Причина прежнего timeout не установлена: доступ восстановился без наших изменений.
Проверен source commit `284b99a4619ec4153cb0b7c5a4381b07cec1b7bc`,
source digest до/после совпал. Production не переустанавливался.

- Stage71 live/oracle PASS: wrong/invented/lost=0, 30 physical + 10 logical,
  215/215 enabled current entities представлены, 2 skips; P95 0.8631 s.
- Stage72 natural **99/100**, N04 error через 10.016 s; model calls=5.
  Диагностический повтор только N04 позднее дал правильный plan за 2.4361 s;
  он не заменяет полный результат и не доказывает устранение причины задержки.
- Stage72 room/type **42/42**, 21 room/type plans, P95 1.6114 s.
- Stage73 fresh shadow **170/205**, 35 relay clarifications против прежних
  expected plans; P95 2.0958 s. Frozen expectations не менялись.
- Owner-review fresh **24/25**, 17 plans + 8 clarifications, R20 unchanged.
  HTML обновлён на свежий источник; owner-reviewed/live-authorized остаются false.
- Repository suite **145/145 PASS**, 52.5796 s, skips=0; actual HA network attempts=0.
- Fake endpoint/security **49/49 PASS**, 18.151 s: fake POST=27, fake GET=99,
  реальные HA_POST=SERVICE_CALLS=0. Это не live devices/receipts.

Все measured wrong-target/room/action/ambiguous-plan/forbidden-plan/false-action
counters = 0. Combined instrumented network: HA_GET=68, REGISTRY_READS=13,
HA_POST=SERVICE_CALLS=BLOCKED=0. Отдельный N04 повтор: HA requests=0.
Allowlist records=0, flags OFF, registry bindings отсутствуют у пяти canaries,
sealed canary plans=0. Phase A по-прежнему FAIL / NOT_READY, Phase B не разрешена.
Evidence: `reports/stage73-fresh-recheck-2026-09-09-restored.json` и связанные
per-suite JSON. Следующие разделы — исторические checkpoints, не текущий HA health.

## История до свежего повторения

Владелец выбрал для подготовки пять targets: switch «Свет» (physical «кабинет»),
switch «коридор», light «ночник Подсветка», switch «Вытяжка на кухне» и switch
«реле вентилятора». Это не Phase B approval. Allowlist records/action credential
не установлены, оба flags OFF. Canary runtime отсутствует. Stage73 deploy
не выполнялся. 2026-09-09 по отдельному разрешению владельца установлен только
CSRF-hotfix Stage72 local_chat_gateway.py (две замены selector, 281→281 строк)
и перезапущен только home-butler-local-chat.service. Остальные 14 проверенных
runtime scripts не изменились; Alice PID/active timestamp не изменились.
CSRF checkpoint: Stage72 base + этот uncommitted hotfix, не byte-identical main.
Repository suite на этом checkpoint: 138/138 PASS (55.342 s); staged-runtime CSRF tests:
4/4 PASS. Browser /help: HTTP 200, правильный CSRF. Два «привет»: HTTP 503;
диагностическое воспроизведение в mount namespace сервиса дошло до
validate_owner_answer и отклонило non-BMP pictograph (не CSRF и не HA timeout).
Это был отдельный backend defect; CSRF-hotfix фильтры ответа модели не менял.
Evidence: reports/local-chat-csrf-hotfix-2026-09-09.json. HA control не включался.

Следующее отдельное разрешение владельца 2026-09-09: исправлена только
validate_owner_answer в production bounded_ha_agent.py (1137→1143 строк).
Безопасный Unicode сохраняется; прежние secret/technical-ID regex проверяют
исходный текст и дополнительную нормализованную копию без Unicode-маскировки.
Невалидные суррогаты и управляющие символы отклоняются. Рестарт только local chat;
Alice не перезапущена (её загруженный код этим не обновлён), модель/HA не менялись.
Runtime — Stage72 base + CSRF и Unicode hotfix, отдельно от полной Stage73 ветки.
Repository: 145/145 PASS (55.977 s); staged-runtime compatibility: 14/14 PASS.
Browser: «привет» HTTP 200; «спасибо» HTTP 200 с реальным ответом модели «🤗».
Отдельная просьба «Поздоровайся и добавь эмодзи машущей руки.» получила
clarification, не conversation: этот parser limitation зафиксирован, не исправлен
и не посчитан semantic PASS. Phase B не начата; canary runtime отсутствует.
Evidence: reports/local-chat-unicode-hotfix-2026-09-09.json.

Owner clarification 2026-09-08: ночник и реле вентилятора находятся в Кабинете,
вытяжка — на Кухне; выбран именно tuya_local relay. Владелец подтвердил,
что вытяжку и вентилятор безопасно выключать и включать. Это подтверждение
назначения/безопасности targets, не запуск Phase B до green Phase A.
Повторный fresh read нашёл ровно по одному enabled exact entity+physical-name+
domain+tuya_local binding для каждого из пяти targets. Registry area всё ещё
отсутствует у всех пяти: owner-confirmed room не подставляется как HA fact.
Два relay physical nodes не объединяются, allowlist не используется для
disambiguation. Нужны registry bindings и отдельный review live canary command
manifest; frozen expectations не менялись. HA registry не изменялся.
Проверка уточнения: HA_GET=1, REGISTRY_READS=3, HA_POST=SERVICE_CALLS=BLOCKED=0.
Владелец явно ОТКАЗАЛ в изменении registry rooms ради тестов. HA registry не
изменять, не повторять запрос на такое изменение как условие PASS. Его сведения
о комнате не подставлять в registry fact. Phase B и реальные HA POST запрещены.

Stage73 checkpoint до transport hotfix: repository 134/134 PASS (51.789 s); canary/security 49/49 и
prepared-runner fake tests 5/5 PASS. Live runner default STOP, не запущен; engine проверен
только на fake HA. Добавлен GET-only result event, session-local для Web/Alice.
Текущая Stage71 live/oracle попытка ERROR / HA unavailable (InventoryError).
Независимые HA GET и registry-connect probes также TimeoutError (5/10 s).
Предыдущий Stage71 PASS (до последней discourse correction): wrong/invented/lost=0,
30 physical+10 logical, 215/215 enabled represented, 2 skips; это не текущий gate.
Stage72 natural вновь 100/100, room/type snapshot 42/42; P95=.1500/1.6728 s.
Промежуточный A03 AMBIGUOUS_PLAN=1 сохранён в evidence и исправлен generic
qualified-name ambiguity contract; expectations не менялись.
Actual HA_POST=0, SERVICE_CALLS=0, сетевые перехватчики блокируют writes.

Frozen unreviewed Stage73 draft: 205 raw / 183 unique phrases, 5 targets.
Initial 113/205 с false action intent=2; intermediate 158/205 с wrong action=1.
Воспроизведённые generic defects исправлены без house-specific rules.
Последний fresh replay до discourse correction: 160/205, 45 missed plans.
Последние шесть исходных weak-evidence cases прошли targeted snapshot 6/6.
Исправлены adjacent transposition, проверка полного physical name без потери
entity channel, desired-state discourse. Реальные одинаковые physical names
остаются clarification. Итоговый full snapshot: 170/205; 35 remaining cases —
relay ambiguity, не wrong target; P50/P95/P99=1.5402/2.1397/2.4466 s.
Snapshot не является fresh HA acceptance. HA_GET=REGISTRY_READS=0.
Expectations не изменены; owner_reviewed=0, sealed canary plans=0.
Gate ≥200 raw canary shadow commands PASS не достигнут. Это не green Phase A.

Новый frozen owner-review draft: 25 обычных фраз только для выбранных пяти
canaries, `tests/data/stage73_owner_review_25.jsonl`; human table
`reports/STAGE73-OWNER-REVIEW.html`. Owner approval=false, live approval=false.
Итог: 17 plans + 8 clarifications; 24/25 совпадений с исходным draft, R20
оставлен расхождением (правильное clarification), а не изменён после ответа.
Короткая «вытяжка» также имеет конкурирующий physical target: clarification
правилен; ошибочное draft expected plan сохранено, не переписано после ответа.

Диагностика: TCP к HA-порту timeout и из Windows, и из WSL; HA address отвечает
на ICMP (15 ms), LAN gateway (0 ms), Ethernet route в той же подсети.
Сбой до HTTP/auth/registry commands; DNS/token/resolver не являются причиной
этого TCP timeout. Listener/firewall/identity отвечающего host не различимы
без HA console logs или проверки другим LAN client. Ничего не перезапускалось.
Измерения: reports/stage73-ha-connectivity-2026-09-08.json.

Предыдущие local chat и локальные Alice/model/HA-read probes PASS; публичная Alice probe
FAIL (SSLEOFError), несмотря на active services и валидную Funnel configuration.
Позднее HA read стал недоступен; старый health PASS не выдаётся за текущий.
Сеть и сервисы не менялись. Подробные traces/metrics/limitations:
`STAGE-73-RESULT.md`. Live cycles=0, Phase B запрещена без отдельного разрешения.

## Историческое завершение Stage 72

Статус завершённого cleanup: `STAGE72_READ_CLEANUP_GATE_GREEN`.

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
