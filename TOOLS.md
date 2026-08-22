# Разрешённые инструменты

- один очищенный GET `/api/states` для чтения всех сущностей Home Assistant;
- чтение локальных CPU, памяти, дисков, температур, systemd и ограниченных
  диагностических журналов;
- read-only проверки сети, DNS, MQTT и Zigbee2MQTT только по фиксированным
  локальным адресам;
- приватный monitor/inventory для связи entity→platform→device/config entry,
  подтверждённых инцидентов и локального IP drift; IP, MAC, атрибуты, ключи и
  сырые evidence модели не передаются;
- owner-chat может после явной команды вызвать только точный
  `ha_control_entity`: switch/light — `turn_on`, `turn_off`, `toggle`, button —
  `press`, всегда с повторным GET;
- operator-side `tts.yandex_station_say` разрешён только для подтверждённых
  критических инцидентов, новых не-baseline переходов любого ранее доступного
  физического устройства или интеграции в `unknown`/`unavailable` после
  20 секунд подтверждения и их восстановления, а также ежедневного отчёта
  ровно в 13:00 на Станции Макс через отдельные фиксированные сервисы;
- bounded recovery может выполнить только рассмотренные LocalTuya/Tuya Local
  reload либо один Core restart после config-check, со своим cooldown и
  повторной проверкой. Эти инструменты модели недоступны.

## Home Assistant для модели

Единственный модельный инструмент —
`mcp__home_assistant_read__ha_get_snapshot`. Он возвращает очищенные
ID/state/time и `proof_entity`, но никогда не возвращает атрибуты или
чувствительные строки. `home-butler-ha-proof.service` — отдельный
operator-side валидатор, а не дополнительный инструмент модели.

Для голосового управления оболочка детерминированно сопоставляет безопасное
русское `friendly_name` с точным entity ID. Владелец не обязан говорить
`switch`, `light`, `button` или `entity_id`. При нескольких совпадениях POST не
выполняется: дворецкий просит одно полное имя. После однозначного выбора adapter
разрешает только пути:

- `/api/services/switch/{turn_on,turn_off,toggle}`;
- `/api/services/light/{turn_on,turn_off,toggle}`;
- `/api/services/button/press`.

Body содержит только `{entity_id}`; произвольные домены, service names и
дополнительные service data отклоняются.

# Запрещённые инструменты

- произвольный shell, sudo, SSH-команда, terminal/code/browser/web/search;
- доступ к root, паролям, токенам и приватным пользовательским файлам;
- произвольный Home Assistant service call;
- restart, reboot, shutdown, delete, update или изменение конфигурации вне
  перечисленных выше узких operator-side recovery-контуров.
