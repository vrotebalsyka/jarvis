# Phase 66 — production deployment и rollback runbook

Дата preflight: 2026-08-24. Этот документ сам ничего не изменяет.

> Статус: описанный ниже preflight исторический. Phase 66 уже развёрнута;
> актуальный post-deployment parity audit подтверждает 68/68 runtime scripts и
> 58/58 managed units без mismatch. Текущие доказательства находятся в
> `PHASE-66-RESULT.md`. Раздел controlled O/P остаётся неприменённым.

## Read-only preflight

- full offline suite: 650 passed, 1 skipped за 115.524 s;
- model evaluator: 5/5, `home-butler:latest`, 2.3B Q4_K_M, context 8192,
  модель полностью в VRAM;
- no-cloud audit и `git diff --check`: PASS;
- source systemd verification: PASS; единственное предупреждение относится к
  системному `snapd.service`, а не к Home Butler;
- `/opt/home-butler`: 491 MiB; private state: 7.2 MiB; свободно около 924 GiB;
- source отличается от runtime, новые memory/agent/scheduler/playbook/trace
  modules не deployed;
- на момент этого historical preflight
  `home-butler-dialogue-qualification.service` в старом runtime failed с
  `ExecMainStatus=2` после восьми bounded restart attempts. Пункт не
  описывает текущую уже развёрнутую source-версию.

## Что будет временно остановлено

Только units с префиксом `home-butler-` и основной `home-butler.service`:

- running services: `home-butler.service`, `home-butler-local-chat.service`,
  `home-butler-alice-skill.service`, `home-butler-incident-monitor.service`;
- Funnel assertion: `home-butler-alice-tunnel.service`;
- paths: `home-butler-alice-finalize.path`,
  `home-butler-alice-rotation-finalize.path`;
- active timers: `home-butler-alice-health.timer`,
  `home-butler-automation-diagnostics.timer`, `home-butler-daily-report.timer`,
  `home-butler-device-health.timer`, `home-butler-diagnostic-monitor.timer`,
  `home-butler-dialogue-qualification.timer`,
  `home-butler-entity-freshness.timer`,
  `home-butler-ha-device-knowledge.timer`, `home-butler-heartbeat.timer`,
  `home-butler-incident-notifier.timer`, `home-butler-inventory.timer`,
  `home-butler-model-study.timer`, `home-butler-operations-supervisor.timer`,
  `home-butler-startup-ha-check.timer`,
  `home-butler-startup-self-check.timer`,
  `home-butler-startup-voice-status.timer`,
  `home-butler-system-log-diagnostics.timer`.

Home Assistant, его container/OS, устройства, Ollama и Tailscale daemon не
останавливаются и не переключаются. Action recovery timers остаются staged и
disabled. Ожидаемый перерыв local chat/Alice/monitoring: 2–5 минут.

## Безопасная последовательность после отдельного разрешения владельца

1. Повторить read-only preflight и сохранить exact active-unit list.
2. Создать root-only backup с timestamp:
   `/var/backups/home-butler/phase66-<timestamp>/` для `/opt/home-butler`,
   managed systemd units и private state. Secrets не копировать в рабочую
   директорию и не печатать.
3. Остановить только перечисленные Home Butler units.
4. Запустить существующий fixed installer в
   `HOME_BUTLER_INSTALL_ACTION_TIMERS_MODE=staged`; не использовать новый
   deploy subsystem.
5. Проверить runtime policy, unit sandbox, exact hashes, active services,
   local loopback/LAN chat, Memory Store schema, scheduler status, HA read-only
   health, Alice gateway/tunnel/model/HA component status.
6. Если любая обязательная проверка failed — остановить новые Home Butler units,
   восстановить backup runtime/units/state, выполнить `daemon-reload` и вернуть
   exact прежний active-unit list.
7. Только после зелёного deploy отдельно проводить acceptance O/P.

## Controlled-live O/P после отдельного разрешения

- O: временно снять только Funnel route, не останавливая skill/model/HA;
  измерить automatic non-LLM recovery и подтвердить bounded time.
- P: по одному изолировать skill, tunnel, Tailscale reachability, model endpoint
  и HA read path; проверять, что guardian выбирает правильный компонент и не
  создаёт restart storm.
- HA и устройства не перезапускать. Model/HA failure можно квалифицировать
  безопасным deny/reachability fixture; реальный HA outage не обязателен.

Deployment и O/P — два разных разрешения. Успешный source test не считается
разрешением ни на одно из них.
