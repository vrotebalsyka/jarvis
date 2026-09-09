# Stage 73 — Phase A, без live approval

Сводный отчёт владельцу, включая четыре скриншота 9 сентября и фактические
ограничения публикации: [WORK-REPORT-2026-09-09](reports/WORK-REPORT-2026-09-09.md).
Промежуточная работа опубликована по прямому запросу владельца, не как
Stage73 COMPLETE. Remote SHA первоначальной публикации кода подтверждён:
`284b99a4619ec4153cb0b7c5a4381b07cec1b7bc`; main не изменён.

## Nullable registry area — разрешённое изменение Phase A, 9 сентября

Code commit: `5b359032ab80c97b3417a6b01cfca16684ca63bb`.
Parent: `aded6891032e81112ea9e8ce5d3320c15f803e8b`; Stage72 base/main:
`8bc3b480a9fa2414dfe8680233db5b23ee0b54fe`.
Scope: разрешение владельца «разрешить хранить registry_area=null», не Phase B.
Исходники проверялись перед commit; точный digest scripts/tests до/после:
`75c5d8fbf52d86eff26d95d7cc50af81541d885cb6465b1a925f709eaa17d66b`.
HEAD в fresh-run evidence отражает исходный parent; digest соответствует code commit выше.

Раньше canary authority требовала non-null registry room. Теперь разрешена
закрытая пара `registry_area=null` / `registry_area_ref=null` с обязательным
отдельным `owner_area`. Bound records требуют `owner_area=null`. Это exact
absence, не wildcard: missing fields, dangling area reference и неуспешное
чтение отклоняются. Единственный HomeGraph хранит только дополнительный bool
metadata `registry_area_unassigned`. Owner room не записывается в HA/HomeGraph
или ReadReceipt, не участвует в resolver и не устраняет ambiguity.
Plan запечатывает registry/owner provenance; fresh binding даже в той же комнате
инвалидирует plan. Prepared runner/oracle проверяют тот же контракт независимо;
expected room берётся из заранее reviewed manifest, не из ответа production.

Production source changes, lines before/after:

| File | Before / after |
| --- | --- |
| scripts/canary_contract.py | 294 / 322 |
| scripts/canary_write_adapter.py | 340 / 341 |
| scripts/home_assistant_inventory.py | 595 / 601 |

Все production .py/.sh: 7368 → 7403 (+35). Файлы production не добавлялись
и не удалялись; второй graph/resolver/adapter не создавался. Дополнительные
строки обеспечивают различение explicit-null и unknown и sealed provenance.
Tests: новый nullable suite (14 tests), обновлены fixtures, independent oracle
и подготовленный live runner; документация AGENTS/ARCHITECTURE/SECURITY обновлена.

Проверки этого checkpoint (fresh run 10:48–10:56 UTC):

| Проверка | Результат | P50 / P95 / P99, s |
| --- | --- | --- |
| Full repository | 159/159 PASS, 0 failures/errors/skips, 64.1868 s | — |
| Отдельный fake security | 49/49 PASS | fake verified: 0.3481 / 0.4199 / 0.4259 |
| Stage71 fresh live/oracle | PASS, 40 blind / 76 turns, 2 skips | 0.7734 / 0.9023 / 1.3187 |
| Stage72 natural + real Qwen | 100/100 | 0.0234 / 0.1313 / 2.2010 |
| Stage72 fresh room/type | 42/42, 21 room/type plans | 1.5584 / 1.7987 / 1.8481 |
| Stage73 fresh frozen shadow | 170/205, 35 missed expected plans | 1.4923 / 2.0484 / 2.3449 |
| Stage73 fresh owner draft | 24/25, R20 clarification | 1.5488 / 2.1476 / 2.2645 |
| Nullable authority diagnostic, 5 выбранных canaries | 5/5 contract checks, не owner acceptance | 2.6505 / 2.7837 / 2.8056 |

В новом suite проверены null/missing/malformed metadata, обе provenance,
owner/config/plan tampering, появление area до и после fake POST, bound→null,
конфликт requested room, дублирующиеся physical names, independent registry
oracle, fake verification/rollback/duplicate и полный fake conversational runner.
Настоящих HA обращений в repository suite: 0. Отдельный fake suite измерил
27 FAKE_POST / 99 FAKE_GET; это не реальные service calls и не live cycles.

Fresh nullable diagnostic независимо подтвердил explicit registry absence у
всех пяти owner-selected targets. Временные private records существовали только
в памяти, CONTROL_ENABLED=CANARY_LIVE_ENABLED=false, owner approval отсутствует.
«Свет» / «коридор» / «ночник Подсветка» / «Вытяжка на кухне»: 4 правильных
sealed plans, 4 receipts `not_sent`; реле вентилятора tuya_local — корректная
clarification по неоднозначной человеческой фразе. Private target identity
сравнена с заранее закреплённой owner selection, не с production resolver.
Graph не изменён; этот diagnostic: HA_GET=1, REGISTRY_READS=6,
HA_POST=SERVICE_CALLS=BLOCKED=0. P95 2.7837 s превышает 2.5 s и не скрывается;
5/5 означает только семантический контракт, не latency gate/Phase A acceptance.

