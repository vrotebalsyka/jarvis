# Home Butler

Локальный ИИ-дворецкий для наблюдения за Ubuntu/WSL и Home Assistant. Все
модельные обращения идут к локальной модели `home-butler`; облачные API и
произвольный shell отключены. В личном чате разрешено строгое управление
только сущностями `switch`, `light` и `button` по явной команде владельца.

## Личный диалог с Home Butler

Откройте обычный терминал Ubuntu и выполните:

```bash
cd /root/Jarvis/home-butler
./talk-to-home-butler.sh
```

Owner-chat проверяет локальный Ollama endpoint и запускает модель с коротким
жёстким профилем Home Butler. Запросы маршрутизируются детерминированно:

- «подключись к HAOS», `Home Assistant`, `датчик`, `Tuya` — модель обязана
  выбрать `ha_get_snapshot`; результат сверяется и выводится без свободного
  пересказа;
- «включи/выключи `switch.entity_id`/`light.entity_id`», «нажми
  `button.entity_id`» — модель
  обязана вернуть точный `ha_control_entity`; оболочка допускает только
  фиксированный service path и затем повторно читает состояние;
- «какие ресурсы/GPU/CPU» — факты читаются из текущего `/api/ps`;
- «проверь компьютер» — запускается read-only health pipeline;
- остальные сообщения идут в обычный локальный диалог с небольшой памятью
  текущей сессии.

Токен уже настроен: отправлять его модели или вставлять в чат не нужно. Выход —
`Ctrl+D` или `/exit`. Быстрые команды: `/ha`, `/ресурсы`, `/health`,
`/инциденты`, `/голос`, `/help`.

Один вопрос без интерактивного режима:

```bash
./talk-to-home-butler.sh --oneshot "Что сейчас известно о Home Assistant?"
```

Не используйте для личного диалога `ollama run home-butler` и старый
`hermes chat`: первый не получает проверенные данные, а общий Hermes prompt
слишком велик для этой 2B-модели и ухудшает соблюдение роли. Owner-chat оставляет
Hermes gateway для фонового сервиса, но личные вопросы направляет через более
короткую fail-closed оболочку.

## Текущее состояние

> **Live-control заблокирован результатами приёмки 1 сентября 2026.** Команда
> выключить свет в ванной выбрала и выключила свет в кабинете, после чего
> модель сообщила подтверждённый успех. Изменение возвращено и проверено
> отдельным GET. Перехваченный dry-run также показал, что текущий catalog
> допускает `vacuum.start`, несмотря на policy проекта. До исправления этих
> дефектов production-модель должна оставаться read-only. Подробности:
> [`reports/STAGE-69-LIVE-AUDIT-2026-09-01.md`](reports/STAGE-69-LIVE-AUDIT-2026-09-01.md).

- Windows Ollama `0.32.5` установлен в `H:\Ollama`, модели находятся в
  `H:\OllamaModels`. Пользовательская задача `Home Butler Ollama GPU`
  отслеживает внутренний WSL vEthernet, проверяет Authenticode-подпись и
  запускает сервер только на его точном private-адресе. Linux Ollama остаётся
  CPU fallback на `127.0.0.1:11434`.
- `home-butler` основан на `qwen3.5:2b-q4_K_M`: 2.3B параметров, Q4_K_M,
  контекст 64K. На RX 6600 XT используется нативный Windows Vulkan;
  `size_vram == size == 2405810829` при 64K, то есть модель целиком в VRAM.
- Hermes Agent `0.19.1` установлен в изолированный root-owned runtime
  `/opt/home-butler`. System unit выполняется как выделенный непривилегированный
  пользователь `homebutler`; процессы Hermes и MCP имеют UID/GID 998.
- `home-butler.service` и `home-butler-heartbeat.timer` включены и активны.
  Windows-задача `Home Butler WSL Runtime` после входа владельца удерживает
  пользовательский WSL runtime активным и повторно запускается с пробуждением
  Windows в 12:58 перед ежедневным отчётом на Станцию Макс в 13:00; это
  позволяет systemd units реально продолжать работу после завершения команды
  `wsl.exe`. Heartbeat впервые
  запускается через минуту после старта WSL, затем каждые 10 минут с jitter до
  30 секунд, хранит состояние
  с правами `0600` и подавляет одинаковые уведомления в течение часа.
