# Security Policy

Production работает только на чтение на завершённом Stage 72. Ветка Stage 73
Phase A содержит canary adapter, но не имеет разрешения на реальные HA writes.
CONTROL_ENABLED=false и CANARY_LIVE_ENABLED=false по умолчанию; реальная
owner allowlist пока не создана. Phase B требует отдельного owner approval.

- `home_assistant_read.py` допускает HTTP GET только к `/api/` и `/api/states`.
- Inventory дополнительно читает только entity/device/area registry list через
  аутентифицированный WebSocket; подписок и команд изменения нет.
- Единственный canary write adapter ограничен четырьмя fixed light/switch
  turn_on/turn_off paths и принимает только host-sealed plan. Toggle запрещён.
  Он не установлен в production, не получает read token как action credential.
- Локальная модель не получает shell, filesystem, browser, scheduler, memory,
  recovery или Home Assistant control tools.
- В текущем production обычные фразы управления не исполняются. Единственный ActionPolicyRegistry
  допускает только sealed light/switch turn_on/turn_off shadow plans; опасные
  domains/actions hard-deny.

Токен HA хранится только в root-owned systemd credential с закрытыми правами.
Секреты запрещено помещать в Git, Markdown, inventory, ответы или журналы.
Inventory имеет режим `0600`, содержит только metadata и не хранит state,
availability, value или timestamps. Физические representation связываются
только по точному registry device ID, сохранённому как SHA-256 hash; имя,
модель и комната не являются identity.

Host формирует закрытый `IntentFrame` и кандидатов. Модель разбирает только
action/requested name/area/type/feature, не видит candidates и не выбирает
target. Entity/device/service/capability IDs от модели отклоняются.
Ответ о доме строится только после свежего GET и создания
`ReadReceipt`.

В готовом owner-facing ответе безопасные Unicode/emoji разрешены. Проверки
секретов и technical IDs выполняются над исходным текстом и дополнительной
NFKC-копией без supplementary/format/combining маскировки; сама копия не
возвращается владельцу и не переписывает значения. Невалидные суррогаты и
управляющие символы после штатной нормализации whitespace отклоняются.

Action scope повторно сверяется host-ом с комнатой, типом, именем и feature
resolved target. Равные candidates не передаются модели и всегда требуют
уточнения. ActionPlan содержит только host-internal target ref, safe label,
scope, allowlisted action/value и process-local HMAC seal; entity ID и service
path отсутствуют. Stage72 shadow trace безопасен для журнала и содержит
`service_calls=0`, `ha_post=0`. В Stage73 canary trace дополнительно отражает
фактический receipt и число попыток отправки; только independent verified
readback разрешает утверждать успех. Phase A блокирует реальные HA writes.

Отложенная проверка загружает только read credential и не выполняет POST.
Она требует исходный authentic sealed plan и durable receipt. Полярность,
target и allowlist не меняются; выявленный side effect сохраняет emergency OFF.
Pending result events изолированы по transport session; они не дают модели
новую authority. При потере сессии/перезапуске уведомление может потеряться,
но journal не позволяет повторить неизвестную доставку.

Владелец запретил менять registry rooms ради приёмки. Owner-confirmed room
остаётся отдельной owner metadata, не записывается в HA и не подменяет registry fact.

Названия и состояния из HA считаются недоверенными данными. Они проходят
ограничение длины, символов и secret/prompt-injection фильтр. Недоступные,
отсутствующие и redacted значения нельзя заменять догадками.

Развёртывание и перезапуск сервисов требуют отдельного явного подтверждения
владельца. Safety tag Stage 70 позволяет восстановить состояние до purge.
