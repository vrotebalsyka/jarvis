#!/usr/bin/env bash
set -euo pipefail

readonly URL_FILE="/root/Jarvis/home-butler/secrets/alice-webhook-url.txt"
readonly ORIGIN_FILE="/root/Jarvis/home-butler/secrets/alice-public-origin.txt"
readonly CLIP_EXE="/mnt/c/Windows/System32/clip.exe"

fail() {
  printf '%s\n' "$1" >&2
  exit 2
}

[[ "${EUID:-$(id -u)}" == 0 ]] || fail 'Run this helper as root.'
[[ -f "$URL_FILE" && ! -L "$URL_FILE" ]] \
  || fail 'The private Alice Webhook file is unavailable.'
[[ -f "$ORIGIN_FILE" && ! -L "$ORIGIN_FILE" ]] \
  || fail 'The private Alice public origin file is unavailable.'
[[ "$(stat -c '%F:%u:%g:%a:%h:%s' -- "$URL_FILE")" =~ \
  ^regular\ file:0:0:600:1:[1-9][0-9]{1,2}$ ]] \
  || fail 'The private Alice Webhook file metadata is unsafe.'
[[ "$(stat -c '%F:%u:%g:%a:%h:%s' -- "$ORIGIN_FILE")" =~ \
  ^regular\ file:0:0:600:1:[1-9][0-9]{1,2}$ ]] \
  || fail 'The private Alice public origin metadata is unsafe.'
[[ -x "$CLIP_EXE" ]] || fail 'Windows clipboard integration is unavailable.'

IFS= read -r origin < "$ORIGIN_FILE" \
  || fail 'The private Alice public origin is unreadable.'
[[ "$origin" =~ ^https://[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+\.ts\.net$ ]] \
  || fail 'The private Alice public origin is unexpected.'
expected_prefix="$origin/alice/"
IFS= read -r url < "$URL_FILE" || fail 'The private Alice Webhook is unreadable.'
[[ "$url" == "$expected_prefix"* ]] \
  || fail 'The private Alice Webhook origin is unexpected.'
secret="${url#"$expected_prefix"}"
[[ "$secret" =~ ^[A-Za-z0-9_-]{32,128}$ ]] \
  || fail 'The private Alice Webhook credential is malformed.'

printf '%s' "$url" | "$CLIP_EXE"
unset origin expected_prefix url secret
printf '%s\n' 'Новый Webhook Алисы скопирован в буфер обмена без вывода на экран.'
