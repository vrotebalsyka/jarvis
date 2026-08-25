# Phase 66 — итог эволюционного апгрейда Home Butler

Дата итоговой source/runtime-квалификации: 2026-08-25.

Статус: Phase 66 развёрнут в `/opt/home-butler`. Перезапускались только службы
Home Butler. Home Assistant, его контейнер, устройства, `ollama.service` и
`tailscaled.service` не перезапускались и не переключались. Локальный чат
доступен на `http://127.0.0.1:8780/`, защищённый LAN-вход — на
`http://192.168.1.175:8780/`.

Controlled outage acceptance O/P по указанию владельца не выполнялись. Поэтому
deployment и штатное end-to-end здоровье подтверждены, а время автоматического
восстановления искусственно остановленного Funnel/skill ещё не измерено.

## Главные root causes

1. Model/context/timeout задавались в нескольких местах и расходились с
   документацией.
2. Разговор хранился короткой session history и не переживал restart как единая
   owner memory.
3. Монолитный routing смешивал разговор, tool selection и business rules;
   некоторые ответы становились шаблонными или обещали будущую работу без
   persistent task.
4. Entity, component, HA device, integration, physical device и network node
   не во всех ответах различались одинаково.
5. Alice guardian видел transport слишком крупным блоком и мог лечить не тот
   компонент; cadence давал минутные задержки.
6. Напоминания и отчёт имели несколько источников времени.
7. Recovery predicates были распределены по коду, а staged/qualified состояние
   не было единым декларативным контрактом.
8. Runtime DB/WAL/log/cache попадали в Git и создавали риск утечки/дрейфа.
9. После deployment одновременно стартовали несколько model probes при
   `OLLAMA_NUM_PARALLEL=1`; это создало очередь и тайм-ауты. Частый Alice health
   ошибочно выполнял генерацию каждые 10 секунд вместо дешёвой проверки уже
   подтверждённой readiness.
10. `home-butler-system-log-diagnostics.service` вызывал GPU-модель, но sandbox
    разрешал только HA, а `TimeoutStartSec=30` был короче runtime policy.
11. System Log Intelligence включал изменяющиеся timestamp/count в occurrence
    fingerprint и не имел отдельного semantic fingerprint. Повтор одного и того
    же текста заново занимал единственную очередь Ollama. Допустимый повтор
    allow-listed read-only check в JSON модели также ошибочно превращал весь
    результат в молчаливый `unknown` fallback.
12. Windows wake вычислялся из scheduler только во время ручной установки.
    Live task сохранил старый ежедневный trigger `12:58`, поэтому перенос
    расписания не обновлял пробуждение и создавал второй источник времени.
13. Windows GPU supervisor сохранял устаревший server default
    `OLLAMA_CONTEXT_LENGTH=2048`. Профильные запросы уже переопределяли его,
    но следующий холодный запуск без options мог снова получить 2K default.

## Исправленный дрейф документации

- «64K production context» был неверен: старый live runtime показывал 4096;
  выбранный и сейчас подтверждённый voice runtime — 8192.
- Управление не ограничивалось switch/light/button: catalog уже охватывает fan,
  humidifier, siren, vacuum, number и select.
- Заявленные 30 секунд/около 22 секунд Tunnel recovery не соответствовали
  старому timer 60 секунд и общему failure counter. Новый расчётный source
  upper bound после confirmation — 41.2 секунды; реальный controlled outage ещё
  не измерен.
- Фактическая установка — HA Container/Core path, а не HAOS supervisor path.

## Model Runtime Policy и benchmark

Единый `model_runtime_policy.py` задаёт immutable profiles. Все production
routes используют одну модель, чтобы не перегружать единственный model slot:
`qwen3.5:4b-q4_K_M`, фактически 4.7B, Q4_K_M, полностью в VRAM. После
`evaluate_model.py` `/api/ps` показывает 8192 для последнего structured/voice
запроса. Отдельный фактический dialogue-profile turn завершён, после него
`/api/ps` показал `context_length=32768` и `size_vram=size=3.78 GB`.
Dialogue/diagnostic поэтому подтверждены не только policy, но и live runtime.

| Profile | Модель | Context | Назначение |
|---|---|---:|---|
| voice_fast | Qwen 4B | 8K | Алиса, короткий HA query/action |
| dialogue | Qwen 4B | 32K | длинный local chat |
| diagnostic | Qwen 4B | 32K | semantic diagnosis/tool loop |
| structured | Qwen 4B | 8K | закрытый JSON |
| summarizer | Qwen 4B | 16K | memory compaction без tools |

