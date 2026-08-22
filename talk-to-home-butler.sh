#!/usr/bin/env bash
set -euo pipefail

readonly PROJECT_DIR="/root/Jarvis/home-butler"
readonly ENDPOINT_GUARD="$PROJECT_DIR/scripts/ollama_endpoint.py"
readonly OWNER_CHAT="$PROJECT_DIR/scripts/owner_chat.py"

if (( EUID != 0 )); then
  printf '%s\n' 'Запустите эту команду из Ubuntu-терминала пользователя root.' >&2
  exit 2
fi
if [[ ! -x "$OWNER_CHAT" || ! -x "$ENDPOINT_GUARD" ]]; then
  printf '%s\n' 'Home Butler установлен не полностью.' >&2
  exit 2
fi

cd "$PROJECT_DIR"
export HOME_BUTLER_OLLAMA_BASE_URL
HOME_BUTLER_OLLAMA_BASE_URL="$(python3 "$ENDPOINT_GUARD")"

if (( $# == 0 )); then
  if [[ ! -t 0 || ! -t 1 ]]; then
    printf '%s\n' 'Для диалога нужен обычный интерактивный Ubuntu-терминал.' >&2
    exit 2
  fi
  exec python3 "$OWNER_CHAT"
fi

if (( $# == 2 )) && [[ "$1" == "--oneshot" && -n "$2" ]]; then
  exec python3 "$OWNER_CHAT" --oneshot "$2"
fi

printf '%s\n' 'Использование: ./talk-to-home-butler.sh [--oneshot "вопрос"]' >&2
exit 2
