# Stage 68 — итоговый отчёт

Дата завершения квалификации: 28 августа 2026 года.

## 1. Что реально установлено

Stage 67 подтверждён на commit `28848f17b884fc46c203053fe181211f624afe4e`, применён и сохранён как baseline в `reports/stage68-baseline/`. Установлены bounded `device_learning.py`, экспорт validated dataset, DeviceKnowledgeProfile, компактный read-only resolver и on-demand systemd unit для изучения ровно одного физического устройства. Learning unit не включён постоянно и после обучения не работает.

## 2. Тесты

Финальный полный offline suite: **723 теста**, **0 ошибок**, **1 ожидаемый пропуск**. `evaluate_model.py`: **7/7**. Stage 67 read-only evaluator: зелёный. Stage 68 evaluator: **8/8**. Реальный Alice/Yandex JSON E2E: **2/2**.

## 3. Какие реальные устройства изучены

- Робот-пылесос **«Андрей»**: 22 функции, 15 capabilities, Xiaomi Miot, зона «Кухня».
- Посудомойка **Dishwasher**: 23 функции, 8 capabilities, Midea AC LAN.

Долговременная идентичность строится по physical device из существующего DeviceGraph, а не по одному `entity_id`. Второй DeviceGraph не создавался.

## 4. Сколько примеров создано

Создано **160** examples. В validated JSONL принято **150**: по **75** для Андрея и посудомойки. В каждом наборе 50 positive и 25 negative examples. Категории охватывают чтение, coreference, выбор действия, диагностику, обслуживание, conditional availability, unknown facts, partial failure и action verification.

## 5. Сколько отклонено

Validator отклонил **10** заведомо плохих или неподтверждённых примеров. Они изолированы в `training/rejected/` и `reports/learning-rejected/` и не входят в `training/validated/stage68-all.jsonl`.

## 6. Что Jarvis знает про Андрея

Jarvis связывает русские формы «Андрей», «Андрея», «Андрею», «Андреем», «об Андрее» с одним физическим роботом. Он различает status, battery, dock, cleaning, modes, consumables, maintenance, diagnostics, configuration и conditional features. Постоянные regression lessons сохраняют смысл `charging`, батарею 100%, исторически подтверждённые ресурсы 13/56/72/13 и правило: две недоступные вторичные функции не означают поломку всего робота. Текущий live profile использует только актуальные значения HA; в финальном evaluator это 12% фильтра, 56% основной щётки и 12% боковой щётки.

## 7. Что Jarvis знает про посудомойку

Профиль различает power, status/progress, program/mode, door, error, rinse aid, salt и cycle time. Controls, которые недоступны при `power=off`, считаются условно доступными при `power:on`, а не отказом прибора. `power=on` не доказывает запуск цикла, а `button.press` не доказывает физический успех: подтверждение требует reviewed status/progress/time transition. `accepted != verified`.

## 8. Какие hallucinations блокируются

- timestamp управляющей кнопки не может стать процентом батареи;
- charging не превращается в движение или уборку;
- батарея 100% не называется разряженной;
- числа не берутся вне текущих source facts;
- неизвестная причина не подменяется Wi‑Fi, сервером, интеграцией или зависанием;
- отказ feature не превращается в physical outage;
- maintenance percentage не называется зарядом;
- action без подтверждающего transition не называется выполненным;
- технические IDs, приватные адреса и секреты не попадают в ответ.

## 9. Latency Alice до и после

Первый неоптимизированный Stage 68 путь занимал около **60,7 с** и делал до пяти model calls. После prepared profile + compact retrieval один read-only turn использует один model call и три релевантные функции. В финальном прогретом прогоне восемь ответов заняли **1,366–4,669 с**; обязательная последовательность Alice E2E заняла **3,697–3,973 с**, и оба ответа вернулись напрямую без deferred reply. Первый прогон сразу после перезапуска штатно использовал deferred-канал, поэтому готовность оценивается после прогрева модели. Voice runtime не замедлился; он стал существенно быстрее.

## 10. Нужна ли LoRA

Нет. Обязательный evaluator и Alice E2E дали 100% с retrieval, grounding и verifier. Веса `qwen3.5:4b-q4_K_M` не изменялись, ничего не скачивалось. Экспортёр подготовлен на будущее, но запуск LoRA без нового доказанного пробела не оправдан.

## 11. Что остаётся следующему этапу

Следующий этап может расширить тот же pipeline на остальные реальные устройства, добавить owner-confirmed corrections и больше проверенных state transitions. Stage 68 не включает vision, новые recovery actions, загрузку моделей или self-patching production — эти действия не выполнялись.

## Безопасность и эксплуатация

Модель записывает только в bounded workspace `/home/homebutler/.local/share/home-butler/model-workspace` с квотой **10 GiB** и файлами режима 0600. Пользователь `homebutler` не может записывать в `/root/Jarvis`, `/opt/home-butler` или `/etc`; learning unit дополнительно использует `ProtectSystem=strict`, `NoNewPrivileges=yes` и один разрешённый `ReadWritePaths` для workspace. Recovery timers остаются disabled/inactive. Home Assistant и устройства во время Stage 68 не перезапускались и не переключались.