Полная матрица 2B/4B × 8/16/32/64K находится в
`PHASE-66-MODEL-BENCHMARK.md`. Финальный warm end-to-end Alice facade:
30/30 model-completed, fallback 0, P50 1.924 s, P95 2.981 s, max 2.993 s при
budget 3.2 s. Текущий evaluator после чистой загрузки измерил cold load
29.612 s; последующие ответы заняли 0.948–2.917 s.

## Память и незавершённые задачи

Одна private SQLite FTS5 Memory Store хранит bounded recent turns, compact
summary, semantic owner/device/episodic/procedural facts, corrections через
supersedes, несколько ActiveGoal и secret-safe retrieval traces. Raw transport
session заменён fingerprint. Context Builder извлекает релевантные блоки в
пределе 4700 approximate tokens, а не отправляет весь history.

Slow Alice turn теперь создаёт настоящий ActiveGoal и возвращает task ID.
Результат сохраняется, появляется на следующем turn и доступен через
`статус задачи <ID>`. После restart read-only/conversation goal возобновляется;
mutating control не переигрывается и остаётся честным `delivery_unknown`.

## DeviceGraph, network identity и MCP

Существующий inventory мигрирован до schema 3 без второго графа. Он различает:

`entity → component/feature → HA device → config entry/integration → physical device → network identity`.

Raw registry/config-entry IDs, IP/MAC и connections остаются private. Модель
видит opaque physical ID и сокращённый network status. Совпадение имени не
объединяет устройства; stable identity и alternate integration учитываются.

Read-only MCP содержит 14 tools: snapshot, legacy entity/device lookup,
compact index, natural device/entity discovery, exact details, diagnostics,
related incidents/logs, bounded history, capabilities и onboarding queue.
Обычный agent loop не получает полный entity dump и не имеет service-call
surface.

## Семантические ошибки и отказ components

Semantic Entity Catalog использует domain, device class, translation/original
name, unit, options, siblings и текущее observation. Log Intelligence принимает
только sanitized warning/error как untrusted data и возвращает закрытую schema
с `action_authority=none`.

Минутный deterministic сбор логов сохранён, но semantic classification теперь
кэшируется по стабильному смысловому fingerprint без timestamp/count. Кэш
versioned, ограничен 4096 записями и 30 днями, не содержит raw log text. Новое
occurrence по-прежнему записывается в incident ledger. Live proof: первый batch
из семи новых смыслов занял 53 секунды и дал `model_classified=7`; следующий
batch из пяти повторов завершился менее чем за секунду с
`model_classified=0`, `semantic_cache_hits=5`, `actions_attempted=0`.

Поэтому:

- одна diagnostic entity пылесоса не делает весь пылесос неисправным;
- неизвестный расходник посудомойки связывается с конкретной feature без
  словаря vendor-фраз;
- неизвестный numeric code сообщается буквально и без выдуманного значения;
- полный physical outage требует согласованных evidence, а краткий unavailable
  закрывается debounce;
- доступные siblings/alternate integration блокируют recovery всего прибора.

## Alice health и Funnel

Guardian schema 3 раздельно проверяет gateway, public route, Tailscale, model
endpoint/load/turn, HA read и owner config. Transport check — 10 секунд,
confirmation — два наблюдения; дорогие model/HA probes кэшируются. Частый
health turn больше не запускает новую LLM-генерацию: readiness устанавливается
только после успешного startup warm-up, а редкие heartbeat/self-check дают
полный model proof. Recovery
выбирает только exact component, имеет backoff/circuit breaker и не использует
LLM. HA failure никогда не перезапускает Tunnel, а model failure — HA.

Startup self-check теперь выполняется один раз после запуска WSL: в production
timer оставлен только `OnBootSec=90s`, без повторного `OnUnitActiveSec=10min`.
Периодическую проверку продолжает отдельный heartbeat каждые 10 минут. После
развёртывания timer один раз завершил проверку со статусом `ready`, затем
перешёл в `active (elapsed)` с `NEXT=n/a`; это убирает лишнюю конкуренцию за
единственную очередь Ollama, не ослабляя постоянный heartbeat.

