# Stage 71 — Semantic Contract and Truthful Reads

Статус: `PREDEPLOY_GREEN_AWAITING_OWNER_ACTIVATION`. Production не изменён и
остаётся на Stage 70. Stage 72 не начат.

## Safety point и baseline

- Annotated tag `post-stage70-complete-45972cb` указывает на commit
  `45972cb0dcbc624e26f7ae59b1876450661b5e18` и опубликован в `origin`.
- Baseline tests: 22/22 PASS в Windows и WSL.
- Baseline import graph: Web 9, Alice 9, union 10 production modules.
- Baseline units: 12; отдельно работал `home-butler.service` с Hermes gateway.
- Model: `qwen3.5:2b-q4_K_M`, digest
  `124a03c347777e8e4e5955c33610ae01d9d90d8c2a718bfba069c498d5c7f3c9`,
  size 1,945,323,638 bytes, Ollama 0.32.5.
- Фактическая загрузка: context 8192, `100% CPU`; `nvidia-smi` отсутствует,
  GPU offload не подтверждён и не заявляется.
- Конфигурация baseline содержала конфликтующие systemd overrides 32768/64000;
  фактический loaded request оставался 8192. Stage 71 ставит поздний
  `zz-home-butler.conf`, но до activation он не применён.
- Baseline latency: deterministic P50/P95/P99 0.0949/0.1863/0.2015 s (n=5);
  старый model-assisted classifier 11.0390/11.1972/11.2112 s (n=3).
- Baseline inventory schema v4: 178 entities, 50 physical devices, 8 areas,
  535 persistent current fields. HA имел 253 registry rows и 215 current
  entities: 211 registry-backed enabled current, из которых 178 представлены и
  33 logical пропущены; ещё 4 state-only current отсутствовали в graph.

## Hermes и transport

Аудит Web/Alice import graph, unit, процессов, listener и журналов не нашёл
caller реального dialogue path. Hermes сообщал об отсутствии messaging
platforms, а standalone unit держал отдельный runtime и redundant MCP child.
Удалены gateway unit, runner, каталог `hermes/` и optional MCP stdio transport.
После activation closed systemd set будет содержать 11 units.

## Архитектура после

- Один metadata-only HomeGraph schema v5: physical, logical, area и integration
  nodes; registry и state-only entities представлены совместно.
- Physical identity строится только из точного device-registry ID; имя, модель
  и комната никогда не являются основанием для merge.
- Persistent inventory запрещает state, availability, current value и
  timestamps. Current facts берутся только из нового GET `/api/states` внутри
  read turn.
- Host строит закрытый IntentFrame и ordered candidates. Модель может выбрать
  только opaque turn-local ref или clarification; entity/device/service/
  capability ID отклоняются.
- Closed feature resolver, typed ReadReceipt, receipt-first renderer и
  ephemeral per-session focus с TTL 20 минут находятся в одном dialogue path.
- Независимый oracle не импортирует production resolver, renderer, inventory
  или read adapter и отдельно проверяет receipts и готовый answer.
- Production import graph после: Web 8, Alice 8, union 9 modules.

## Corpus и tests

- 587 raw русских utterances без подставленного IntentFrame: 572 уникальных
  direct cases и 15 специальных строк (morphology, typo, alias, room+type,
  feature follow-up, ambiguity, correction, causal, compound, conditional
  control, general conversation).
- Feature follow-up и correction дополнительно проверены как session sequences.
- Fixture replay покрывает robot docked/cleaning, dishwasher off/running,
  conditional controls, unavailable feature, alternate integration, unknown
  error, rinse-aid, child-lock off и camera enum.
- Anti-regressions: ванная/кабинет, Андрей/Roborock, brush/computer resource,
  child-lock off, unknown error и stale inventory/current separation.
- Full suite: 50/50 PASS в Windows и WSL; syntax/compile checks PASS.
- Test failures: 0. Live skips: 4 безопасно неоднозначных labels; они не были
  засчитаны как точные reads и не помешали минимуму 30 physical/10 logical.

## Pre-deploy live acceptance

Frozen blind owner corpus: 40 строк, SHA-256
`4ff8119abb39dd0e33b17c239699f8df15f372f414aa743d256c234eab602376`.

- Proposed inventory: schema 5; 257 metadata entities, 51 physical nodes,
  37 logical nodes, 8 area nodes, 28 integration nodes.
- Coverage: 215/215 enabled current entities represented.
- Exercised: 30 physical devices и 10 logical entities.
- `WRONG_TARGET=0`, `INVENTED_FACTS=0`, `LOST_REQUESTED_VALUES=0`.
- Persistent inventory current values: 0; model-generated entity IDs: 0;
  `HA_SERVICE_CALLS=0`; failures: 0; skips: 4.
- Deterministic latency n=76: P50 0.0560 s, P95 0.1230 s,
  P99 0.1563 s.
- Actual model-assisted selector n=20: P50 1.8539 s, P95 1.8762 s,
  P99 1.8982 s.
- Oracle result: PASS; gates PASS before deploy.

## Размер и файлы

- Lines before: all tracked 6,770; production Python 4,603; tests Python 557.
- Lines after: all tracked text 7,818; production Python 4,895;
  tests Python 1,569.
- Deleted: `config/systemd/home-butler.service`, `hermes/.no-bundled-skills`,
  `hermes/config.yaml`, `scripts/run-hermes-gateway.sh`, три obsolete Stage 70
  test files и старый `tests/live_read_acceptance.py`.
- Changed runtime: Alice session boundary, bounded agent, inventory, resolver,
  read adapter, installer, model policy и owner chat.
- Added verification: blind corpus, Stage 71 fixtures/corpus/oracle, unit tests и
  live acceptance.

## Оставшийся обязательный шаг

Activation не выполнялась. После отдельного явного разрешения владельца нужно
запустить Stage 71 installer с `--activate`, проверить 11-unit closed set,
model/offload и persistent schema v5, затем повторить live-read acceptance.
Только после этого Stage 71 может получить статус `COMPLETE`.
