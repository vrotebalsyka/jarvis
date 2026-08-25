# Phase 66 / Phase 5 — Alice health и fault isolation

Дата проверки: 2026-08-24.

## Обнаруженные причины ненадёжности

1. Старый health probe ожидал фиксированный ответ «Дворецкий на связи.» и полностью обходил LLM и Home Assistant.
2. Один общий счётчик не различал gateway, Funnel/Tailscale, модель и HA.
3. При публичном сбое guardian переходил к restart tunnel без отдельной проверки точной Funnel-конфигурации и её безопасного reassert.
4. Cadence 60 секунд и порог три наблюдения давали до нескольких минут до начала recovery.
5. `Type=oneshot` с `RemainAfterExit=yes` ошибочно мог выглядеть здоровым без нового end-to-end probe.
6. Модельный endpoint, загруженная модель, фактический model turn и read-only HA adapter не имели отдельных readiness-фактов.

## Реализованная миграция

Существующий `alice_skill_health.py` расширен; второй watchdog не создавался.

Status schema 3 хранит отдельные факты:

- `gateway_ready`;
- `public_route_ready`;
- `tailscale_ready`;
- `model_endpoint_ready`;
- `model_loaded`;
- `model_turn_ready`;
- `ha_read_ready`;
- `overall_voice_ready`;
- `owner_config_ready`.

Schema 1/2 читается и безопасно мигрирует в памяти. Старое transport-only состояние не объявляет модель или HA готовыми. Состояние остаётся root-private, atomic и secret-free.

В gateway добавлены два строго фиксированных локальных probe-запроса:

- короткий реальный model turn через production `voice_fast` policy;
- read-only HA snapshot.

Они не создают пользовательскую session, не пишут conversation memory, не вызывают action tools и возвращают readiness только после точной проверки результата.

## Cadence и нагрузка

- transport/Tailscale checks: каждые 10 секунд;
- подтверждение отказа: два последовательных наблюдения;
- реальный model turn: не чаще одного раза в 60 секунд;
- HA read probe: не чаще одного раза в 30 секунд;
- `/api/version` и `/api/ps`: лёгкие bounded checks;
- recovery после подтверждения имеет расчётный верхний бюджет 41,2 секунды;
- exact systemd restart отправляется неблокирующе, а успех определяется только последующим probe/readback.

## Recovery isolation

| Отказ | Разрешённая реакция | Запрещённая побочная реакция |
|---|---|---|
| local gateway | restart только `home-butler-alice-skill.service`, затем local/public readback | restart Tunnel, Tailscale или HA |
| public route при живом Tailscale | повторный public probe → inspect exact Funnel → reassert → verify → только затем restart exact tunnel → verify | restart skill, model или HA |
| подтверждённый Tailscale/policy fault | restart exact `tailscaled.service` → reassert Funnel → verify → при необходимости exact tunnel | restart gateway/model/HA |
| model not loaded / turn failed | safe warm model + `/api/ps` + synthetic turn readback | restart Tunnel/Tailscale |
| model endpoint unavailable | ожидание существующего Windows GPU supervisor и новые endpoint probes | restart Tunnel/Tailscale |
| HA read unavailable | только фиксируется `ha_read_ready=false` | restart Home Assistant или устройств |
| owner/config fault | secret-free status, без recovery | любые restart |

Backoff и circuit breaker ведутся раздельно для gateway, public route и model. После неудачи: 30, 60, 120 секунд и далее до 15 минут; после пяти неудачных recovery попыток открывается 15-минутный circuit. Поэтому сбой одного компонента не блокирует recovery другого и не создаёт restart storm.

## Проверки

- targeted offline suite: 66/66 passed;
- full offline suite: 560 passed, 1 skipped;
- no-cloud audit: passed; cloud API keys и cloud fallback отсутствуют;
- первичный legacy `tests/evaluate_model.py` на source-этапе давал 4/5,
  а тогдашний `/api/ps` — 2.3B/context 4096. Эти две строки являются
  historical pre-deployment evidence, а не current runtime;
- текущий evaluator: 7/7; production model `qwen3.5:4b-q4_K_M`,
  dialogue `/api/ps context_length=32768`, full VRAM;
- source/runtime hashes совпадают для `alice_skill_health.py`,
  `alice_tailscale_funnel.py`, health service и timer;
- health timer `enabled/active`, exact tunnel `active (exited)`, gateway
  `active (running)`; последние timer runs завершаются
  `alice_skill_health=ready`;
- безопасная owner-проверка свежего root-private status:
  `python3 /opt/home-butler/scripts/alice_skill_health.py --check-status`;
- controlled live stop/restart tests не выполнялись.

Для служебной qualification также предусмотрен `--probe-only`: он
обновляет readiness и не вызывает recovery. Его нельзя запускать
как обычную source-команду: production secrets передаются только
через systemd `LoadCredential`. Для ручного read-only status используется
`--check-status` из установленного `/opt`.