Расчётный Funnel recovery upper bound после confirmation — 41.2 секунды.
Controlled live outage не выполнялся, поэтому это не называется измеренным
production recovery time.

## Scheduler, напоминания и отчёт

Единый SQLite TaskSpec scheduler хранит reminders, reports, scheduled actions,
follow-up и deferred results. Daily report 13:00 Asia/Yekaterinburg теперь
является записью в базе. Natural parser понимает «завтра утром в восемь
напомни…» без обязательного шаблонного слова. Update/cancel ищет обычное имя.
Lease/idempotency обеспечивают exactly-once; delivery_unknown не повторяется.

Read-only production status подтвердил task `system-daily-report`: следующий
запуск 25 августа 2026 в 13:00 Asia/Yekaterinburg, wake export — 12:58,
`state=not_due`, `attempts=0`. После restart только Home Butler scheduler
timer/oneshot task ID, next run и attempts сохранились; tick вернул
`executed=0`, то есть ранней доставки не произошло.

Windows wake теперь не хранит собственное расписание. Скрытый ограниченный
worker `Home Butler Scheduler Wake Sync` раз в пять минут, но только когда
Windows уже работает, читает безопасный `wake_epoch` из Ubuntu. Нативный helper
принимает только один целочисленный epoch и может обновить только one-shot task
`Home Butler Scheduler Wake`; его единственное действие — запустить exact task
`Home Butler WSL Runtime`. Runtime actions не используют PowerShell. Live
readback подтвердил: WSL Runtime имеет только logon trigger, one-shot wake
назначен на 25 августа 12:58, `WakeToRun=true`, sync завершился с кодом 0, а
private evidence сохранён с mode `0600`.

GPU supervisor теперь получает server default из
`ModelRuntimePolicy.dialogue.context_window`, то есть 32768. Добавлен приватный
single-instance lock: после обнаружения двух осиротевших WSL-наблюдателей
они были завершены, а задача подняла ровно один экземпляр. Завершение
старой Windows task закрыло и её дочерний Ollama: PID изменился с 14584
на 70996. Новый supervisor автоматически восстановил `ollama.exe` 0.32.5;
после dialogue-turn `/api/ps` показал `context_length=32768`,
`size_vram=3.78 GB`. Home Assistant, устройства, Tailscale и Linux
`ollama.service` не перезапускались.

Отдельный source-аудит подтвердил: production-literal `13:00` существует ровно
один раз — в seed-записи `system-daily-report` внутри persistent scheduler.
Systemd, operations supervisor, Windows wake helper и разговорный backend не
имеют собственного времени отчёта. Новый regression-тест сканирует production
source/config и запрещает появление второго такого источника.

Примеры после deployment:

- `напомни завтра в восемь заказать таблетки для посудомойки`;
- `с завтрашнего дня ежедневный отчёт в 11:40`;
- `покажи мои напоминания`;
- `отмени напоминание про таблетки`.

## Recovery playbooks

Registry содержит девять bounded playbooks R0/R1: observe/notify, Yandex
backoff, exact retry original intent, exact Yandex/config-entry reload, exact
helper repair, closure already matched/obsolete intent, exact generic/local
integration reload. Каждый имеет evidence, preconditions, allow-listed adapter,
verification, rollback note, attempt budget, cooldown и stop conditions.

R1 остаётся staged до offline + dry-run + owner approval + controlled-live +
post-observation qualification. Definite failure может перейти к следующему
declared safe step; verified/no-action останавливает ladder; delivery_unknown
останавливает её немедленно.

## Новое устройство и изменение поведения

Onboarding schema 2 собирает известные facts в private queue и спрашивает только
неизвестные name/area/criticality/recovery/notification facts. Ответы владельца
можно дать несколькими сообщениями: partial answers сохраняются до завершения
proposal. Модель не получает opaque ID/hash: deterministic facade сам находит
единственный текущий item и принимает только точную фразу подтверждения для его
human name. Proposal требует внутренний exact hash approval; до него HA write
отсутствует. Restricted/unknown device остаётся observe-only. Schema 1
мигрируется идемпотентно без потери очереди.

Фактический model evaluator провёл полный natural flow: «Он находится в
спальне.» создало proposal, «Подтверждаю предложение для Комнатный датчик.»
перевело его в `approved`, `actions_performed=0`. Защищённый production POST
через `http://127.0.0.1:8780/api/chat` с реальными cookie/Origin/CSRF на вопрос
«Есть новые устройства?» вернул: «Новых устройств, ожидающих настройки или
подтверждения, сейчас нет.» Защита local chat не ослаблялась.

