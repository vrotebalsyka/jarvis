#!/usr/bin/env bash
set -uo pipefail

readonly LISTEN_ADDRESS="192.168.1.175"
readonly LISTEN_PORT="8780"
readonly BACKEND_PORT="8781"
readonly NETSH="/mnt/c/Windows/System32/netsh.exe"

if (( EUID != 0 )); then
  printf '%s\n' 'Home Butler LAN forwarding must run as Ubuntu root.' >&2
  exit 2
fi

wsl_address="$(
  ip -4 route get 192.168.1.127 \
    | awk '{for (i = 1; i <= NF; i++) if ($i == "src") {print $(i + 1); exit}}'
)"
if [[ ! "$wsl_address" =~ ^172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
  printf '%s\n' 'Home Butler refused an unsafe Ubuntu forwarding address.' >&2
  exit 2
fi

"$NETSH" interface portproxy delete v4tov4 \
  listenaddress="$LISTEN_ADDRESS" listenport="$LISTEN_PORT" >/dev/null 2>&1 || true
"$NETSH" interface portproxy add v4tov4 \
  listenaddress="$LISTEN_ADDRESS" listenport="$LISTEN_PORT" \
  connectaddress="$wsl_address" connectport="$BACKEND_PORT" protocol=tcp >/dev/null 2>&1 || true

rule_dump="$("$NETSH" interface portproxy dump 2>/dev/null | tr -d '\r')"
if [[ "$rule_dump" != *"listenport=$LISTEN_PORT"* \
  || "$rule_dump" != *"connectaddress=$wsl_address"* \
  || "$rule_dump" != *"connectport=$BACKEND_PORT"* ]]; then
  logger -t home-butler-lan-forward 'LAN forwarding verification failed.'
  exit 1
fi

logger -t home-butler-lan-forward "LAN forwarding ready at $wsl_address."
printf 'home_butler_lan_forward=ready backend=%s\n' "$wsl_address"
