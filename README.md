# Jarvis / Home Butler

Локальный русскоязычный помощник для чтения текущих состояний Home Assistant.
Stage 70 намеренно удалил управление, обучение, recovery, scheduler, reminders,
долгую память, onboarding и параллельные диалоговые маршруты.

## Единственный путь реплики

`Web или Alice → owner_chat.py → bounded_ha_agent.py →
home_assistant_inventory.py/home_assistant_mcp.py → home_assistant_read.py → ответ`

- `home_assistant_inventory.py` — единственный граф физических устройств;
- `home_assistant_mcp.py` — единственный resolver и три read-only операции:
  compact index, поиск устройств, свежие детали;
- `home_assistant_read.py` — только HTTP GET `/api/` и `/api/states`;
- `bounded_ha_agent.py` — один grounded renderer и локальная модель без tools;
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

Live acceptance читает HA, но не меняет его. Последний доочисточный аудит:
[`reports/STAGE-69-LIVE-AUDIT-2026-09-01.md`](reports/STAGE-69-LIVE-AUDIT-2026-09-01.md).

Состав и границы описаны в `ARCHITECTURE.md`, текущая задача — в
`CURRENT-GOAL.md`, правила разработки — в `AGENTS.md`.