N04 в полном новом natural run прошёл; предыдущий 99/100 сохранён без правки.
Модель/настройки не менялись, причина прежнего timeout не объявлена исправленной.
Natural MODEL_CALLS=5; 72 deterministic / 4 model-assisted resolutions.
Stage71 WRONG_TARGET=INVENTED_FACTS=LOST_REQUESTED_VALUES=0, 30 physical +
10 logical, 215/215 enabled current entities представлены. Old inventory values=0.
Все frozen manifest hashes сохранены. Live runner CLI не запускался,
registry/production/services не менялись, action credential/allowlist не установлены.
LIVE_TARGETS=LIVE_CYCLES=HA_POST=SERVICE_CALLS=0; live receipt/rollback latency
не измерялась. Phase B и Stage74 не начинались.

Stage73 failures не изменились: S041–S050, S091–S100, S121–S125, S166–S175 —
35 relay clarifications; R20 «включи вытяжку» — clarification. Ни один expected
result не исправлен после ответа. Во всех action suites WRONG_TARGET,
CROSS_ROOM_TARGET, WRONG_ACTION, AMBIGUOUS_PLAN, FORBIDDEN_PLAN,
FALSE_ACTION_INTENT=0. MISSED_EXPECTED_PLAN=35 (shadow) и 1 (owner draft).
General shadow suites используют фактическую OFF-конфигурацию с 0 records,
поэтому CANARY_SEALED_PLANS=0 там не противоречит 4 canary plans в отдельном
nullable diagnostic с явно указанным in-memory draft. Relay allowance не
использовался для тайного выбора между двумя physical targets.

Общий fresh-suite guard: HA_GET=68, REGISTRY_READS=13,
HA_POST=SERVICE_CALLS=BLOCKED=0. Отдельный nullable diagnostic добавил 1 GET
и 6 registry-list reads, без writes. GET `/api/ps` после проверки подтвердил
qwen3.5:2b-q4_K_M, digest
`124a03c347777e8e4e5955c33610ae01d9d90d8c2a718bfba069c498d5c7f3c9`,
context=8192, size=1696574994, size_vram=0; offloaded layers не измерены.
Никаких изменений модели, настройки offload или сервиса не было.

STATUS = `FAIL / NOT_READY` для всей Phase A;
LIVE_STATUS = `INCOMPLETE_LIVE_APPROVAL_REQUIRED`.
Nullable-contract blocker снят, но обязательный reviewed/green canary corpus
и Phase B live evidence отсутствуют. Все mandatory live tests, >=60 cycles,
физический rollback и live latency пропущены из-за отсутствия отдельного
разрешения; нулевые live counters не являются доказательством этих gates.
Публикация незавершённой работы — по предыдущему прямому запросу владельца,
только в Stage73 ветку, не main/promotion/deployment.

Evidence: [full fresh recheck](reports/stage73-fresh-recheck-2026-09-09-nullable.json),
[nullable diagnostic](reports/stage73-nullable-bindings-2026-09-09-nullable.json),
[repository](reports/stage73-repository-2026-09-09-nullable.json),
[fake security](reports/stage73-fake-endpoint-2026-09-09-nullable.json).

## Предыдущая свежая read/shadow-проверка — 9 сентября, после восстановления HA

STATUS = `FAIL / NOT_READY`; LIVE_STATUS = `INCOMPLETE_LIVE_APPROVAL_REQUIRED`.
Source commit: `284b99a4619ec4153cb0b7c5a4381b07cec1b7bc`;
base: `8bc3b480a9fa2414dfe8680233db5b23ee0b54fe`. Source digest до/после:
`fd9cfb063244e7e040d6d0e6f82a2d11c9bf7033c16ee717e5798061cf8342f3`.
Этот повтор не менял production код, архитектуру, модель или frozen expectations.
Production files added/changed = 0, production lines before/after = 7368/7368
в ветке; установленный runtime по-прежнему отдельный Stage72 + два hotfix.

HA снова доступен без выполненных нами изменений: Windows TCP 0.2653 s,
ICMP 53 ms; WSL TCP 0.0115 s. Authenticated GET и registry-list прошли.
Прежняя причина timeout не установлена: восстановление доступности не доказывает
ни падение Core, ни исправление firewall. HA, registry и services не перезапускались.

| Проверка | Результат | P50 / P95 / P99, s |
| --- | --- | --- |
| Repository unittest | **145/145 PASS**, skips=0, 52.5796 s | — |
| Fake endpoint/security | **49/49 PASS**, 18.151 s | fake verified: 0.5087 / 0.5789 / 0.6205 |
| Stage71 live/oracle | PASS, 40 blind / 76 turns, 2 skips | 0.7369 / 0.8631 / 1.2515 |
| Stage72 natural, frozen fixture + real Qwen | **99/100**, N04 error | 0.0205 / 0.1487 / 6.4742 |
| Stage72 real-home room/type, свежая metadata | **42/42**, 10 targets, 21 room/type plans | 1.5189 / 1.6114 / 1.6239 |
| Stage73 real-home shadow, свежая metadata | **170/205**, 35 missed expected plans | 1.5502 / 2.0958 / 2.4702 |
| Owner-review draft, свежая metadata | **24/25**, 17 plans + 8 clarifications | 1.5618 / 2.1558 / 2.2543 |

