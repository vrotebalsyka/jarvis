# Security Policy

Production работает только на чтение.

- `home_assistant_read.py` допускает HTTP GET только к `/api/` и `/api/states`.
- Inventory дополнительно читает только entity/device/area registry list через
  аутентифицированный WebSocket; подписок и команд изменения нет.
- В коде отсутствуют HA service-call и POST adapters.
- Локальная модель не получает shell, filesystem, browser, scheduler, memory,
  recovery или Home Assistant control tools.
- Обычные фразы управления не исполняются.

Токен HA хранится только в root-owned systemd credential с закрытыми правами.
Секреты запрещено помещать в Git, Markdown, inventory, ответы или журналы.
Inventory имеет режим `0600`; стабильная физическая identity представлена
SHA-256 hash, а технические IDs не показываются владельцу.

Названия и состояния из HA считаются недоверенными данными. Они проходят
ограничение длины, символов и secret/prompt-injection фильтр. Недоступные,
отсутствующие и redacted значения нельзя заменять догадками.

Развёртывание и перезапуск сервисов требуют отдельного явного подтверждения
владельца. Safety tag Stage 70 позволяет восстановить состояние до purge.
