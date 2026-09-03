# Current Goal — Stage 71

Статус: `COMPLETE`.

Stage 71 реализован, активирован после отдельного разрешения владельца и прошёл
repository, fixture, independent-oracle, pre-deploy и post-activation live
read-only gates. Stage 72 не начат.

Готово:

- safety tag `post-stage70-complete-45972cb` создан и опубликован;
- baseline tests/import graph/units/model/context/offload/latency/inventory
  зафиксирован;
- отсутствие Hermes caller доказано, gateway/unit/runner/optional MCP transport
  удалены из проекта;
- один metadata-only HomeGraph schema v5 покрывает physical/logical/area/
  integration nodes и 215/215 enabled current entities;
- закрытые IntentFrame, ordered resolver, feature resolver, typed ReadReceipt и
  ephemeral session focus реализованы;
- независимый oracle, 587 raw utterances, required fixtures, anti-regressions и
  frozen 40-row blind live corpus добавлены;
- full suite 50/50 PASS в Windows и WSL;
- pre-deploy live: all zero gates, 30 physical/10 logical reads,
  deterministic P95 0.1230 s, model-assisted P95 1.8762 s,
  `HA_SERVICE_CALLS=0`.
- post-activation deployed-runtime live: 215/215 coverage, 30 physical/10
  logical reads, all zero gates, failures 0, safe ambiguity skips 4,
  deterministic P50/P95/P99 0.0510/0.1594/0.2291 s, model-assisted
  2.1168/2.9852/3.0073 s, `HA_SERVICE_CALLS=0`, oracle PASS;
- closed production set contains 11 units and 14 hash-matched runtime files;
  Hermes is absent, all permanent units are active, failed-unit list is empty;
- production inventory is metadata-only schema v5, mode `0600`; actual model
  is `qwen3.5:2b-q4_K_M`, context 8192, `100% CPU`, without claimed GPU offload.

Stage 71 завершён. Дальнейших действий в рамках этой стадии нет.

Полный отчёт:
[`reports/STAGE-71-SEMANTIC-CONTRACT-2026-09-03.md`](reports/STAGE-71-SEMANTIC-CONTRACT-2026-09-03.md).
