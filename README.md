# Jarvis / Home Butler

Локальный русскоязычный помощник для чтения текущих состояний Home Assistant.
Stage 71 задаёт закрытый семантический контракт и достоверное read-only чтение.
Управление, Hermes gateway, optional MCP transport, learning, recovery,
scheduler, reminders и persistent dialog memory отсутствуют.

## Единственный путь реплики

`Web или Alice → owner_chat.py → bounded_ha_agent.py →
home_assistant_inventory.py/home_assistant_mcp.py → home_assistant_read.py →
ReadReceipt → ответ`

- `home_assistant_inventory.py` — единственный metadata-only HomeGraph для
  физических устройств, logical entities, комнат и integration bindings;
- `home_assistant_mcp.py` — единственный host-only resolver; MCP transport в
  production отсутствует;
- `home_assistant_read.py` — только HTTP GET `/api/` и `/api/states`;
- `bounded_ha_agent.py` — закрытые `IntentFrame`/`ReadReceipt`, ephemeral
  session focus и один grounded renderer; локальная модель работает без tools;
- Web и Alice вызывают один и тот же `owner_chat`.

Обычная команда управления не исполняется. Помощник сообщает, что управление
отключено, и при возможности показывает свежее прочитанное состояние. Запуск
роботов-пылесосов невозможен: в production нет HA service-call/POST adapter.

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

Live acceptance читает HA, но не меняет его. Отчёт Stage 71:
[`reports/STAGE-71-SEMANTIC-CONTRACT-2026-09-03.md`](reports/STAGE-71-SEMANTIC-CONTRACT-2026-09-03.md).
Предыдущий аудит:
[`reports/STAGE-69-LIVE-AUDIT-2026-09-01.md`](reports/STAGE-69-LIVE-AUDIT-2026-09-01.md).

Состав и границы описаны в `ARCHITECTURE.md`, текущая задача — в
`CURRENT-GOAL.md`, правила разработки — в `AGENTS.md`.
