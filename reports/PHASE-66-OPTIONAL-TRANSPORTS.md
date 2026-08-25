# Phase 66 / этап 10 — optional Home Assistant Assist и Vision

Дата исследования: 2026-08-24. Это архитектурная квалификация, не deployment.

## Home Assistant Assist

Официальный совместимый путь существует:

- [Conversation API](https://developers.home-assistant.io/docs/intent_conversation_api/)
  принимает текст через REST/WebSocket, возвращает `conversation_id`, speech,
  success/failed targets и тип результата;
- [LLM API](https://developers.home-assistant.io/docs/core/llm/) предоставляет
  встроенный Assist API только для exposed entities и без административных
  действий; зарегистрированные LLM API могут публиковаться через MCP;
- встроенный Assist MCP доступен как `/api/mcp/assist`, а configured API — как
  `/api/mcp` после настройки официальной MCP Server integration;
- [Assist Pipeline](https://developers.home-assistant.io/docs/voice/pipelines/)
  объединяет wake word, STT, conversation и TTS и поддерживает
  `conversation_id` через WebSocket.

Рекомендуемая следующая стадия — тонкий HA Conversation adapter к существующему
`owner_chat.answer_natural`, с той же Memory Store, capability catalog, policy
engine и readback. Это будет новый transport, а не вторая модель и не второй
agent core. До qualification он остаётся feature-disabled. Сначала нужны:

1. отдельный least-privilege HA user/credential;
2. exposed-entity policy;
3. read-only text round-trip;
4. owner/session mapping;
5. action test только через существующий bounded capability path;
6. rollback удалением adapter без изменения Memory Store.

Никакая integration, STT/TTS model или пакет не устанавливались.

## Optional Vision

Vision остаётся выключенным. Допустимая будущая оболочка принимает только
локально переданный JPEG/PNG ограниченного размера, удаляет metadata, не
загружает URL, считает весь распознанный текст untrusted data и возвращает
только evidence/confidence. Vision не получает action tools и не заменяет
DeviceGraph, entity metadata, states или logs. Новая multimodal model без
разрешения владельца не загружалась.