- В текущем Phase 66 working tree фиксированные 12:58/13:00 уже заменены одним
  persistent SQLite scheduler: 13:00 остаётся только начальным значением
  ежедневного TaskSpec, переносится обычной русской фразой, а systemd выполняет
  частый лёгкий tick. Supervisor и безопасный Windows wake-export читают одну
  базу. Эта миграция ещё не развёрнута в `/opt/home-butler`; подробности и
  доказательства находятся в `reports/PHASE-66-SCHEDULER.md`.
- В том же staged working tree обычные запросы локального чата и Алисы
  переведены на natural bounded tool loop. Он ищет physical device, читает
  только нужные semantic details и управляет через закрытый capability catalog
  с реальными enum/range, readback и отдельным R3 confirmation. Технические ID
  модели не раскрываются. Этот этап ещё не установлен в `/opt/home-butler`;
  migration note: `reports/PHASE-66-NATURAL-TOOL-LOOP.md`.
- `home-butler-startup-ha-check.timer` один раз после каждого старта WSL
  запускает модельный GET-only check: Ollama должна сама вызвать
  `ha_get_snapshot`, получить точный очищенный HA-факт и доказать отсутствие
  service calls. Этот check допускает CPU fallback; отдельный ручной
  `home-butler-ha-proof.service` по-прежнему требует полный GPU offload.
- Health pipeline состоит из read-only collector, закрытого JSON Schema для
  модели и детерминированного русского renderer. Свободный модельный текст не
  становится системным фактом.
- Home Assistant доступен по GET-only adapter на `192.168.1.127:8123`.
  Режим чтения охватывает все сущности из одного ограниченного
  `GET /api/states`; сырые атрибуты и чувствительные строки не передаются.
  Для чтения используется `ha_get_snapshot`. Личный чат также имеет
   `ha_control_entity`, ограниченный доменами `switch`, `light` и `button`;
   произвольные services отсутствуют.
- Новые, не-baseline отказы `sensor`/`binary_sensor` озвучиваются только на
  Яндекс Станции Макс после 120 секунд недоступности; минутный контур проверки
  удерживает доставку в окне 2–3 минуты. После принятой тревоги восстановление
  даёт второе сообщение. Все инциденты старше времени включения правила,
  startup baseline и короткие сбои остаются без звука.
- Сущности разных интеграций, относящиеся к одному физическому Tuya-устройству,
  объединяются по обезличенной стабильной identity. Поэтому отказ одного
  датчика не превращается в несколько одинаковых тревог.
- В рабочем дереве подготовлены диагностический и recovery-контуры только для
  проверенной автоматизации гардероба, а также отдельный пятиминутный контроль
  свежести периодической телеметрии. Он сначала учится обычной частоте отчётов
  каждого датчика и лишь затем отмечает действительно устаревшие данные;
  событийные датчики движения не считаются зависшими из-за тишины. Локальная модель видит
  очищенные факты и выбирает один из заранее разрешённых планов: наблюдать,
  дождаться облака, один раз повторить исходное действие, исправить helper или
  ограниченно перезагрузить интеграцию. Модель не формирует service path и
  параметры. Этап 54 развёрнут в staged-режиме: диагностический и freshness
  timers активны, а timers, способные выполнять recovery-действия, выключены до
  контролируемых испытаний.
- После каждого допустимого действия выполняется GET-проверка через 20, 40 и
  60 секунд. Неизвестная доставка не повторяется, повторы ограничены, а
  cooldown растёт до 30 минут. Причина, действие и результат попадают в
  приватную 24-часовую хронологию и в ежедневный отчёт в 13:00. Суточный отчёт
  называет каждое существенное событие, его длительность и результат, а через
  Алису можно свободно спросить, что ломалось, что восстановлено и какие
  устройства сейчас требуют внимания.
