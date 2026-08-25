# Phase 66 — наблюдаемость agent turns и recovery

Дата проверки: 2026-08-24.

## Исправленный пробел

До этой правки Memory Store сохранял только retrieval trace: какие записи
памяти были выбраны, почему и сколько заняли. Полного trace одного ответа
модели не было, поэтому требования раздела 19 ТЗ выполнялись частично.

Теперь local chat и Alice записывают в ту же приватную SQLite Memory Store один
bounded trace на каждый обработанный turn:

- trace ID, owner scope, хэш сессии и transport;
- route и все фактически использованные runtime profiles/models;
- точные `prompt_eval_count`/`eval_count`, если их вернул Ollama;
- имена секций context и ID извлечённых memories;
- только имена tool calls, latency, policy result и безопасный status;
- playbook/action/verification codes, total latency и final disposition.

Текст пользователя, prompt, tool arguments/results, entity IDs, IP, MAC,
Authorization и credentials в turn trace не сохраняются. Недопустимое значение
заменяется кодом `unknown`; Memory Store дополнительно применяет общий secret
scanner и ограничения размера JSON.

## Recovery

Второй Incident Adjudicator не создан. Существующий Incident Store уже хранит
incident ID, fact IDs/evidence, decision source, выбранный candidate/playbook,
attempts/cooldown, action adapter, before/after, verification и result.
`incident_status.py` строит из него проверяемое объяснение владельцу и не
показывает IP/MAC. Ответ на вопрос «почему ты перезагрузил интеграцию?» теперь
называет подтверждённую причину, тип точечного bounded action, число попыток и
readback-проверок. Private config entry ID, IP и MAC в ответ не попадают. Новый
декларативный executor использует тот же lifecycle, а не отдельный журнал.

## Runtime drift

Read-only сверка 2026-08-24 подтвердила:

- source и `/opt/home-butler` различаются;
- в `/opt/home-butler` ещё нет `memory_store.py`, `bounded_ha_agent.py`,
  `persistent_scheduler.py`, `recovery_playbook_executor.py` и
  `turn_observability.py`;
- deployed Alice/local-chat units не разрешают запись в Memory Store, тогда как
  подготовленные source units разрешают только точный приватный memory path;
- production `home-butler-dialogue-qualification.service` завершился с
  `ExecMainStatus=2` после восьми bounded restart attempts; основные gateway,
  local chat, Alice skill и Tunnel при сверке были active.

Это означает: source-функция доказана тестами, но пока не является production.

## Проверки

- targeted observability/memory/local-chat/Alice/model/HA/tool-loop/systemd:
  121 tests — OK;
- targeted owner recovery explanation/incident timeline: 79 tests — OK;
- полный offline suite после правки: 650 tests — OK, 1 skipped;
- Python compile — OK;
- ни одна служба, Home Assistant или устройство не перезапускались и не
  изменялись.
