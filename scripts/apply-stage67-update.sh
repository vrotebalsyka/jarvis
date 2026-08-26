#!/usr/bin/env bash
set -euo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C.UTF-8

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
RUNTIME_DIR="/opt/home-butler"
LESSON_WORKSPACE="/home/homebutler/.local/share/home-butler/model-workspace"

fail() {
  printf '%s\n' "$1" >&2
  exit 2
}

(( EUID == 0 )) || fail 'Запустите скрипт от root.'
for required in \
  scripts/model_runtime_policy.py \
  scripts/model_ha_proof.py \
  scripts/home_assistant_control.py \
  scripts/capability_catalog.py \
  scripts/install_verified_lessons.py \
  training/stage67_verified_examples.jsonl \
  tests/evaluate_stage67.py; do
  [[ -f "$PROJECT_DIR/$required" && ! -L "$PROJECT_DIR/$required" ]] \
    || fail "Обязательный файл обновления отсутствует или небезопасен: $required"
done

cd "$PROJECT_DIR"

printf '%s\n' '1/6 Проверяю синтаксис и обязательные regression-тесты...'
python3 -m py_compile \
  "$PROJECT_DIR/scripts/model_runtime_policy.py" \
  "$PROJECT_DIR/scripts/model_ha_proof.py" \
  "$PROJECT_DIR/scripts/home_assistant_control.py" \
  "$PROJECT_DIR/scripts/capability_catalog.py" \
  "$PROJECT_DIR/scripts/install_verified_lessons.py" \
  "$PROJECT_DIR/tests/evaluate_stage67.py"
for pattern in \
  test_truthful_device_grounding.py \
  test_home_assistant_control.py \
  test_capability_catalog.py \
  test_model_runtime_policy.py \
  test_install_verified_lessons.py; do
  python3 -m unittest discover -s tests -p "$pattern" -v
done
python3 -m unittest discover -s tests -p 'test_*.py' -v

printf '%s\n' '2/6 Останавливаю только разговорные службы Home Butler...'
systemctl stop home-butler-alice-skill.service 2>/dev/null || true
systemctl stop home-butler-local-chat.service 2>/dev/null || true
systemctl stop home-butler.service 2>/dev/null || true

printf '%s\n' '3/6 Разворачиваю обновлённый runtime в staged-режиме...'
HOME_BUTLER_INSTALL_ACTION_TIMERS_MODE=staged \
  "$PROJECT_DIR/scripts/install-home-butler-service.sh"

printf '%s\n' '4/6 Устанавливаю проверенные уроки в ограниченное workspace модели...'
lesson_tmp="$(mktemp -d /tmp/home-butler-stage67.XXXXXX)"
chmod 0755 -- "$lesson_tmp"
cleanup() {
  rm -rf -- "$lesson_tmp"
}
trap cleanup EXIT
install -o root -g root -m 0644 -- \
  "$PROJECT_DIR/scripts/install_verified_lessons.py" \
  "$lesson_tmp/install_verified_lessons.py"
install -o root -g root -m 0644 -- \
  "$PROJECT_DIR/training/stage67_verified_examples.jsonl" \
  "$lesson_tmp/stage67_verified_examples.jsonl"
runuser -u homebutler -- env -i \
  HOME=/home/homebutler \
  PATH=/usr/bin:/bin \
  LC_ALL=C.UTF-8 \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="$RUNTIME_DIR/scripts" \
  HOME_BUTLER_MODEL_WORKSPACE="$LESSON_WORKSPACE" \
  HOME_BUTLER_STAGE67_TRAINING_FILE="$lesson_tmp/stage67_verified_examples.jsonl" \
  /usr/bin/python3 "$lesson_tmp/install_verified_lessons.py"
cleanup
trap - EXIT

printf '%s\n' '5/6 Перезапускаю локальный чат и навык Алисы...'
systemctl restart home-butler-local-chat.service
if systemctl is-enabled --quiet home-butler-alice-skill.service 2>/dev/null; then
  systemctl restart home-butler-alice-skill.service
fi

printf '%s\n' '6/6 Выполняю безопасную read-only проверку...'
python3 "$PROJECT_DIR/tests/evaluate_model.py"
python3 "$PROJECT_DIR/tests/evaluate_stage67.py"
systemctl is-active --quiet home-butler-local-chat.service \
  || fail 'Локальный диалог не запустился.'
if systemctl is-enabled --quiet home-butler-alice-skill.service 2>/dev/null; then
  systemctl is-active --quiet home-butler-alice-skill.service \
    || fail 'Навык Алисы не запустился.'
fi

cat <<'EOF'
Обновление Stage 67 применено.

Первые проверки:
1. «Что с роботом Андреем?»
2. «Сколько у Андрея батареи?»
3. «Что у него с фильтром?»
4. «Верни Андрея на базу.»
5. Для посудомойки проверь двухходовое уточнение: «включи dishwasher» → «Питание».

Слова «готово/включил/запустил» теперь допустимы только после verified readback.
Stateless-кнопка возвращает accepted_unverified и не выдаётся за физический успех.
Проверенные уроки и 20 regression-примеров записаны только в ограниченное workspace модели.
EOF