- Private inventory каждые 10 минут отдельно фиксирует проверенные возможности
  Tuya-интеграций. Установленная LocalTuya 5.2.5 умеет сама обновлять IP по
  `gwId` через UDP discovery. HA Core обновлён до 2026.7.4, Tuya Local — до
  2026.7.2; пять его config entries загружены, `setup_error=0`, а private
  inventory подтверждает `automatic_ip_recovery=true`. В этом upstream-релизе
  добавлены active Tuya LAN rediscovery и автоматическое обновление IP по
  стабильной identity. Подготовленный owner-only updater теперь остаётся
  только зафиксированным повторяемым maintenance/verification артефактом.
- `home-butler-ha-proof.service` выполняет fail-closed доказательство чтения: Ollama
  должна сама выбрать `ha_get_snapshot`, очищенный HA-факт возвращается модели
  в закрытой схеме, после чего результат сравнивается с ним точно. Любая
  подмена ID, значения, времени или source завершает unit ошибкой.
- `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY` и cloud fallback
  отсутствуют. В Hermes глобально выключены terminal, file, browser, cronjob,
  code execution, delegation, web, search и встроенный HA toolset.

## Сервисы

Использован system-scope unit с `User=homebutler`, а не user-manager unit. Для
WSL это даёт надёжный запуск вместе с systemd и не делает агент root-процессом.
Поскольку дистрибутив WSL зарегистрирован для конкретного Windows-пользователя,
Windows-часть запускается после его входа в систему, а не до экрана входа.
Задачи имеют `RunLevel=Limited`; WSL keepalive выполняет только фиксированный
`/usr/bin/sleep infinity` от пользователя `homebutler`. Systemd запускает
Linux Ollama, gateway и timer; gateway до 45 секунд предпочитает GPU endpoint,
после чего разрешает только loopback CPU fallback.

```bash
systemctl status home-butler.service --no-pager
systemctl status home-butler-heartbeat.timer --no-pager
systemctl status home-butler-startup-ha-check.timer --no-pager
systemctl status home-butler-automation-diagnostics.timer --no-pager
systemctl status home-butler-automation-recovery.timer --no-pager
systemctl list-timers home-butler-heartbeat.timer --no-pager
journalctl -u home-butler.service --since today --no-pager
journalctl -u home-butler-heartbeat.service --since today --no-pager
journalctl -u home-butler-startup-ha-check.service --since today --no-pager
```

Ручной безопасный heartbeat:

```bash
systemctl start home-butler-heartbeat.service
journalctl -u home-butler-heartbeat.service -n 40 --no-pager
```

Проверяемый пример использования HA самой Ollama-моделью:

```bash
systemctl start home-butler-ha-proof.service
journalctl -u home-butler-ha-proof.service -n 60 --no-pager -o cat
```

Unit успешен только если в журнале одновременно есть `verified: true`, вызов
`ha_get_snapshot`, точный HA-факт, `service_calls: 0` и `fully_on_gpu: true`.

## Проверки разработки

Recovery planner использует декларативный реестр
`scripts/recovery_playbook_registry.py`. Его универсальный executor остаётся
staged/dry-run, пока конкретный playbook не прошёл controlled-live
квалификацию и отдельное разрешение владельца. Успешные offline-тесты сами по
себе не включают recovery timer. См.
`reports/PHASE-66-RECOVERY-PLAYBOOKS.md`.

Новые physical devices попадают в приватную read-only onboarding queue.
Модель видит безопасный `ha_get_onboarding_queue`, задаёт только отсутствующие
вопросы и готовит proposal; ни один HA config plan не выполняется без exact
owner approval и отдельной live qualification. См.
`reports/PHASE-66-DEVICE-ONBOARDING.md`.

Простые изменения поведения задаются обычной фразой и сохраняются только через
закрытые `behavior_get` / `behavior_set` / `behavior_reset`: например,
«Не сообщай о Wi-Fi-сбоях короче минуты». Настройки структурированы в Memory
Store и не могут включить shell, произвольный HA service call, отключить
verification/cooldown или изменить R3 policy. Свободный текст
`HOME-BUTLER-INSTRUCTIONS.md` остаётся справочным и не добавляется в system
prompt.

Для улучшений кода production-модель может создать только структурированный
`ChangeProposal` через `change_proposal_create`. Свободная запись в
`proposals/settings` запрещена; patch, тесты, точное approval, deployment,
health verification и rollback доступны только отдельному вручную запускаемому
`maintenance_worker.py`. Сам proposal или approval production не меняют. См.
`reports/PHASE-66-SAFE-MAINTENANCE.md`.

