# Jarvis / Home Butler

Локальный русскоязычный помощник для достоверного чтения Home Assistant и
Stage 72 shadow action planning. Реальное управление, Hermes gateway, optional
MCP transport, learning, recovery, scheduler, reminders и persistent dialog
memory отсутствуют.

Stage 72 активирован в production в SHADOW mode 2026-09-05 из
`7eb0a9fd8b03cf481e58aff06b78830b6658a868` (safety tag
`stage72-complete-7eb0a9f`). Stage 73 не начат. Текущее состояние приёмки
после promotion и cleanup указано в `CURRENT-GOAL.md`.
Read cleanup разделяет registry и inferred rooms; `ReadReceipt` содержит только
registry area. Разбор исходных failures и повторная приёмка также сохранены там.

## Единственный путь реплики

`Web или Alice → owner_chat.py → bounded_ha_agent.py →
home_assistant_inventory.py/home_assistant_mcp.py → home_assistant_read.py →
ReadReceipt или sealed shadow ActionPlan → ответ`

- `home_assistant_inventory.py` — единственный metadata-only HomeGraph для
  физических устройств, logical entities, комнат и integration bindings;
- `home_assistant_mcp.py` — единственный host-only resolver; MCP transport в
  production отсутствует;
- `home_assistant_read.py` — только HTTP GET `/api/` и `/api/states`;
- `bounded_ha_agent.py` — закрытые `IntentFrame`/`ReadReceipt`, ephemeral
  session focus и один action/read path; локальная модель работает без tools;
- `shadow_action_policy.py` — единственный deny-by-default registry и HMAC-
  sealed, неисполняемый ActionPlan;
- Web и Alice вызывают один и тот же `owner_chat`.

Команда управления не исполняется. Для однозначных light/switch turn_on/off
может быть построен только shadow-план; vacuum/button/appliance/lock/climate/
script и остальные действия hard-deny. В production нет HA service-call/POST
adapter.

## Установка

```bash
sudo ./scripts/install-home-butler-service.sh
```

Команда синхронизирует закрытый набор runtime-файлов и systemd units, но не
перезапускает сервисы. Активация выполняется отдельно и только после явного
решения владельца:

```bash
sudo ./scripts/install-home-butler-service.sh --activate
```

Токен HA хранится только как systemd credential
`home-assistant.token`; конфигурация использует `read_all` и `*`.

## Проверка

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

Stage 72 live acceptance использует production metadata и локальную модель, но
физически блокирует HA POST. Evidence report:
[`reports/STAGE-72-SHADOW-ACTION-PLANNING-2026-09-03.md`](reports/STAGE-72-SHADOW-ACTION-PLANNING-2026-09-03.md).
Историческая независимая real-home acceptance до correction — `FAIL` (56/60; safety и latency
gates зелёные):
[`reports/STAGE-72-FINAL-REAL-HOME-ACCEPTANCE-2026-09-03.md`](reports/STAGE-72-FINAL-REAL-HOME-ACCEPTANCE-2026-09-03.md).
Stage 72 correction до promotion: real-home 60/60 и новый natural-language
corpus 100/100, без HA POST; deployment на момент этого отчёта не выполнялся:
[`reports/STAGE-72-CORRECTION-2026-09-04.md`](reports/STAGE-72-CORRECTION-2026-09-04.md).
Stage 72 room/type capability closeout: 42/42, 10 real targets,
`REAL_ROOM_TYPE_PLANS=21`, HA POST/service calls 0:
[`reports/STAGE-72-ROOM-TYPE-CLOSEOUT-2026-09-04.md`](reports/STAGE-72-ROOM-TYPE-CLOSEOUT-2026-09-04.md).
Отчёт Stage 71:
[`reports/STAGE-71-SEMANTIC-CONTRACT-2026-09-03.md`](reports/STAGE-71-SEMANTIC-CONTRACT-2026-09-03.md).
Предыдущий аудит:
[`reports/STAGE-69-LIVE-AUDIT-2026-09-01.md`](reports/STAGE-69-LIVE-AUDIT-2026-09-01.md).

Состав и границы описаны в `ARCHITECTURE.md`, текущая задача — в
`CURRENT-GOAL.md`, правила разработки — в `AGENTS.md`.
