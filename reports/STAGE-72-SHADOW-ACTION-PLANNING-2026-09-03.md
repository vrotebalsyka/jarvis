# Stage 72 — Shadow Action Planning Evidence

Статус: `GREEN_SHADOW_NOT_DEPLOYED`. Реальные HA service calls не выполнялись,
production services не перезапускались и продолжают Stage 71. Stage 73 не начат.

## Baseline

- Stage 71 commit: `76ac341af8ab4708edd108abda81210f69cc503a`.
- Baseline suite: 50/50 PASS; tracked text 7,862 lines, production Python 4,895,
  tests Python 1,571.
- Persistent HomeGraph остаётся metadata-only schema v5; второй graph/resolver
  не создан.
- Model: `qwen3.5:2b-q4_K_M`, digest
  `124a03c347777e8e4e5955c33610ae01d9d90d8c2a718bfba069c498d5c7f3c9`,
  context 8192, фактически 100% CPU; GPU offload не заявляется.

## Архитектура после

- `IntentFrame` расширен закрытыми action/value/scope; scope содержит только
  requested areas/types/name/feature.
- Единственный `ActionPolicyRegistry` deny-by-default. Разрешены исключительно
  `light|switch × turn_on|turn_off`, mode `shadow`.
- Vacuum, button, appliance, lock, climate, script, другие domains, toggle,
  press, start/stop, set, lock/unlock, compound и недоверенные команды hard-deny.
- Host строит candidates, повторно сверяет requested room/type/name/feature и
  не передаёт равных кандидатов модели: при равенстве всегда clarification.
- Модель видит safe labels и только turn-local `rN`; её единственный structured
  output — opaque candidate ref или `clarify`. Технические IDs/service paths
  отклоняются.
- Host создаёт immutable ActionPlan с process-local HMAC seal. В нём нет entity
  ID, device ID, capability ID или service path; execution/dispatch API нет.
- JSON trace содержит intent, safe candidates, selected target label, policy,
  sealed-plan summary, `service_calls=0` и `ha_post=0`.

## Network boundary

Planning не вызывает HA snapshot или read adapter. HA HTTP adapter по-прежнему
имеет закрытую GET-only функцию и allowlist `/api/`, `/api/states`; method нельзя
передать caller-ом. Live harness дополнительно перехватывал все HTTP requests и
физически отклонял non-GET или `/api/services` к HA host.

Итог instrumented live run: HA POST 0, HA service paths 0, service calls 0,
trace failures 0. Зафиксированные POST относились только к loopback Ollama
`/api/generate`, не к Home Assistant.

## Corpus и regression evidence

Ровно 1,000 raw Russian commands без готового IntentFrame/target:

- 300 exact;
- 200 area+type;
- 150 morphology/typo;
- 150 ambiguity;
- 100 cross-room adversarial;
- 50 prompt injection;
- 50 compound/unsupported.

Получено 650 корректных sealed shadow plans и 350 безопасных clarification/
hard-deny. Deterministic full-corpus latency: P50/P95/P99
0.010924/0.021725/0.025082 s.

Frozen owner blind corpus: 40/40 PASS (100%), SHA-256
`9dc26fcdc6fccd15d7bd2270e8e8372a5b03424d290f9afb38ecbb2c794d1c99`.

Отдельно проверены пары ванная/кабинет, туалет/прихожая, кухня/коридор,
зеркало/основной свет, вытяжка/реле вентилятора и Андрей/Roborock. Sealed-plan
tampering, model ID smuggling и попытка вызвать HA snapshot fail closed.

## Actual production parser/resolver/model

Финальный live-model shadow run использовал текущий production metadata
inventory, настоящий `bounded_ha_agent`, единственный resolver и фактический
Ollama model. 30/30 model selections успешны; найдено 5 live targets с
однозначной allowlisted metadata.

- P50 1.6891 s;
- P95 1.8405 s;
- P99 1.9139 s;
- mean 1.6652 s;
- failures 0; prompt technical-ID leaks 0; trace failures 0.

Промежуточные попытки сохранены в evidence честно: первый run имел 20 model
clarifications и P95 2.5733 s; после исправления selector semantics — failures
0, но P95 2.5699 s. Закрытый output был сокращён до единственного opaque choice;
финальный n=30 run прошёл порог без ослабления policy или network boundary.

## Gates

- `WRONG_TARGET=0`
- `CROSS_ROOM_TARGET=0`
- `AMBIGUOUS_SIDE_EFFECT=0`
- `UNSUPPORTED_ACTION_PLANNED=0`
- `VACUUM_PLAN_ALLOWED=0`
- owner blind corpus `40/40 = 100%`
- model-assisted plan P95 `1.8405 s <= 2.5 s`
- `HA_POST=0`, `HA_SERVICE_PATHS=0`, `HA_SERVICE_CALLS=0`
- production control/executor absent
- repository suite: 59/59 PASS в Windows и WSL; compile/shell syntax PASS

## Размер и изменённые файлы

- Lines before: tracked text 7,862; production Python 4,895; tests Python 1,571.
- Lines after: tracked text 9,174; production Python 5,393; tests Python 2,213.
- Added: `scripts/shadow_action_policy.py`, Stage 72 corpus/fixtures/blind/live
  acceptance/unit tests и этот report.
- Changed: bounded agent, one resolver, GET-only adapter injection filter,
  model selector policy, installer closed runtime list, Stage 71 regression
  expectations, repository contract и project documentation.
- Deleted: none.

## Итог

Все Stage 72 gates зелёные. Код остаётся shadow-only и не развёрнут; production
control отсутствует. Stage 73 не начат.
