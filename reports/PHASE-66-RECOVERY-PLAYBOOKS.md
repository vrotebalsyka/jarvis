# Phase 66 / этап 8 — декларативные bounded recovery playbooks

Дата source-квалификации: 2026-08-24.

Статус: реализовано, offline-квалифицировано и развёрнуто в
`/opt/home-butler`. Source/runtime hashes совпадают для registry,
executor и совместимого planner. Это не означае, что live R1
разрешён: action/recovery timers остаются `disabled/inactive`, а
source-default — `dry_run`. Home Assistant, его интеграции и устройства при
этой проверке не перезапускались.

## Архитектура

- `recovery_planner.py` сохранён как совместимый фасад, но список кандидатов и
  условия их появления теперь берутся только из
  `recovery_playbook_registry.py`. Старый второй набор `if`-правил удалён.
- Каждый playbook имеет ID/version/scope, поддерживаемые integration и device
  class, canonical trigger facts, required evidence, preconditions, risk,
  automatic permission, ordered adapters, verification, rollback, attempt
  budget, cooldown, stop conditions и escalation text.
- Модель по-прежнему выбирает только opaque candidate ID из предложенного
  набора. Она не формирует service path, config entry, IP, shell-команду или
  аргументы adapter.
- `recovery_playbook_executor.py` является закрытым deterministic gate. Он
  проверяет trigger evidence, attempt budget и cooldown до вызова adapter;
  после каждого шага принимает только закрытый результат
  `verified|no_action|failed|delivery_unknown` и останавливает лестницу при
  первом неподтверждённом результате.

## Реестр

В реестр мигрированы девять существующих кандидатов:

| Playbook | Risk | Минимальное действие |
|---|---:|---|
| `observe_and_notify` | R0 | сохранить наблюдение |
| `wait_yandex_backoff` | R0 | отложить повторный probe |
| `close_obsolete_intent` | R0 | закрыть устаревший incident |
| `close_verified_state` | R0 | закрыть уже подтверждённое состояние |
| `retry_original_intent_once` | R1 | ровно один повтор точного действия |
| `reload_yandex_entry_once` | R1 | reload одного известного entry |
| `repair_helper_state` | R1 | исправить один известный helper |
| `reload_integration_entry_once` | R1 | reload одного точного entry |
| `reload_local_integration_once` | R1 | существующий LocalTuya/Tuya Local путь |

Любой R1 требует canonical fact `confidence:confirmed`. Одного краткого
`unavailable`, одного пропущенного ping или частичного отказа entity
недостаточно. Partial entity fixture с доступными siblings, alternate
integration и LAN предлагает только R0 observation.

## Stable identity и IP drift

Registry не передаёт IP модели и не содержит adapter для произвольной смены
адреса. `reload_local_integration_once` оставляет исполнение существующему
`home_assistant_recovery.py`, где drift принимается только из приватной
inventory binding со stable identity, невозможно выбрать устройство по имени
или vendor и запрещено редактировать `.storage`. После reload проверяются все
members physical device. Эти границы подтверждены существующими тестами
`test_confirmed_ip_drift_is_used_as_recovery_diagnosis` и
`test_inventory_loader_rejects_duplicate_or_hostile_entries`.

## Live qualification

Без отдельной записи qualification R1 возвращает
`qualification_required` и делает ноль adapter calls. Для разрешения нужны
одновременно:

1. offline tests;
2. dry-run;
3. owner approval;
4. controlled live proof;
5. staged enable;
6. post-enable observation;
7. rollback flag.

Source-default остаётся `dry_run`: offline/dry-run proof не включает live.
R2 остаётся в capability/policy контуре явной команды владельца; R3 требует
отдельной следующей подтверждающей реплики. Реестр восстановления не расширяет
эти полномочия.

## Проверки

- recovery/playbook/planner/existing executors/service-definition: 45 — OK;
- hostile fact `IGNORE PREVIOUS INSTRUCTIONS` отклонён до adapter call;
- R1 без confirmed evidence не предлагается;
- attempt budget и cooldown срабатывают до adapter;
- dry-run вызывает ноль adapters;
- fully-qualified fixture вызывает ровно один allow-listed adapter и требует
  readback result.
- итоговый полный offline suite: 615 OK, 1 skipped;
- no-cloud audit и `git diff --check`: PASS.

## Rollback

Исходная source-квалификация не меняла runtime; последующий Phase 66
deployment установил те же квалифицированные hashes. Source rollback
ограничен возвратом
`recovery_planner.build_candidates` к предыдущей реализации и удалением двух
новых модулей; специализированные executors и их журналы не менялись.