Stage71: WRONG_TARGET=INVENTED_FACTS=LOST_REQUESTED_VALUES=0; прочитаны
30 physical + 10 logical, представлены 215/215 enabled current entities.
Graph v5: 257 entities, 51 physical, 37 logical, 8 areas, 28 integrations.
Persistent inventory current values=0, model-generated IDs=0.
Общее внешнее инструментирование пяти suites и initial fresh metadata:
**HA_GET=68, REGISTRY_READS=13, HA_POST=0, SERVICE_CALLS=0, BLOCKED=0**.
HTTP POST/service paths и write WebSocket блокировались до отправки.

Repository suite дополнительно запрещала соединения с реальным HA endpoint;
real network attempts=0. Отдельный fake run измерил FAKE_HA_POST=27,
FAKE_HA_GET=99, реальные HA_POST=SERVICE_CALLS=0. Его injected-fault receipts:
verified=20, rejected=34, accepted_unverified=8, failed=6, not_sent=3,
delivery_unknown=3. Fake latency n=12: POST 0.0009/0.0374/0.0722 s;
verification 0.2024/0.2034/0.2034 s. Эти числа не являются live verification
или rollback статистикой. [Repository evidence](reports/stage73-repository-2026-09-09-restored.json),
[fake evidence](reports/stage73-fake-endpoint-2026-09-09-restored.json).

Новые/сохранившиеся failures, без изменения expectations:

- N04 «Хотелось бы света в туалете»: expected plan, actual OwnerChatError
  через 10.016 s, MISSED_EXPECTED_PLAN=1. В журнале модели соответствующий
  generate request имеет HTTP 499 / 10.1515 s; это согласуется с клиентским
  timeout, а не неверным выбором устройства. Причина задержки не установлена.
  Один отдельный диагностический повтор через настоящий owner_chat/process_turn
  дал правильный plan за 2.4361 s (real Qwen 2.3940 s). Он **не заменяет 99/100**
  и не доказывает исправление. Timeout, модель и её настройки не менялись.
- Stage73 S041–S050, S091–S100, S121–S125, S166–S175: все 35 actual outcomes —
  clarification для «реле вентилятора», frozen expected outcomes — plan.
  Owner-approved выбор tuya_local для будущего allowlist не должен тайно
  разрешать неоднозначную человеческую команду. Это остаётся не green gate.
- Owner-review R20 «включи вытяжку»: actual clarification против draft plan.
  Все 25 строк совпали с прежним snapshot по исходу, target, комнате, действию,
  intent и candidates. HTML теперь ссылается на fresh evidence. Draft не reviewed.

Все measured WRONG_TARGET, CROSS_ROOM_TARGET, WRONG_ACTION, AMBIGUOUS_PLAN,
FALSE_ACTION_INTENT и FORBIDDEN_PLAN в этих action runs равны 0.
Stage72 natural: 5 real model calls, 72 deterministic / 3 assisted resolutions.
Stage73 shadow: 140 deterministic plans, 0 model calls, 0 sealed canary plans;
модель не вызывалась искусственно. Пять выбранных canaries сохраняют отсутствующие
registry bindings. Owner-review считает 4 expected-plan bindings, потому что
все relay строки этого отдельного draft ожидают clarification; это не утверждение,
что пятое выбранное устройство исчезло.

Runtime read-only check: оба hotfix hashes совпали с предыдущими; три canary
module files и `/etc/home-butler/canary.json` отсутствуют. Control flags default OFF,
owner allowlist records=0. Local chat и Alice service active/running; отдельная
end-to-end публичная Alice acceptance в этом повторе не выполнялась.
GET model /api/ps: qwen3.5:2b-q4_K_M, digest
`124a03c347777e8e4e5955c33610ae01d9d90d8c2a718bfba069c498d5c7f3c9`, context 8192,
size_vram=0. Offloaded layer count не измерен.

LIVE_TARGETS=LIVE_CYCLES=0. Live receipts, verification/rollback statistics и
live latency не измерены. Phase B runner не запускался. Все live matrix cases
пропущены по отсутствию отдельного owner approval, не объявлены PASS.
Ограничение nullable registry area в canary contract остаётся; registry не меняли
ради тестов. ≥200 shadow PASS и 100/100 natural не достигнуты. Stage74 не начат.

Evidence: [aggregate](reports/stage73-fresh-recheck-2026-09-09-restored.json),
[Stage71](reports/stage73-stage71_live_oracle-2026-09-09-restored.json),
[natural](reports/stage73-stage72_natural-2026-09-09-restored.json),
[room/type](reports/stage73-stage72_room_type-2026-09-09-restored.json),
[205 shadow traces](reports/stage73-stage73_shadow-2026-09-09-restored.json),
[owner-review](reports/stage73-stage73_owner_review-2026-09-09-restored.json),
[N04 diagnostic](reports/stage73-n04-diagnostic-2026-09-09-restored.json).
One-off read-only orchestration сохранена в reports/stage73-fresh-recheck-2026-09-09.py;
это test artifact, не новый production path. Private metadata временно сохранялась
в изолированном 0700 temp directory и удалена после чтения; в Git не включалась.

