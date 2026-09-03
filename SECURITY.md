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
Inventory имеет режим `0600`, содержит только metadata и не хранит state,
availability, value или timestamps. Физические representation связываются
только по точному registry device ID, сохранённому как SHA-256 hash; имя,
модель и комната не являются identity.

Host формирует закрытый `IntentFrame` и кандидатов. Модель видит только
безопасные labels и turn-local refs (`r1`, `r2`, ...), а вернуть может только
эти refs или clarification. Entity/device/service/capability IDs от модели
отклоняются. Ответ о доме строится только после свежего GET и создания
`ReadReceipt`.

Названия и состояния из HA считаются недоверенными данными. Они проходят
ограничение длины, символов и secret/prompt-injection фильтр. Недоступные,
отсутствующие и redacted значения нельзя заменять догадками.

Развёртывание и перезапуск сервисов требуют отдельного явного подтверждения
владельца. Safety tag Stage 70 позволяет восстановить состояние до purge.