Поведение меняется через закрытые `behavior_get/set/reset`, например:
`не сообщай о Wi-Fi-сбоях короче минуты`. Настройка не может включить shell,
arbitrary HA call, отключить verification/cooldown или изменить R3 policy.

## Safe self-improvement

Production model может создать только семипольный ChangeProposal. Patch
готовит отдельный owner-invoked worker в isolated worktree; fixed pipeline
проверяет unit/full/no-cloud/model/diff. Exact approval не deploy; требуется
второе exact `DEPLOY <hash>`. Failed health probe автоматически снимает patch и
повторно устанавливает предыдущий source. Conversation model не имеет этих
maintenance tools.

## Наблюдаемость

Local chat и Alice теперь создают один secret-safe turn trace в существующей
SQLite Memory Store. Он содержит owner/session fingerprint, transport,
route/profile/model, Ollama input/output token counts, context sections,
retrieved memory IDs, tool names/latency, policy result, playbook/action/
verification codes, total latency и final disposition. Raw prompts, сообщения,
tool arguments/results, entity IDs, IP и MAC туда не записываются. Существующий
Incident Store остаётся единственным recovery ledger и уже хранит facts,
decision, adapter, attempts/cooldown, before/after, evidence и result. Детали:
`PHASE-66-OBSERVABILITY.md`. На вопрос владельца о причине перезагрузки
интеграции ответ теперь включает подтверждённую причину, bounded action,
количество попыток и readback-проверок, не раскрывая private target, IP или MAC.

Дополнительный live read-only аудит подтвердил, что device-health и notifier
выполняются каждые 10 секунд. На момент аудита один полный outage был
подтверждён за 20 секунд, а сообщение принято колонкой с первой попытки через
21 секунду; переключений устройств и recovery не было. Одновременно найден и
исправлен lifecycle-дефект: повторный отвал в течение трёх минут после уже
озвученного восстановления мог наследовать старую отметку «сообщено».
Корреляция до recovery notice сохранена как антидребезг, но после принятого или
delivery-unknown recovery notice следующий подтверждённый отвал получает новый
device-incident и отдельную дедупликацию. Старые открытые rollup-записи
исправляются идемпотентно без потери истории.

Qualification renderer теперь fail-closed проверяет временную причинность:
accepted outage notice должен быть не раньше confirmation, а recovery notice —
не раньше recovery. Историческая невозможная метка больше не отображается как
отрицательная задержка и не засчитывается доказательством. Live UI после
deployment показывает `alert_seconds=null` и `waiting_alert`, сохраняя две
действительно подтверждённые аппаратные проверки и подтверждённый диалог.

## Optional Assist и Vision

Официальные Conversation API, Assist LLM API/MCP и Assist Pipeline позволяют
добавить Home Assistant как transport к тому же core. Adapter пока не
установлен. Vision остаётся feature-disabled, не получает action tools и без
разрешения не загружает model. Детали: `PHASE-66-OPTIONAL-TRANSPORTS.md`.

## Проверки

- полный offline suite после onboarding facade и закрытого approval:
  **693 tests, OK, 1 skipped**;
- isolated model evaluator: **7/7 passed**, включая natural onboarding и
  deterministic approval без HA write;
- targeted onboarding/agent/chat/MCP/systemd: **48 tests, OK**;
- `/api/ps`: Qwen 4.7B Q4_K_M, dialogue context 32768, full VRAM;
- no-cloud audit: cloud keys absent, cloud fallback absent;
- `git diff --check`: passed;
- warm full Alice facade: P95 2.981 s, 30/30 completed.
- targeted turn-observability regression: 121 passed.
- live read-only model/HA startup self-check: ready, GPU, 215 entities,
  `service_calls=0`;
- live local/public dialogue qualification: ready; history и free dialogue
  подтверждены через оба transport;
- live Alice health: `alice_skill_health=ready`;
- semantic system-log diagnostics после sandbox correction завершилась за
  103.3 s без recovery/action.
- semantic log cache live proof: 53 s для семи новых смыслов, затем менее
  1 s для пяти повторов без нового model call; startup self-check после этого
  снова `ready` за 35.8 s.
