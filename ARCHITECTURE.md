# Architecture

## Production graph

```text
local_chat_gateway ─┐
                    ├→ owner_chat → bounded_ha_agent
alice_skill_gateway ┘                    │
                                         ├→ home_assistant_mcp (resolver)
                                         │        │
                                         │        └→ inventory.json
                                         └→ home_assistant_read → fresh GET /api/states

home_assistant_inventory → HA registries + fresh GET → inventory.json
```

`bounded_ha_agent.respond()` — единственная точка обычного диалога после
transport. Он загружает inventory, делает свежий snapshot, находит физическое
устройство и формирует ответ одним renderer. Модель используется только для
структурированного read intent/general phrasing и всегда вызывается без tools.

## Границы

- `home_assistant_inventory.py`: один private physical-device graph.
- `home_assistant_mcp.py`: compact index, find devices, current details.
- `home_assistant_read.py`: текущие очищенные состояния, GET-only.
- `owner_chat.py`: тонкая conversational facade без compatibility routes.
- Web/Alice: только аутентификация, сессия и transport deadlines.

Systemd содержит runtime, local chat, inventory refresh, Alice skill,
Tailscale tunnel и probe-only Alice health. Recovery timers отсутствуют.
