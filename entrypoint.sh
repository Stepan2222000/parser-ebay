#!/bin/bash
# Роль контейнера — по первому аргументу (command в compose), образ один (SPEC §7).
set -e

case "$1" in
  coordinator)
    exec python -m parser_ebay.coordinator
    ;;
  worker)
    # свой Xvfb на контейнер (контейнеры изолированы — :99 у каждого); exec делает
    # python PID 1, чтобы SIGTERM шёл прямо воркеру (штатный дренаж, SPEC §6)
    Xvfb :99 -screen 0 1920x1080x24 >/dev/null 2>&1 &
    export DISPLAY=:99
    sleep 1
    exec python -m parser_ebay.worker
    ;;
  *)
    echo "usage: entrypoint.sh {coordinator|worker}" >&2
    exit 1
    ;;
esac
