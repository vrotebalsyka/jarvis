# Stage 68 — готовность обучающего корпуса

Дата квалификации: 28 августа 2026 года.

## Результат

Обучающий контур готов к накоплению проверенных данных, но обучение весов сейчас не требуется. Текущая `qwen3.5:4b-q4_K_M` с DeviceKnowledgeProfile, компактным retrieval и детерминированной проверкой дала 100% на обязательном read-only evaluator. Веса модели не изменялись, LoRA/QLoRA не запускалась, новые модели и base weights не скачивались.

## Корпус

- Создано примеров: **160**.
- Прошло детерминированную проверку: **150**.
- Отклонено: **10**; они находятся только в `training/rejected/` и `reports/learning-rejected/`.
- Андрей: **75** validated examples — 50 positive и 25 negative.
- Посудомойка: **75** validated examples — 50 positive и 25 negative.
- Охват: **2 реальных физических устройства**.
- Production weights modified: **false**.

## Категории validated corpus

| Категория | Примеров |
|---|---:|
| READ | 12 |
| COREFERENCE | 12 |
| ACTION_SELECTION | 12 |
| DIAGNOSTICS | 27 |
| MAINTENANCE | 12 |
| CONDITIONAL_AVAILABILITY | 10 |
| UNKNOWN_FACT | 20 |
| PARTIAL_FAILURE | 20 |
| ACTION_VERIFICATION | 25 |
| **Всего** | **150** |

## Валидация

В trusted corpus допускаются только примеры, построенные из HA tool result, DeviceGraph/inventory, registry metadata, подтверждённых receipts/transitions, owner corrections, integration metadata или очищенных fixtures. Ответ модели не является обучающим фактом.

Validator блокирует:

- числа и состояния, которых нет в source facts;
- выдуманные причины недоступности;
- подмену отказа отдельной функции отказом всего прибора;
- `accepted` как `verified`;
- несуществующую capability или action без verification rule;
- `entity_id`, IP, MAC, token и config-entry ID в пользовательском ответе;
- попадание rejected examples в production learning.

## Evals

- Обязательный Stage 68 evaluator: **8/8**.
- Реальный Yandex JSON E2E: **2/2**, включая follow-up «А батарея?» без повторного имени.
- Action calls в read-only qualification: **0**.
- Запрещённые инструменты: **0**.
- Максимальная задержка прогретого фактического Alice E2E: **3,973 с**; оба ответа получены напрямую.

## Решение по LoRA

**LoRA сейчас не нужна.** Retrieval + grounding + runtime verifier уже достигают целевой точности на обязательном наборе. Возвращаться к LoRA имеет смысл только после накопления существенно большего multi-device корпуса и доказанного, повторяемого провала модели, который нельзя исправить профилем, retrieval или validator. Для будущего обучения понадобится отдельная trainable base model; quantized Ollama GGUF напрямую не обучается.
