# Current Goal — Stage 70 PURGE

Статус: `READY_FOR_DEPLOYMENT`. Код, repository gates и live read-only
acceptance пройдены. Активация очищенного runtime ожидает отдельного разрешения
владельца на остановку старых units и перезапуск необходимых сервисов.

## Обязательный отчёт

### FILES_DELETED

- 280 tracked files удалено.
- Удалены learning/control/proof/recovery/scheduler/memory/onboarding,
  diagnostics/maintenance/stress layers, старые corpora, units и документы.
- Git history и safety tag `pre-stage70-cleanup-4ebbff4` остаются архивом.

### FILES_REDUCED

- 20 существовавших файлов стали короче.
- Крупнейшие сокращения: `owner_chat.py`, `bounded_ha_agent.py`,
  `home_assistant_inventory.py`, `home_assistant_mcp.py`,
  `home_assistant_read.py`, `alice_skill_gateway.py`,
  `alice_skill_health.py`, `local_chat_gateway.py`, installer и Hermes config.

### LINES_BEFORE

- Все tracked files: 83 009 строк.
- Production Python: 74 modules, 43 449 строк.
- Tests Python: 88 файлов, 23 353 строки.

### LINES_AFTER

- Все tracked files: 6 744 строки.
- Production Python: 14 modules, 4 594 строки.
- Tests Python: 7 файлов, 556 строк.

### PRODUCTION_IMPORT_GRAPH_BEFORE

- Web: 43 reachable production modules.
- Alice: 38 reachable production modules.
- Union: 43 modules, включая несколько routers, graphs, renderers, action
  planners, learning, memory, scheduler, recovery и incident layers.

### PRODUCTION_IMPORT_GRAPH_AFTER

```text
local_chat_gateway ─┐
                    ├→ owner_chat → bounded_ha_agent
alice_skill_gateway ┘                    │
                                         ├→ home_assistant_mcp
                                         │    └→ home_assistant_inventory
                                         └→ home_assistant_read
```

- Web: 9 reachable production modules.
- Alice: 9 reachable production modules.
- Union: 10 modules, включая transports и shared sanitizer/policy/endpoint.
- Один inventory, resolver, fresh-read adapter и grounded renderer.

### TESTS_REMOVED_AS_OBSOLETE

- 90 legacy test artifacts удалено, включая 88 старых Python tests/evaluators.
- Вместо них оставлено 7 focused Python files для sanitization, identity,
  inventory, resolver, fresh state, ambiguity, rendering, transports,
  secret leakage и live acceptance.
- Полный оставшийся suite: 22/22 PASS в Windows Python и Ubuntu/WSL Python.

### LIVE_READ_ACCEPTANCE

- 19 строк Stage 69, 20 target groups (ванная и туалет отдельно).
- 30 физических registry identities прочитано через 100 естественных русских
  фраз на настоящем production path.
- `wrong_device=0`, `invented_facts=0`, `lost_available_values=0`.
- `technical_leaks=0`, `ha_service_calls=0`; роботы не запускались.

После активации требуется подтвердить закрытый набор systemd units, свежий
inventory, Web, Alice local/public probes и повторить live acceptance. После
Stage 70 остановиться; Stage 71 не начинать.
