# Phase 66 — сохраняемые медленные задачи Алисы

Дата source-квалификации: 2026-08-24.

## Исправленный дефект

Раньше `BoundedTurnExecutor` возвращал до Yandex deadline честную фразу о
продолжающейся проверке, но работа существовала только как `Future` в памяти
процесса. Это не было persistent task: при перезапуске результат терялся, а
владелец не получал task ID.

Теперь превышение voice budget создаёт namespaced `ActiveGoal` в уже
существующем owner-scoped Memory Store. Второй scheduler или отдельная база не
создавались.

## Поведение

- ответ Алисы содержит короткий ID задачи;
- background worker сохраняет фактический ответ в goal;
- один сохранённый результат добавляется к следующему обычному turn;
- результат остаётся доступен по фразе `статус задачи <ID>`;
- после restart возобновляются только `general`, `home_assistant` и
  `incidents`;
- `home_assistant_control` после restart никогда не переигрывается: goal
  получает `blocked`, а результат — `delivery_unknown`;
- если Memory Store недоступен, gateway не называет timeout «сохраняемой
  задачей» и возвращает disposition `timeout_unpersisted`.

Webhook не имеет end-to-end readback произнесённой реплики, поэтому
автоматически приложенный результат помечается `delivery_unknown`, а не
`delivered`. Это исключает ложное подтверждение и повтор автоматической
доставки; explicit status lookup остаётся доступен.

## Проверки

- slow read-only turn создаёт durable goal и возвращает ID;
- после завершения worker результат сохраняется и появляется на следующем
  turn;
- explicit status lookup находит тот же результат;
- restart возобновляет read-only goal;
- restart не повторяет mutating control goal;
- namespace query не смешивает deferred work с другими active goals;
- warm local `voice_fast` probe: 30 samples, warm-up исключён,
  P50 `0.666 s`, P95 `0.744 s`, max `0.862 s`, budget `3.2 s`.
- полный in-process Alice `SkillApplication` + `BoundedTurnExecutor`:
  30 простых turns после warm-up, P50 `1.924 s`, P95 `2.981 s`,
  max `2.993 s`; все 30 завершились реальным model response, fallback `0`.

На момент исходной source-квалификации эти изменения находились
только в working tree, Alice service не перезапускался, Home Assistant и
устройства не изменялись. Это исторический snapshot, а не текущий status.

## Текущий runtime readback

После одобренного Phase 66 deployment от 2026-08-24:

- source и `/opt/home-butler` hashes совпадают для `alice_skill_gateway.py`,
  `owner_chat.py` и `memory_store.py`;
- `home-butler-alice-skill.service` и `home-butler-local-chat.service`
  активны;
- production model: `qwen3.5:4b-q4_K_M`; evaluator 7/7;
- полный suite: 693 tests OK, 1 skipped;
- controlled O/P по-прежнему не выполнялся, поэтому source/runtime
  parity не выдаётся за доказательство восстановления реального outage.