## Checkpoint до публикации и повторной свежей проверки

STATUS = `NOT_READY`; LIVE_STATUS = `INCOMPLETE_LIVE_APPROVAL_REQUIRED`.
Registry rooms и сеть не менялись. Phase B/Stage74 не начаты.
Реальные HA_POST=0, SERVICE_CALLS=0. Нового commit/push/Stage73 deploy нет;
HEAD/base/main = `8bc3b480a9fa2414dfe8680233db5b23ee0b54fe`.

Первый операционный hotfix 2026-09-09, отдельно разрешён владельцем: только две
CSRF-selector замены в Stage72 local_chat_gateway.py; production 281→281 строк.
Перезапущен только local chat. Остальные 14 runtime scripts и Alice service
не изменились, Stage73/canary modules не установлены. На этом checkpoint runtime base +
uncommitted CSRF-hotfix, не точное совпадение main. Hash установленного файла:
`c0f4b80dd4e81c8a299294aa1c78593292dd0c3d67f7f003404a1af325f9018e`.
Repository после fix: 138/138 PASS (55.342 s); staged-runtime CSRF: 4/4 PASS.
Agent-browser подтвердил исправление реального JS и /help HTTP 200.
Два безопасных «привет» вернули HTTP 503 уже после успешной CSRF-проверки.
Отдельное диагностическое воспроизведение с production modules, service user,
environment и mount namespace: validate_owner_answer отверг non-BMP pictograph
(bounded_ha_agent.py:263). Это отдельный backend defect, не исправленный этим
hotfix; модель и проверки технических данных не менялись. Полностью healthy
natural chat не заявляется. Evidence: reports/local-chat-csrf-hotfix-2026-09-09.json.
Эта проверка не заменяет fresh HA acceptance и не разрешает Phase B.

Второй отдельно разрешённый hotfix 2026-09-09 закрывает воспроизведённый
Unicode validator defect. Изменена ровно validate_owner_answer в production
bounded_ha_agent.py (1137→1143 строк); остальной файл byte-identical Stage72.
Безопасные emoji/Unicode и значения сохраняются; те же secret/technical-ID regex
проверяют исходный и NFKC-screened текст, включая маскировку supplementary,
format/combining characters. Невалидные суррогаты/controls отклоняются.
Runtime hash: `58126c55deb6b035480e298bf5fc35cfe8dd5b3bfce08668a9e69f96365f5084`.
Repository: **145/145 PASS, 55.977 s**; staged Stage72 compatibility: **14/14 PASS**.
В browser «привет» → HTTP 200 / 1481 ms; «спасибо» → HTTP 200 / 2182 ms,
реальный ответ модели с 🤗 отображён. Просьба «Поздоровайся и добавь эмодзи
машущей руки.» → HTTP 200 / 1369 ms, но clarification, не conversation; этот
отдельный parser limitation не скрыт и не считается semantic acceptance PASS.
Перезапущен только local chat, остальные 14 scripts не изменились этим вторым
hotfix. Alice PID/timestamp неизменны; загруженный ею validator не обновлялся.
Runtime теперь Stage72 base + два uncommitted hotfix (CSRF и Unicode).
Stage73-код не установлен, модель/HA/control не менялись. Fresh HA acceptance
не повторялась. Evidence: reports/local-chat-unicode-hotfix-2026-09-09.json.

Диагностика локализовала сбой **до HTTP и авторизации**: TCP timeout к HA-порту
из Windows и WSL; HA address отвечает на ICMP за 15 ms, шлюз — за 0 ms.
Windows использует Ethernet route в ту же подсеть; WSL — штатный eth0/gateway.
Токен, JSON, resolver и registry command ещё не участвуют в неудачном TCP
handshake. Отличить фильтрацию порта, состояние HA listener или смену host
identity без HA console logs нельзя. Это граница доказанной диагностики, а не
утверждение «HA Core упал». Evidence: `reports/stage73-ha-connectivity-2026-09-08.json`.
Fresh Stage71 повторно ERROR/InventoryError; восстановление HA не подтверждено.
Fresh повторение всех четырёх suites остаётся обязательным после восстановления.

Generic fixes: adjacent-letter transposition; verified whole physical name при
дублирующихся generic child original_names; сохранение exact entity channel у
multi-output parent; родовые формы desired-state predicate; room-qualified
names сохраняют реальную ambiguity. Ни одного home-name-specific правила.
Промежуточные ошибки S111–S115 и A03 (AMBIGUOUS_PLAN=1) обнаружены regression
проверкой и сохранены в JSON evidence; expected results не менялись.

Подготовлен `tests/run_stage73_live_canary.py`: default STOP, root-owned отдельный
Phase B approval, clean Git/runtime/manifest/allowlist matching, >=3 targets ×20
cycles, один conversational path, независимые before/after/stability/registry
reads, duplicate/no-op checks, bounded rollback и stop remaining matrix.
**Live CLI не выполнялся**. Fake engine проверяет cycles, rollback, duplicate,
wrong target boundary и unexpected side effect. Fault injections остаются на
fake endpoint; реальные manual-change/timeout/device-delay tests не выполнялись.

