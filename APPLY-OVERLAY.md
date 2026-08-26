# Jarvis Stage 67 overlay

Этот архив содержит только изменённые и новые текстовые файлы. В нём нет
резервной копии Home Assistant, токенов, IP/MAC, config-entry IDs или других
секретов.

Текущая production-модель остаётся Qwen 4B. Обновление добавляет обязательный
reviewed lesson pack, 20 проверенных training/regression examples в bounded
workspace и детерминированную проверку фактов/результатов. Весы модели не
переписываются, поэтому скорость и размер модели сохраняются.

## Применение поверх текущей ветки main

```bash
cd /root/Jarvis/home-butler
git status --short
git rev-parse HEAD
sudo tar -xzf /путь/jarvis-stage67-main-overlay.tar.gz \
  -C /root/Jarvis/home-butler --strip-components=1
sudo ./scripts/apply-stage67-update.sh
```

Скрипт запускает targeted regression tests, полный offline suite, существующий model evaluator и
read-only проверку реального устройства «Андрей». Recovery/action timers
остаются в staged/disabled режиме.

## Что заменяется

- `scripts/model_runtime_policy.py`
- `scripts/model_ha_proof.py`
- `scripts/home_assistant_control.py`
- `scripts/capability_catalog.py`
- `tests/test_home_assistant_control.py`

## Что добавляется

- `scripts/install_verified_lessons.py`
- `scripts/apply-stage67-update.sh`
- `tests/test_truthful_device_grounding.py`
- `tests/test_install_verified_lessons.py`
- `tests/evaluate_stage67.py`
- `tests/fixtures/stage67/*`
- `training/stage67_verified_examples.jsonl`
- `reports/STAGE-67-TRUTHFUL-MODEL.md`
