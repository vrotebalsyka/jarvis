# Phase 66 / этап 7 — естественный bounded tool loop

Дата source-квалификации: 2026-08-24.

Статус: реализовано, offline-квалифицировано и развёрнуто в
`/opt/home-butler`. Source/runtime hashes совпадают для bounded agent,
capability catalog, общего `owner_chat` и Alice gateway; local chat и Alice
services активны. Это не добавляет live action qualification: Home
Assistant и устройства для теста не переключались.

## Что изменилось

- Обычная реплика локального чата и Алисы сначала попадает в общий
  `owner_chat.answer_natural`, а не в меню из regex-команд. Slash-команды,
  secret detection, scheduler, incidents и системные проверки сохранили
  детерминированные границы и compatibility fallback.
- `bounded_ha_agent.py` отдельно классифицирует только текущую доверенную
  реплику владельца. History и Memory могут разрешить «он/его/там», но не могут
  дать право на действие. HA names, attributes и tool results всегда остаются
  недоверенными данными.
- Для одного запроса разрешено ограниченное число read tools, один action-plan
  и одна корректирующая попытка финального текста. Бесконечного цикла нет.
  Полный snapshot не предлагается модели для обычного запроса об устройстве.
- `capability_catalog.py` строит действия из существующих HA control facts и
  DeviceGraph. Модель видит opaque capability ID, реальные enum options,
  number range/step, availability, risk и verification, но не service path и
  не приватный `entity_id`.
- Обычный R2 action требует явной текущей просьбы. Один plan call может
  содержать максимум два заранее полностью проверенных шага одного физического
  прибора: например, выбрать реальную программу посудомойки, затем нажать её
  Start. Ошибка второго шага не вызывает скрытого повтора.
- R3 action останавливается до side effect и возвращает точную фразу отдельного
  подтверждения. Следующая реплика принимается только при явном
  «подтверждаю/разрешаю» сразу после этой точной фразы; иначе подтверждение
  отклоняется.
- Финальный ответ не может раскрыть hashes, capability IDs, entity IDs,
  private IP, token/secret, Markdown или новые числовые HA-факты. Один
  отклонённый текст можно лишь переформулировать по уже полученным tool results;
  повторного действия при этом нет.

## Coreference и ответы

Проверен диалог об одном physical device:

1. «Что с роботом Андреем?»
2. «А батарея у него?»
3. «Тогда отправь его на базу.»

Поиск, details и capability относятся к одному opaque physical device. Модель
не просит произнести entity ID. Ответ на действие содержит прибор, функцию,
результат и способ verification.

## Проверки

- targeted natural/capability/facade tests: 22 — OK;
- широкий regression owner chat/local chat/Alice/MCP/control/runtime: 168 — OK;
- итоговый полный offline suite: 606 — OK, 1 skipped;
- `tests/evaluate_model.py`: 5/5 PASS, safe tool selection — ровно
  `ha_get_snapshot`, runtime context — 8192, модель целиком в VRAM;
- read-only source proof на сохранённом DeviceGraph и свежем GET показал:
  «Робот Андрей сейчас находится в док-станции (заряжается)… заряд 93%».
  Service action не вызывался.

Initial cold read-only proof занял 136,4 секунды: dialogue profile загрузил 4B-модель и
выполнил classifier, find, details и validated final. Это честно считается
source proof корректности, а не voice latency proof. Последующая warm
квалификация общего Alice facade дала 30/30 model-completed, fallback 0,
P50 1.924 s и P95 2.981 s при budget 3.2 s. Controlled outage O/P в это
измерение не входил.

## Миграция и rollback

- Новый default локального транспорта: `owner_chat.answer_natural`.
- Новый default Alice transport: `natural_voice_answer`; прежний
  `fast_model_answer` остаётся fallback.
- Старый `owner_chat.answer` не удалён и сохраняет совместимость CLI/tests.
- Для rollback достаточно вернуть defaults обоих transport к прежним
  answerers; HA control adapter, secret storage и DeviceGraph не меняются.

## Что пока не доказано

- живые action calls намеренно не выполнялись;
- warm latency общего facade измерена, но controlled route outage и
  multi-turn coreference через физическую Станцию Макс не квалифицированы;
- составной action проверен fixture-адаптером, а не реальной посудомойкой;
- отдельное R3 подтверждение проверено без side effect.
