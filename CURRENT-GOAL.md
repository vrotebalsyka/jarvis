# Current Goal — Stage 71

Статус: `PREDEPLOY_GREEN_AWAITING_OWNER_ACTIVATION`.

Stage 71 реализован и прошёл repository, fixture, oracle и live read-only gates.
Production остаётся на Stage 70: deployment/activation не выполнялись, потому
что для них требуется отдельное явное разрешение владельца.

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

Следующий и единственный шаг Stage 71: после отдельного разрешения владельца
выполнить activation и повторить live-read acceptance. Stage 72 не начинать.

Полный отчёт:
[`reports/STAGE-71-SEMANTIC-CONTRACT-2026-09-03.md`](reports/STAGE-71-SEMANTIC-CONTRACT-2026-09-03.md).
