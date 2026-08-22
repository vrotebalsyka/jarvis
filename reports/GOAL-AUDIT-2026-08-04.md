# Home Butler — аудит исходной цели

Дата живой проверки: 2026-08-04, часовой пояс Asia/Yekaterinburg.

Статус цели: **не завершена**. Основной оперативный контур работает, но ещё нет
живого доказательства full-dialog через физическую колонку и реального DHCP
IP-change/convergence после power-cycle.

## Матрица требований и доказательств

| Требование владельца | Текущее доказательство | Статус |
| --- | --- | --- |
| Самозапуск при старте ПК | Windows tasks `Home Butler Ollama GPU` и `Home Butler WSL Runtime` существуют; все основные systemd services/timers active; четыре проверенных долгоживущих процесса `NRestarts=0` | Подтверждено |
| Подключение к Home Assistant | Свежий GET: API доступен, 198 сущностей, 139 доступны, 59 недоступны; status `stale_data`, а не `api_unavailable` | Подтверждено |
| Чтение всех безопасных HA-состояний моделью | Санитайзер читает все домены, не выпускает attributes/secrets; model tool/fact proof покрыт тестами и ранее проходил live | Подтверждено |
| Быстрое обнаружение отказов | WebSocket monitor active; debounce 60 секунд; сейчас 50 открытых подтверждённых инцидентов, из них 14 новых warning | Подтверждено |
| Не считать каждую сущность отдельным устройством | Private inventory коррелировал 14 новых entity-инцидентов в одну platform-группу `xiaomi_miot`: 2 device identity, 14 сущностей, 0 unmapped | Подтверждено |
| Безопасно восстановить текущий Xiaomi MIoT outage | Две loaded config entry разведены по четырём device identity; проблемная entry имеет 27/27 unavailable, а все 14 новых кандидатов однозначно сгруппированы в один reload. Живой worker вернул `permission_required`, `service_calls=0`; реальный путь ограничен одним reload/час и GET-проверкой | Подготовлено, ждёт отдельного разрешения |
| Отслеживать будущую смену IP Xiaomi | Четыре registry identity имеют строгий формат `MAC-модель`; identifier немедленно хешируется, а MAC/IP остаются в приватном inventory. Live созданы 2 Xiaomi baseline bindings, integrity/FK-check прошли. Новый IP при unavailable даёт `ip_changed`, доступная entity на новом IP — `converged` | Подтверждено по live baseline и тестам; реальный drift ещё не произошёл |
| Автовосстановление LocalTuya после смены IP | LocalTuya 5.2.5 имеет stable-ID UDP auto-update; bounded worker допускает один reload и GET-check; один device recovery уже записан | Подтверждено по capability/action, без нового DHCP live event |
| Автовосстановление Tuya Local после смены IP | Core 2026.7.4, Tuya Local 2026.7.2, 5/5 entries loaded, setup_error=0, `automatic_ip_recovery=true`; upstream 2026.7.2 добавил active LAN rediscovery и auto-update IP ([release](https://github.com/make-all/tuya-local/releases/tag/2026.7.2)) | Подтверждено по live capability, реальный IP drift ещё не наблюдался |
| Проверять, что HA всегда активен | startup check, heartbeat, inventory, incident monitor, Core recovery и out-of-band timer active | Подтверждено |
| Безопасно восстанавливать HA | Независимый forced-command SSH вернул `status=healthy_no_action`; локальный Core restart разрешён только после check_config, частичного outage >5 минут, один раз/6 часов | Подтверждено без искусственного аварийного restart |
| GPU + CPU fallback | Windows listener закреплён на WSL private gateway; единый активный `home-butler` runner использует context 2048/keep-alive 24h и загружен 1,590,291,331/1,590,291,331 bytes в VRAM, `fully_on_gpu=true`; loopback CPU остаётся единственным fallback | Подтверждено |
| Включать/выключать свет и нажимать button | Только switch/light/button, точный entity, model-gated tool call, один fixed service path и GET-readback; natural alias коридора закреплён | Статически подтверждено; новый физический вызов без точной команды владельца не выполнялся |
| Критические сообщения через колонки | Обе разрешённые Yandex Station найдены и доступны; notifier active, dedupe/retry policy протестирована; текущих critical phase нет, поэтому TTS calls=0 | Готово, но live critical TTS не доказан |
| Свободный разговор с локальной моделью через Алису | Full-dialog gateway и ngrok tunnel active; приватный skill/user identity закреплён автоматически после настоящего запроса Яндекс Диалогов. Тестер получил model response, но live-журнал не содержит ни одного отдельного session от физической колонки: автомодерация пройдена, кнопка «Опубликовать» ещё не была нажата. Staged ротация сохраняет старый Webhook до валидного запроса по новому | Ожидается публикация и физическая колонка |
| Не использовать сценарий на каждую фразу | Целевая схема — один приватный Yandex Dialogs skill с `end_session=false`; старый fixed-route bridge `disabled/inactive` и оставлен только как ручной rollback | Подтверждено архитектурой и live state |
| Использовать существующие проекты | Применены/учтены Home Assistant, LocalTuya, Tuya Local, AlexxIT/YandexStation, Yandex Dialogs, ngrok; unsupported microphone interception отвергнут первичными источниками | Подтверждено |

## Живое состояние

- Все 11 проверенных основных services/timers — `active`.
- Старый сценарный `home-butler-voice-intent.service` намеренно переведён в
  `disabled/inactive`; установщик больше не возвращает его в автозапуск.
- `home-butler.service`, incident monitor, Alice gateway и tunnel работают от
  непривилегированного `homebutler`, `NRestarts=0`.
- Out-of-band HA host доступен только forced command; результат
  `healthy_no_action`.
- Tuya Local полностью исправлен после обновления: 5 loaded, 0 setup_error.
- 14 новых предупреждений принадлежат двум `xiaomi_miot` устройствам. Обе их
  config entry `loaded` и поддерживают unload/reload; private inventory
  подтвердил installed/latest `v1.1.4`. Upstream-путь config-entry reload для
  этой точной версии проверен. Read-only разбор показал две loaded entry: одна
  имеет 3/30 unavailable, другая 27/27; четыре device identity не пересекаются.
  Все 14 новых предупреждений принадлежат одной проблемной entry. Bounded path
  реализован, но `automatic_recovery_enabled=false`: рабочий unit возвращает
  `permission_required`, пока владелец отдельно не разрешит один reload.
- У Xiaomi state attributes не содержат LAN IP/MAC, а config-entry diagnostics
  недоступны. Registry identifier безопасно сводится к хешу и MAC, поэтому
  будущие DHCP-переезды теперь отслеживаются. Live inventory создал две stable
  Xiaomi baseline identity; обе принадлежат частично работающим устройствам.
  Ни одно из двух устройств проблемной entry в LAN сейчас не наблюдается,
  поэтому текущий outage нельзя называть сменой IP.
- Alice provisioning claim успешно обработан: skill/user identity закреплены,
  claim удалён, gateway работает в pinned mode. `home-butler-alice-finalize.path`
  остаётся enabled/active для безопасного восстановления состояния.
- Навык прошёл приватную автомодерацию, но остаётся черновиком до отдельного
  нажатия владельцем «Опубликовать». После тестов новый root-only Webhook
  staged без простоя: старый маршрут отвечает, а commit запрещён до валидного
  pinned request по новому маршруту.
- Все активные модельные пути приведены к единому GPU profile context 2048.
  Проверенный отдельный voice alias не используется: Windows/Radeon Ollama
  выгружал соседний runner, вызывая задержки 16–19 секунд. После унификации
  последующий голосовой ответ остаётся быстрым даже после HA/health/control
  проверок; живой результат — 1,747 секунды и `fully_on_gpu=true`.
- Голосовое чтение HA теперь имеет bounded proof: модель сама вызывает
  `ha_get_snapshot`, получает один точный очищенный факт, а ответ формируется из
  того же snapshot. Живой proof — 2,091–2,797 секунды; полный Yandex envelope —
  2,636 секунды, без service calls.
- Команда `/голос` теперь проверяет только целевой full-dialog контур: gateway
  active, tunnel active, mode pending, 2/2 разрешённые колонки доступны,
  отдельные сценарии отключены.

Полная регрессия: 245 тестов, 244 успешны, один live-тест намеренно пропущен без
явного opt-in.

## Что ещё необходимо для полного закрытия цели

1. Нажать «Опубликовать» у уже прошедшего автомодерацию приватного навыка и
   убедиться, что он доступен аккаунту физической колонки.
2. Выполнить живой разговор на колонке: две общие реплики, follow-up с памятью,
   HA read и одна точная разрешённая команда света с readback.
3. После живого теста заменить Webhook URL на staged новый адрес из root-only
   файла, получить authenticated marker и выполнить bounded commit ротации.
4. Дождаться естественной смены IP Tuya после отключения питания либо отдельно
   разрешить контролируемый power-cycle. Доказать `ip_changed -> converged` без
   ручного изменения HA.
5. Владелец должен отдельно разрешить один bounded config-entry reload для
   уже точно определённой проблемной `xiaomi_miot` записи. До разрешения Home
   Butler только обнаруживает, группирует и возвращает `permission_required`;
   live dry-run доказал 14 кандидатов, 0 service calls и неизменный ledger.
