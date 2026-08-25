# Phase 66 / этап 6 — единый persistent scheduler

Дата проверки исходного дерева: 2026-08-24.

## Статус

Реализация развёрнута в `/opt/home-butler`; persistent scheduler timer активен,
а текущая база сохранила `system-daily-report` на 25 августа 2026 в 13:00
Asia/Yekaterinburg. Home Assistant и реальные устройства при развёртывании не
изменялись. Реальная доставка напоминания/отчёта через колонку всё ещё не
выполнялась только ради теста и не считается live-квалифицированной.

## Что изменено

- Один SQLite scheduler хранит `TaskSpec`, состояние выполнения и журнал
  попыток. Время 13:00 осталось только начальным значением системного
  ежедневного отчёта в базе, а не константой нескольких исполнителей.
- Поддержаны local reminder, Yandex native reminder, daily/recurring/one-shot
  report, scheduled device action, follow-up и deferred diagnostic result.
- Добавлены закрытые model-facing schemas для `task_create`, `task_update`,
  `task_cancel`, `task_list` и `task_get`. Shell, service path и произвольный
  executable payload не принимаются.
- Natural parser переводит русскую дату/время в canonical JSON, после чего
  deterministic validator проверяет timezone, recurrence, payload и backend.
- `home-butler-daily-report.timer` больше не содержит `OnCalendar=13:00`:
  частый лёгкий tick запускает только due-задачи из базы.
- `operations_supervisor.py` читает next run, last run, verification и missed
  status из scheduler, не вычисляя их по фиксированному часу.
- Wake-export содержит только ближайший epoch и не раскрывает текст или
  payload задачи. Скрытый Windows sync-worker получает его через
  `persistent_scheduler.py --wake-json` и обновляет отдельный exact one-shot
  wake task. Старый ежедневный триггер 12:58 удалён; runtime path не содержит
  PowerShell.
- Пропущенные задачи исполняются согласно сохранённой policy. `skip` не
  вызывает executor; `run_once`/`catch_up_once` исполняются не более одного
  раза благодаря execution key и lease.
- Неудавшийся отчёт повторяется через 5 минут только внутри 15-минутного окна.
  `delivery_unknown` автоматически не повторяется.
- Legacy `notes/LAST-REMINDER.json` импортируется идемпотентно и не удаляется.
  Уже переданный native Yandex reminder помечается как external-managed:
  scheduler не обещает неподтверждённые update/cancel операции.

## Источник истины и пути

- Код: `scripts/persistent_scheduler.py`.
- Natural parsing: `scripts/scheduler_natural.py`.
- База после установки:
  `/home/homebutler/.local/state/home-butler/scheduler/scheduler.sqlite3`.
- Безопасный status-export:
  `/home/homebutler/.local/state/home-butler/scheduler/scheduler-status.json`.
- Systemd tick сохраняет прежнее имя unit для совместимости:
  `home-butler-daily-report.timer`.

Scheduler DB — единственный источник расписания. Status JSON является только
секрет-безопасным экспортом для supervisor и Windows wake helper; он не
принимается обратно как команда.

## Проверенные acceptance-сценарии

- Q: фраза «Завтра утром в восемь напомни заказать таблетки для
  посудомойки» создаёт persistent local TaskSpec без обязательного слова
  «чтобы».
- R: перенос ежедневного отчёта меняет next run в scheduler; старое время не
  исполняется; supervisor и wake-export показывают новое время; test clock
  выполняет отчёт в новом слоте.
- S: task переживает повторное открытие SQLite store и исполняется ровно один
  раз.
- T: task находится обычным описанием, изменяется и отменяется без duplicate
  execution.
- Missed `skip` не вызывает worker, а bounded report retry не выходит за окно.
- Safe status не содержит natural description и canonical payload.
- Nested JSON schemas имеют `additionalProperties=false`.

Фактические проверки 2026-08-24:

- targeted Phase 6: 124 теста — OK;
- полный offline suite: 584 теста — OK, 1 skipped;
- model evaluation: 5/5 — PASS; safe tool selection вызвал
  `ha_get_snapshot`; загруженный context 8192;
- no-cloud audit: PASS, cloud fallback отсутствует;
- PowerShell parser для Windows installer: PASS;
- `git diff --check`: PASS.

## Миграция и откат

Schema version создаётся транзакционно и повторный запуск идемпотентен.
Legacy reminder record остаётся на месте до отдельного подтверждённого
maintenance. Старые имена systemd unit сохранены, поэтому откат source/runtime
может вернуть прежний исполнитель без переименования units. Перед будущим live
deployment нужно сделать backup scheduler DB и проверить владельца/mode 0600.

## Что ещё не доказано live

- Реальная доставка local reminder через TTS worker после deployment.
- Реальное пробуждение Windows из sleep по новому динамическому one-shot wake
  trigger. Source/runtime readback уже пройден: `WakeToRun=true`, next wake
  25 августа 12:58, sync result 0.
- Фактический отчёт в изменённое владельцем время.

Эти проверки требуют отдельного разрешения на deployment/restart только служб
Home Butler. Переключение устройств для них не требуется.