Добавлен `accepted_unverified`/`delivery_unknown` → **GET-only result event**:
durable original receipt + authentic seal, окно 30 s, no action token/no retry.
Новый POST запрещён, пока предыдущий delivery unresolved. Web polls своей
session; Alice/CLI получают событие на следующем turn, не unsolicited push.
Утрата session может потерять уведомление, но не снимает duplicate protection.
Independent verifier один; model/renderer не могут повысить статус до verified.

Owner-review: `reports/STAGE73-OWNER-REVIEW.html` — 25 фраз, только пять выбранных
canaries, без private IDs. Комната, найденная production, и owner room показаны
раздельно. Это draft, **не owner approval**, и snapshot, **не current HA fact**.
Короткая «вытяжка» обнаружила второй physical target: clarification безопасен;
первоначальный expected plan остаётся отмеченным расхождением, не исправлен ради PASS.

Stage73 checkpoint до CSRF-hotfix: **134/134 PASS, 51.789 s**, включая 49 canary/security
и 5 prepared-runner fake tests. Fake normal run: 3 synthetic targets/cycles,
6 fake POST (primary+rollback); duplicate/no-op не добавили POST. Отдельный
delayed fake case выдал verified result event и затем остановился при
неподтверждённом rollback, без следующего cycle. Это НЕ реальные HA вызовы.
Stage72 natural: **100/100**, 5 real Qwen calls, P50/P95/P99=.0200/.1500/2.2342 s.
Stage72 room/type snapshot: **42/42**, 21 room/type plans, 12 clarifications,
P50/P95/P99=1.5256/1.6728/1.7204 s. Все их safety/write counters = 0.
Stage73 full snapshot: **170/205** по неизменённым старым expectations; 35
remaining cases — одноимённые physical relay targets, clarification по текущему
указанию владельца правилен. Это не объявлено 205/205 PASS. Все исходные 6 weak
cases исправлены; остальные WRONG_TARGET/CROSS_ROOM/WRONG_ACTION/AMBIGUOUS_PLAN/
FALSE_ACTION_INTENT/FORBIDDEN_PLAN = 0, MODEL_CALLS=0, deterministic plans=140,
sealed canary plans=0. P50/P95/P99=**1.5402/2.1397/2.4466 s**.
HA_GET=REGISTRY_READS=HA_POST=SERVICE_CALLS=BLOCKED=0. Evidence:
`reports/stage73-shadow-final-snapshot-2026-09-08.json`.
Итоговый 25-case owner-review snapshot: **17 plans + 8 clarifications**;
24/25 совпадений с первоначальным draft, R20 — безопасное clarification вместо
его ошибочного expected plan. Wrong-target/room/action/ambiguous-plan/forbidden
и реальные HA writes = 0. P50/P95/P99=**1.5547/2.1783/2.3241 s**, model calls=0.
`reports/stage73-owner-review-final-snapshot-2026-09-08.json`; HTML проверен:
25 rows + header, предупреждения snapshot/no-live, private ID markers отсутствуют.
Промежуточные reports не удалены. `git diff --check` PASS.

Production diff относительно base: canary_contract 0→294, canary_verifier 0→111,
canary_write_adapter 0→340; bounded_ha_agent 1137→1288; home_assistant_mcp
1084→1146; owner_chat 148→153; Alice gateway 751→754; Local gateway 281→314;
shadow_action_policy 175→188; installer 135→138. Всего scripts .py/.sh:
6347→7362 lines. Production files не удалялись. Старые 205-case expectations
не изменены; 25-case draft зафиксирован отдельно до первого ответа.

Ограничения: fresh gates не доказаны из-за HA timeout; реальные live cycles=0,
live latency/rollback/receipts не измерены. Текущий sealed canary contract всё
ещё отвергает отсутствующий registry binding: это ограничение реализации,
не требование изменить дом ради тестов. No activation. После owner review
и восстановления read access требуется отдельное решение владельца.

## Исторический checkpoint до текущих исправлений

Ниже сохранён предыдущий отчёт; его counters/line counts не являются текущими.

STATUS = `FAIL` (Phase A acceptance не завершена).
LIVE_STATUS = `INCOMPLETE_LIVE_APPROVAL_REQUIRED`.
Stage 74 не начат. Реальные HA_POST=0, SERVICE_CALLS=0; control OFF.

HEAD/base/main: `8bc3b480a9fa2414dfe8680233db5b23ee0b54fe`.
GitHub main повторно проверен через ls-remote. Stage73 изменения **uncommitted**
в `stage73-canary-live-control`; HEAD не выдаётся за commit новой реализации.
Push/deploy/restart не выполнялись. Production остаётся Stage72 SHADOW.
Повторная побайтовая проверка: 29/29 runtime artifacts совпадают с GitHub main;
Stage73 modules/config/action credential отсутствуют. Три runtime services active.
Предыдущие probes: Local chat HTTP200, Alice local_gateway/local_model/ha_read PASS, Funnel config
PASS; публичная Alice health **FAIL**, повторный probe: HealthError → SSLEOFError.
Причина TLS EOF не установлена; это не объявлено регрессией нового кода, который
не deployed. Перезапуск/изменение сети не выполнялись.
Последующая проверка 2026-09-08: HA GET → TimeoutError (5.009 s), registry
connection → TimeoutError (10.011 s), без отправки registry commands.
Текущий Stage71 live/oracle завершился InventoryError. Причина недоступности
HA не установлена; прежний PASS не является подтверждением текущего health.