Каждый обработанный turn Alice/local chat после source deployment получает
secret-safe trace в общей Memory Store: route/profile/model, token counts,
context/memory IDs, tool latency, policy/action/verification и итог. Текст
запросов, tool arguments/results, IP/MAC и credentials не журналируются. См.
`reports/PHASE-66-OBSERVABILITY.md`.

Если локальная модель не успевает до deadline Алисы, gateway создаёт
сохраняемый `ActiveGoal` и возвращает короткий ID. Результат добавляется к
следующему обращению и доступен по фразе `статус задачи <ID>`. После restart
возобновляются только разговорные/read-only задачи; команда устройству никогда
не переигрывается. См. `reports/PHASE-66-DEFERRED-ALICE-TASKS.md`.

Опциональный transport Home Assistant Assist исследован, но не установлен:
он должен подключаться к тому же `owner_chat`/Memory Store/policy engine, а не
создавать второй мозг. Vision остаётся feature-disabled. См.
`reports/PHASE-66-OPTIONAL-TRANSPORTS.md`.

Итоговая source-квалификация и честно оставленные live-границы:
`reports/PHASE-66-RESULT.md`; матрица обязательных тестов A–Z:
`reports/PHASE-66-ACCEPTANCE.md`.

```bash
cd /root/Jarvis/home-butler
python3 scripts/ollama_endpoint.py
./scripts/no_cloud_audit.py
./scripts/home_assistant_read.py snapshot | jq .
./scripts/local-health-check.sh | ./scripts/health_report.py
python3 tests/evaluate_model.py | jq .
python3 -m unittest discover -s tests -p 'test_*.py' -v
RUN_LIVE_OLLAMA=1 python3 -m unittest discover -s tests \
  -p 'test_health_report_live.py' -v
```

## Границы безопасности

Команда управления должна содержать точный entity ID. Разрешены только
`switch.turn_on`, `switch.turn_off`, `switch.toggle`, `light.turn_on`,
`light.turn_off`, `light.toggle` и `button.press`. Дополнительные параметры
service data запрещены. После switch/light-действия новое состояние обязательно
подтверждается GET; для stateless button подтверждается приём POST и выполняется
контрольный GET без заявления о физическом результате.

Автовосстановление автоматизации не является произвольным управлением. Оно
работает только для зафиксированной связки «датчик движения — helper — реле 2
гардероба», не использует `toggle`, делает не больше одного точного повтора
исходного `turn_on`/`turn_off`, проверяет сохранённое намерение и текущее
состояние, а после неизвестной доставки прекращает повторы. Замки, ворота,
сигнализация, климат, произвольные automations/scripts и параметры модели в HA
не передаются.

Windows GPU backend слушает не LAN и не wildcard, а только текущий внутренний
WSL vEthernet. Endpoint guard сверяет адрес с default gateway WSL и при его
смене использует только loopback CPU fallback. Windows supervisor обновляет
точную привязку после смены vEthernet без публикации на `0.0.0.0`.

Установщик Ollama создал широкое inbound Hyper-V firewall-правило
`Ollama 11434 Inbound`. Сокет всё равно привязан только к внутреннему адресу,
но удаление или сужение правила требует отдельного разрешения владельца.

Токен Home Assistant хранится только в root-owned файле `0600` и передаётся
units через `LoadCredential`. Не выводите его, не копируйте в `/opt` и не
добавляйте secrets в Git.

Windows-задачи автозапуска:

```powershell
Get-ScheduledTask -TaskName 'Home Butler WSL Runtime','Home Butler Ollama GPU'
Get-ScheduledTaskInfo -TaskName 'Home Butler WSL Runtime'
Get-ScheduledTaskInfo -TaskName 'Home Butler Ollama GPU'
```

Одноразовый bootstrap независимого восстановления HA Container выполняется
владельцем интерактивно, чтобы пароль `victor` не попадал в Codex или файлы:

```bash
cd /root/Jarvis/home-butler
./scripts/bootstrap-ha-recovery-victor.sh
```

После успешной команды автоматический timer всё ещё остаётся выключенным до
отдельного доказательства `healthy_no_action` при работающем Home Assistant.
