# Phase 66 / этап 10 — безопасное самоулучшение

Дата source-квалификации: 2026-08-24.

Статус: pipeline реализован, offline-квалифицирован и установлен в
`/opt/home-butler`; source/runtime hashes совпадают для
`maintenance_worker.py` и `model_workspace.py`. Автоматического unit/timer нет
намеренно: worker остаётся ручным owner-invoked инструментом. Patch для
текущего dirty working tree не применялся; HA и устройства не изменялись.

## Два разных режима

Настройки поведения уже проходят через закрытые `behavior_get`, `behavior_set`
и `behavior_reset`; для них не нужен code deployment.

Для изменения кода production-модель получила только
`change_proposal_create`. Инструмент принимает закрытую схему из семи полей:

- observed problem;
- evidence;
- affected components;
- proposed change;
- expected benefit;
- risks;
- proposed tests.

Proposal сохраняется как owner-only JSON в `proposals`, получает content hash
и явно содержит `production_deployed: false`. Строки proposal считаются
untrusted data. Создание proposal не открывает repository, не запускает тесты,
не выдаёт approval и не вызывает deployment.

Свободный `workspace_write_text` теперь может писать только в `knowledge`,
`notes` и `reports`. `proposals` зарезервирован для закрытого builder, а
`settings` — для validated behavior tools.

## Отдельный maintenance worker

`maintenance_worker.py` не подключён ни к local chat, ни к Алисе, ни к
systemd timer. Его может вручную запустить только владелец. Стадии:

1. `prepare` проверяет чистый active repository и создаёт отдельный Git
   worktree/branch в owner-only maintenance directory;
2. `capture` принимает diff только из этого worktree, проверяет список файлов
   относительно affected components, отклоняет binary, secrets и runtime
   artifacts;
3. fixed qualification запускает unit tests, весь offline suite, no-cloud
   audit, model evaluation и `git diff --check`;
4. тесты запускаются через transient `systemd-run` с no-new-privileges,
   пустым capability set, read-only host filesystem и без сети; model
   evaluation получает только loopback и приватный WSL-host range;
5. candidate получает отдельный hash. Approval требует точную строку
   `APPROVE <candidate_hash>`;
6. approval всё ещё ничего не развёртывает. Deployment требует вторую точную
   строку `DEPLOY <candidate_hash>` и отдельный ручной вызов;
7. active repository должен быть чистым и оставаться на candidate base commit;
8. после fixed installer выполняется health probe core services и local chat;
   при regression patch автоматически снимается и предыдущий source повторно
   устанавливается.

Production conversational tool list не содержит prepare, patch capture,
approval или deploy. Модель не получает shell, path к active repository или
произвольную test/deploy command.

## Owner-only команды

Эти команды приведены как интерфейс pipeline, а не как предложение запускать
их на текущем dirty working tree:

```bash
python3 scripts/maintenance_worker.py prepare <proposal_id> --owner-invoked
python3 scripts/maintenance_worker.py capture <proposal_id> \
  --worktree /var/lib/home-butler-maintenance/change-<proposal_id> \
  --owner-invoked
python3 scripts/maintenance_worker.py approve <candidate_id> \
  --confirmation 'APPROVE <candidate_hash>' --owner-invoked
python3 scripts/maintenance_worker.py deploy <candidate_id> \
  --confirmation 'DEPLOY <candidate_hash>' --owner-invoked
```

## Проверки

- proposal schema, secret/path guards и prompt-injection-as-data;
- production proposal не меняет active repository;
- active repository нельзя выдать за isolated worktree;
- patch не может выйти за affected components;
- fixed sandbox command не использует shell и ограничивает сеть;
- полностью qualified candidate без approval не развёртывается;
- неправильный hash отклоняется;
- approval не является deployment;
- неуспешный health probe автоматически возвращает исходный source;
- generic workspace write не может записать proposal/settings.

Фактические итоговые числа полного suite фиксируются в
`reports/PHASE-66-RESULT.md` после общей acceptance-квалификации.

## Rollback

Для установки самого owner-only worker rollback данных не нужен: он не
меняет proposal/candidate до ручного запуска. Если owner-invoked deployment
прошёл installer, но health probe не подтвердил core services/local chat,
worker снимает exact patch, повторно запускает fixed installer из предыдущего
source и записывает `rolled_back`/`rollback_verified` в workspace.