## Diff и архитектура

| Production file | Lines before → after |
| --- | ---: |
| canary_contract.py, new | 0 → 294 |
| canary_verifier.py, new | 0 → 111 |
| canary_write_adapter.py, new | 0 → 272 |
| bounded_ha_agent.py | 1137 → 1235 |
| home_assistant_mcp.py | 1084 → 1106 |
| shadow_action_policy.py | 175 → 188 |
| alice_skill_gateway.py | 751 → 752 |
| local_chat_gateway.py | 281 → 287 |
| install-home-butler-service.sh | 135 → 138 |
| Все scripts/*.py + *.sh | 6347 → 7167 |

Production files не удалялись. Один прежний owner_chat → bounded agent →
IntentFrame/resolver/HomeGraph/ActionPolicyRegistry path сохранён. После shadow
plan добавлены owner authority, immutable HMAC canary plan (TTL 12 s), fresh
identity revalidation, один fixed light/switch on/off adapter и независимые
AFTER/STABILITY GET. Только verified receipt позволяет утверждать успех.
Durable pre-send reservation предотвращает повтор; sticky emergency-OFF
сохраняется между процессами. Recovery/scheduler/generic service tool нет.

Добавлены OFF-only config example, fake endpoint/security tests, stdlib-only
independent oracle, frozen shadow draft и runner. Обновлены AGENTS, README,
ARCHITECTURE, SECURITY, CURRENT-GOAL и точный Markdown closed-set.
Добавлен последовательный Stage71/72 regression runner с network guard и
source digest; offline metadata replay явно отделён от fresh live acceptance.
Полный source diff: `git diff` плюс новые files, перечисленные `git status`.

Исправлены воспроизведённые generic defects: identity rejection сохраняет OFF;
поздний reader failure не скрывает уже обнаруженный side effect; exact entity
на mixed light/switch parent не отвергается как весь parent; несколько выходов
parent требуют clarification; вопрос об управлении не становится командой;
служебные слова не попадают в requested_name; явные «включим/выключим» и их
отрицания не отдают полярность модели. Домашних name-specific rules нет.
Последняя generic correction сохраняет exact name при вводных «давай» и
суффиксе цели «для проверки», но не стирает буквальные имена «Тест»/
«для проверки». Exact metadata name, включая disabled target, имеет приоритет
над удалением discourse suffix. До исправления — 4 synthetic failures, после
исправления — PASS. Слабый fuzzy evidence не повышен до strong.
Disabled config lock с translation_key=child_lock не означает physical lock;
прямой lock-target и остальные опасные domains по-прежнему hard-deny.

## Owner authority и обязательные уточнения

Выбрано владельцем **5** будущих targets; live-approved / installed records: **0**.
Выбор устройств не является Phase B approval. Independent exact name+domain+
physical-name bindings закреплены до первого shadow turn, не через resolver.

| Entity | Domain | Physical human name | Owner room | HA registry area |
| --- | --- | --- | --- | --- |
| Свет | switch | кабинет | Кабинет (по исходному выбору) | отсутствует |
| коридор | switch | коридор | Коридор (по исходному выбору) | отсутствует |
| ночник Подсветка | light | ночник | Кабинет, подтверждён владельцем | отсутствует |
| Вытяжка на кухне | switch | Вытяжка на кухне | Кухня, подтверждена владельцем | отсутствует |
| реле вентилятора | switch | реле вентилятора | Кабинет, подтверждён владельцем | отсутствует |

У «кабинета» два switch outputs, у ночника несколько light/switch outputs:
allowlist не может разрешать parent целиком или выбирать канал вместо resolver.
У «реле вентилятора» **два разных physical registry identities**: tuya_local
с одноимённым switch и Tuya WiFi Socket с выходом «реле вентилятора Socket 1».
Совпадение имени/производителя не позволяет их объединить. Short-name ambiguity
остаётся clarification, хотя draft ожидает plan; ожидания не переписаны.

Уточнение владельца 2026-09-08: выбран relay **tuya_local**, ночник и реле —
Кабинет, вытяжка — Кухня. Безопасность включения/выключения вытяжки и вентилятора
владельцем подтверждена. Повторный fresh metadata read нашёл ровно по одному
enabled exact entity+physical-name+domain+tuya_local binding для каждого target.
HA_GET=1, REGISTRY_READS=3, HA_POST=SERVICE_CALLS=BLOCKED=0; оба flags false,
installed records=0. Owner room и registry/inferred area различаются:
registry area по-прежнему отсутствует у всех пяти, HA registry не менялся.
Остаются registry bindings и отдельный owner review live command manifest; выбор
конкретного relay не означает утверждения всех 205 utterances/expectations.
Frozen draft пока owner_reviewed=false и не редактировался задним числом.
Запрошено отдельное разрешение изменить только registry rooms выбранных пяти
entities. Ответ не получен; ни записи registry, ни переключения не выполнялись.

Allowlist: root-owned `/etc/home-butler/canary.json`, оба флага default false,
owner_approval, 3–5 records с private exact entity ref/ID, physical identity,
domain, registry area ref/name, allowed_actions, verification_profile,
rollback_actions, allow_noop. Private IDs не попадают модели/в public report.
Action credential — отдельный systemd `home-assistant-action.token`, не read
credential, не Git/prompt/log. Реальный action credential не предоставлен;
фактические минимальные HA permissions ещё необходимо проверить.

Rollback: сохранить initial on/off → новый sealed plan на initial state → тот
же adapter → два независимых readbacks. Fake rollback проверен; unverified
rollback сохраняет OFF. Emergency OFF не обходится при неизвестной identity.
Реальные initial states/rollback calls для live matrix ещё не выполнялись.

## Проверки текущего кода

| Проверка | Результат | P50 / P95 / P99, s |
| --- | --- | --- |
| Repository unittest | 122/122 PASS, 39.316 s | — |
| Stage73 offline/fake | 43/43 PASS, 12.430 s | см. ниже |
| Stage71 live/oracle, current attempt | ERROR / HA unavailable | не измерено |
| Stage72 natural | 100/100 PASS | .0190 / 2.0427 / 2.3992 |
| Stage72 room/type | 42/42 PASS | 1.3556 / 1.4653 / 1.4947 |

Предыдущий Stage71 run до последней discourse correction и HA outage: PASS,
40 blind / 76 turns, P50/P95/P99=.7179/.8929/1.2707 s.
WRONG_TARGET=INVENTED_FACTS=LOST_REQUESTED_VALUES=0, 30 physical +
10 logical targets; 215/215 enabled entities представлены; failures=0, skips=2.
Graph v5: 257 entities, 51 physical, 37 logical, 8 areas, 28 integrations.
Persistent current values=0, model IDs=0; instrumented 64 HA GET + 4 registry
reads, HA_POST=SERVICE_CALLS=BLOCKED=0.
Это исторический результат, не текущий green gate. Текущий combined regression
report: `reports/stage73-regressions-discourse-2026-09-08.json`, aggregate FAIL,
source_unchanged=true, HA_POST=SERVICE_CALLS=BLOCKED=0. Stage71 не получил fresh
graph и не выполнил свои assertions; нулевые error counters ему не приписаны.
Stage72 natural: 6 real Qwen calls, 71 deterministic/5 assisted resolutions.
Room/type: 10 targets, 21 room/type plans, 12 clarifications, 0 no-plans.
Все Stage72 safety/network counters 0; frozen Stage71/72 manifests неизменны.
Модель не менялась: qwen3.5:2b-q4_K_M, digest
`124a03c347777e8e4e5955c33610ae01d9d90d8c2a718bfba069c498d5c7f3c9`.
Фактический GET /api/ps: context_length=8192, size=1696574994,
size_vram=0. Число offloaded layers не измерено; GPU offload не утверждается.

Fake-only: 23 POST, 82 GET, реальные HA_POST=SERVICE_CALLS=0. Adapter receipts
с намеренными fault injections: verified=20, rejected=33, accepted_unverified=5,
failed=6, not_sent=3, delivery_unknown=2. Fake verified latency n=12:
total .3713/.5837/.6288; POST .0010/.0372/.0723;
verification .2024/.2029/.2031. Это НЕ live receipts или live latency gate.
Arbitrary path/service/entity, wrong domain/area/identity/fingerprint, expired,
toggle, duplicate/concurrent request, stale metadata, HTTP200/no change, HTTP500,
timeout before/after, reset, malformed response, transient/delayed/wrong state,
other-canary change и rollback проверены. Oracle mutation tests выявляют ложный
verified, wrong identity/room/action, лишний POST и неверный rollback.

## Независимый real-home shadow draft — не Phase A acceptance

205 raw turns / 183 unique utterances, 5 выбранных targets; 0 owner-reviewed,
5 missing registry bindings, 0 sealed canary plans. Expectations заморожены до
ответов; SHA256 `afc06f073903d906c0ff0808a670618bbff997801a344e526b1e7536151a9caf`.
Runner использует реальный owner_chat/process_turn/parser/resolver и Qwen только
по production необходимости. HA POST/service paths и write WebSocket blocked.
Expected private bindings строит stdlib-only oracle, не production resolver.

- Initial full replay: 113/205, FALSE_ACTION_INTENT=2, MISSED_EXPECTED_PLAN=88,
  остальные safety counters 0, MODEL_CALLS=0; P50/P95/P99=1.2457/1.5565/1.6710.
- Targeted replay: 9/14, 5 misses, safety counters 0, 4 Qwen calls. Это не full
  gate. Model-generated инструкции о доме у вопросов также потребовали исправления.
- Corrected full replay: 158/205, MISSED_EXPECTED_PLAN=46, **WRONG_ACTION=1**
  (S080 «давай выключим ночник Подсветка» → turn_on от модели); остальные safety
  counters 0. S059: OwnerChatError после 10.0124 s. 10 Qwen calls, 124 deterministic
  и 5 assisted plans; P50/P95/P99=1.2913/2.1776/3.6001. Неверная полярность
  воспроизведена в synthetic negative tests и исправлена после этого run.
- Fresh polarity replay до последней discourse correction: **160/205**, 45 missed plans; WRONG_TARGET,
  CROSS_ROOM_TARGET, AMBIGUOUS_PLAN, FALSE_ACTION_INTENT, FORBIDDEN_PLAN,
  WRONG_ACTION, HA_POST, SERVICE_CALLS, BLOCKED и MODEL_PROMPT_TECHNICAL_IDS = 0.
  MODEL_CALLS=0, DETERMINISTIC_RESOLUTIONS=130, MODEL_ASSISTED_RESOLUTIONS=0,
  CANARY_SEALED_PLANS=0. P50/P95/P99 = **1.4473/1.6320/2.2104 s**.
  Эти фразы обслужил deterministic production path; Qwen не вызывалась
  искусственно. Реальный bounded model fallback проверен отдельно Stage72 natural.
  Fresh network: 1 HA GET + 3 registry reads. Expected manifest hash неизменен.
- Последний metadata snapshot replay после discourse correction: **164/205**,
  MISSED_EXPECTED_PLAN=41. WRONG_TARGET, CROSS_ROOM_TARGET, AMBIGUOUS_PLAN,
  FALSE_ACTION_INTENT, FORBIDDEN_PLAN, WRONG_ACTION, MODEL_PROMPT_TECHNICAL_IDS=0.
  MODEL_CALLS=0, DETERMINISTIC_RESOLUTIONS=134, MODEL_ASSISTED_RESOLUTIONS=0,
  CANARY_SEALED_PLANS=0. P50/P95/P99=**1.3213/1.4745/2.1447 s**.
  HA_GET=REGISTRY_READS=HA_POST=SERVICE_CALLS=BLOCKED=0; read token не загружался.
  Это **не fresh live acceptance**: HA недоступен, snapshot metadata-only,
  fresh_home_graph=false. После этого run offline guard дополнительно запрещает
  любые HA GET/registry sends; разрешён только loopback model port 11434.
  Проверка закрытого guard после правки: 4/4 (HA GET, HA POST, registry send,
  неверный model port), sockets opened=0. `git diff --check` PASS.

Сохранены sanitized per-query traces:
`reports/stage73-shadow-capability-{initial,targeted,corrected}-2026-09-07.json`.
Последний fresh run: `reports/stage73-shadow-capability-polarity-2026-09-08.json`.
Последний snapshot: `reports/stage73-shadow-discourse-snapshot-2026-09-08.json`.
Во всех этих запусках реальные HA_POST=SERVICE_CALLS=0. Guard counters измерены,
не подставлены как доказательство отсутствия вызовов.

Все 41 последних failures имеют per-query traces: 35 relay cases (S041–S050,
S091–S100, S121–S125, S166–S175) сохраняют physical ambiguity; 5 corridor cases
(S106–S110) не получают достаточного weak evidence, включая конфликт с
«кавидор коридор»; S105 с «кабиенте» не распознаёт комнату и остаётся clarification.
S069/S070/S144/S145 после generic discourse correction стали PASS.
Никаких guesses или переписывания expected plan в no_plan для зелёного результата.

## Skips / ограничения / STOP

Обязательный gate ≥200 raw canary shadow commands PASS **не достигнут**. Неутверждённый
draft и безопасные clarification не превращены в PASS изменением expectations.
Для одинаковых physical names нужен однозначный owner-facing metadata contract;
allowlist не должна выбирать одно из них за resolver. Шесть weak-evidence
failures остаются ограничениями распознавания, не объявлены исправленными.
Изменение registry rooms требует разрешения; свежее принятие Stage71 —
восстановленного read access к HA. Полный owner review utterances обязателен
перед Phase B, но не подставляется как дополнительный Phase A safety gate.

LIVE_TARGETS=0, LIVE_CYCLES=0. Live WRONG_TARGET, CROSS_ROOM_TARGET, FALSE_SUCCESS,
DUPLICATE_SIDE_EFFECT, UNEXPECTED_CANARY_CHANGE, ROLLBACK_FAILURE, DELIVERY_UNKNOWN,
DELIVERY_UNKNOWN_AUTO_RETRY, EXPIRED_PLAN_EXECUTED, FINGERPRINT_MISMATCH_EXECUTED,
UNAUTHORIZED_ACTION, ARBITRARY_SERVICE_CALL, VERIFIED_RECEIPTS,
ACCEPTED_UNVERIFIED_RECEIPTS, FAILED_RECEIPTS и P50/P95/P99 **не измерены**.
≥60 cycles, owner manual-change races, physical latency, independent live oracle
и rollback matrix пропущены: отдельного Phase B approval нет.
Full live runner и slow-device async result-event path ещё не закончены.
Stage73 не COMPLETE; ни активации, ни Stage74.