- следующий полностью timer-driven cycle также прошёл: cached log diagnostics
  менее секунды, startup self-check `ready` за 34 s, heartbeat завершился
  успешно за 8 s; фоновые model jobs больше не образовали очередь.
- production startup timer после исправления выполнил один read-only proof:
  `ready`, GPU, HA, 215 entities, tool proof; затем `active (elapsed)`,
  `NEXT=n/a`. Heartbeat сохранил `OnUnitActiveSec=10min`; recovery-action
  timers остались disabled/inactive.
- secret-safe deployment parity audit: 68/68 runtime scripts совпадают с
  одноимённым source, 58/58 managed systemd units совпадают; Hermes config,
  service config, policies, skills и HA env совпадают после ожидаемых
  path/credential normalization. `verify-runtime-policy.py` вернул
  `RUNTIME_POLICY_OK` при штатном запуске из `/opt`.
- восемь старых неуправляемых runtime-файлов перенесены из importable
  `/opt/scripts` в root-only backup, не удалены. Installer теперь fail-closed
  при любом файле вне закрытого managed script set.

Матрица A–Z: `PHASE-66-ACCEPTANCE.md`. Строгий requirement-by-requirement
completion-аудит и оставшиеся live-гейты: `PHASE-66-COMPLETION-AUDIT.md`.

## Live-тесты, которые не выполнялись

- controlled Funnel, gateway, Tailscale, model или HA outage;
- live reminder/daily report delivery после новой scheduler migration;
- фактическое wake-from-sleep с новым динамическим one-shot trigger;
- live HA action/recovery/onboarding write;
- device toggle, relay stress test, HA restart, reboot или update.

## Оставшиеся ограничения

1. O/P считаются source/runtime-qualified, но controlled-live outage proof
   отсутствует по явному ограничению владельца.
2. Alice speech response не имеет end-to-end playback readback; поэтому
   deferred result delivery честно остаётся `delivery_unknown`.
3. Live delivery мигрированного scheduler/reminder/report ещё не
   квалифицирована.
4. Динамическая синхронизация Windows wake source/runtime-qualified, но
   физическое пробуждение из sleep ещё не наблюдалось.
5. HA Assist и Vision — только спроектированные optional transports.
6. Safe maintenance требует clean repository; текущий большой dirty tree нельзя
   выдавать за isolated candidate.

## Команды владельца для безопасной проверки

Только read-only/offline:

```bash
cd /root/Jarvis/home-butler
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 tests/evaluate_model.py
./scripts/no_cloud_audit.py
python3 /opt/home-butler/scripts/alice_skill_health.py --check-status
runuser -u homebutler -- python3 /opt/home-butler/scripts/persistent_scheduler.py --status
runuser -u homebutler -- python3 /opt/home-butler/scripts/device_onboarding.py --show
systemctl --no-pager --full status home-butler-local-chat.service
```

Из Windows в домашней сети:

```powershell
Invoke-WebRequest -UseBasicParsing http://192.168.1.175:8780/
Get-ScheduledTaskInfo -TaskName 'Home Butler LAN Forwarding'
```

В local chat или навыке владельца можно говорить обычными фразами:

- `Есть новые устройства?`
- после вопроса модели: `Он находится в спальне.`
- после готового proposal: `Подтверждаю предложение для Комнатный датчик.`
- `Напомни завтра в восемь заказать таблетки для посудомойки.`
- `С завтрашнего дня ежедневный отчёт в 11:40.`
- `Покажи мои напоминания.`
- `Отмени напоминание про таблетки.`
- `Не сообщай о Wi-Fi-сбоях короче минуты.`
- `Почему ты перезагрузил интеграцию LocalTuya?`

Onboarding approval подтверждает только proposal и ничего не записывает в Home
Assistant: применение configuration plan остаётся отдельным staged-действием.
Scheduler-фразы меняют persistent TaskSpec, но реальная доставка ближайшего
отчёта и физическое wake всё ещё должны быть подтверждены наступившим событием.

Runtime-state commands намеренно запускаются от `homebutler`: scheduler DB
и onboarding queue принадлежат этому service account. Запуск source-копии
от root может прочитать не тот home/state и не используется как proof.

Controlled outage test всё ещё требует отдельного явного разрешения с
перечислением exact services и ожидаемого downtime. Preflight, exact unit scope
и rollback: `PHASE-66-DEPLOYMENT-RUNBOOK.md`.
