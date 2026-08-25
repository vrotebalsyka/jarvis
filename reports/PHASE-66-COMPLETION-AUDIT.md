# Phase 66 — строгий completion-аудит

Дата: 2026-08-25. Objective SHA-256:
`D1BD9C1A19FE72B4C1FB1D2F40B82DBE135190DB415777EDA6498D8A3D9B0429`.

## Итог

Phase 66 **не помечена завершённой**. Реализация, deployment и offline
регрессия завершены, но цель требует более сильного доказательства, чем
совпадение source/runtime и зелёные unit tests. Открыты два controlled-live
acceptance ID (O/P) и два наблюдаемых scheduler/wake события.

На текущем readback:

- полный suite: 693 tests OK, 1 skipped;
- model evaluator: 7/7;
- no-cloud audit, repository hygiene, manifest и `git diff --check`: PASS;
- source/runtime parity: 68/68 scripts, 58/58 systemd units;
- Qwen 4.7B Q4_K_M: dialogue 32768, voice/structured 8192, full GPU offload;
- local/LAN chat: HTTP 200; Alice health: `ready`;
- scheduler: `system-daily-report`, `not_due`, 25 августа 13:00 +05,
  wake 12:58, attempts 0, verification `not_run`;
- пять action/recovery timers: `disabled/inactive`;
- Home Assistant, устройства, `ollama.service` и `tailscaled.service` в этой
  квалификации не перезапускались и не переключались.

## Requirement → авторитетное доказательство

| Область objective | Доказательство текущего состояния | Статус |
|---|---|---|
| Phase 0: baseline, manifest, runtime drift, hygiene | `PHASE-66-BASELINE.md`; manifest на 288 файлов; `test_repository_hygiene`; runtime DB/WAL/log/cache удалены только из Git tracking и игнорируются | PASS |
| P1: единая Runtime Policy, не случайный 2K | `test_production_call_sites_have_no_manual_context_or_model_route`; `RUNTIME_POLICY_OK`; `/api/ps` после dialogue-turn: 32768 и `size_vram=size` | PASS |
| Benchmark и voice latency | `PHASE-66-MODEL-BENCHMARK.md`; 30/30 warm turns, P95 2.981 s; evaluator 7/7 | PASS |
| P2: indexed memory, 100 turns, correction, restart/cross-transport | `test_100_turns_restart_and_cross_transport_keep_alias_goal_and_preference`; `test_correction_supersedes_old_alias_and_retrieval_returns_only_new`; Memory Store/Context Builder tests | PASS offline |
| P3: один расширенный DeviceGraph, feature/device distinction, compact MCP | inventory schema 3; sanitizer/MCP/catalog tests; обычный bounded loop не экспортирует snapshot tool | PASS offline + read-only runtime |
| P4: unknown consumable, component-only vacuum finding, unknown code | `test_vacuum_with_26_features_reports_only_component_not_device_outage`; `test_unknown_dishwasher_consumable_is_specific_and_read_only`; `test_unknown_numeric_error_code_is_reported_without_invented_meaning` | PASS offline |
| P5: normalized/redacted logs и semantic classification без action authority | system-log tests и live cache proof: первый semantic batch, затем cache hits без нового action/model call | PASS offline + read-only runtime |
| Alice component health и low-resource guardian | `test_alice_skill_health` fault-isolation/backoff suite; timer 10 s, expensive probes cached; current `alice_skill_health=ready` | PASS offline + healthy runtime; O/P pending |
| P6: единый Scheduler, natural reminder, reschedule, restart, update/cancel | Q/R/S/T tests; production DB является единственным источником 13:00; Windows wake читает nearest TaskSpec; delivery-unknown proof сохраняет `last_run`, `attempts`, verification и запрещает duplicate | PASS offline + runtime not-due proof; delivery/wake pending |
| P7: natural bounded tool loop, capabilities, coreference, readback boundary | bounded agent/capability/natural facade tests; dishwasher two-step plan; coreference tests; production protected local-chat POST | PASS offline + read-only runtime |
| P8: declarative recovery ladder, risk/policy/readback/idempotency | `test_recovery_playbook_registry`; delivery-unknown tests; registry/executor/planner deployed | PASS offline; R1 live authority remains staged |
| P9: onboarding and behavior | queue schema 2 migration; partial answers; actual Qwen proposal/approval; `actions_performed=0`; behavior preference/safety tests | PASS offline + read-only runtime |
| P10 optional Assist/Vision | compatibility design documented; adapters/models intentionally not installed without owner authorization | OPTIONAL / not a completion gate |
| Observability | turn trace tests; recovery ledger; owner explanation route | PASS offline + runtime storage |
| Safe self-improvement | proposal/isolated worker/exact approval/rollback tests; no production write tool | PASS offline; owner-invoked only |
| Safety invariants | 693-test suite, no-cloud audit, closed schemas, prompt-injection tests, staged R1, R3 confirmation | PASS |

## Acceptance A–Z

- A–N и Q–Z имеют executable offline evidence; C, S, V и Z дополнительно имеют
  read-only/live runtime evidence.
- **O остаётся PENDING live:** controlled outage только Funnel и измерение
  фактического recovery time.
- **P остаётся PENDING live:** отдельная controlled isolation для skill,
  tunnel/Tailscale, model endpoint и HA с доказательством, что guardian лечит
  только правильный компонент и не создаёт restart storm.

Offline fault injection не заменяет O/P: она доказывает decision logic, но не
реальное поведение systemd/Tailscale/Yandex route.

## Внешние события, которых ещё нет

1. Реальный timer-driven отчёт 25 августа 2026 в 13:00 Asia/Yekaterinburg.
   После события scheduler должен иметь `last_run_epoch`, ненулевой `attempts`
   и честный verification (`confirmed` либо `delivery_unknown`), а не
   `not_run`.
2. Физическое wake-from-sleep около 12:58. Наличие trigger и
   `WakeToRun=true` доказывает конфигурацию, но не факт пробуждения спящего ПК.
3. Controlled O/P. Владелец пока разрешал deployment, но явно запретил O/P;
   поэтому ни один exact transport component искусственно не останавливался.

## Условие будущего завершения

Goal можно пометить complete только после:

- наблюдения и записи фактического scheduler delivery результата;
- доказательства физического wake, если ПК действительно был переведён в sleep
  до 12:58;
- отдельного явного разрешения владельца на controlled O/P, выполнения exact
  runbook без рестарта HA/устройств и прохождения O/P в bounded срок;
- повторной проверки, что полный suite/no-cloud/parity остаются зелёными после
  любых исправлений, найденных live-тестами.

До этих событий зелёная source/runtime квалификация не выдаётся за полное
достижение objective.
