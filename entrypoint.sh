#!/bin/bash
# Роль контейнера — по первому аргументу (command в compose), образ один (SPEC §7).
set -e

case "$1" in
  coordinator)
    exec python -m parser_ebay.coordinator
    ;;
  worker)
    # свой Xvfb на контейнер (контейнеры изолированы — :99 у каждого). Чистим
    # stale-lock от прошлого Xvfb: контейнер рестартует тот же /tmp, и после
    # смерти воркера (Docker убил Xvfb) новый Xvfb иначе не займёт :99 → крашлуп.
    rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
    Xvfb :99 -screen 0 1920x1080x24 >/dev/null 2>&1 &
    export DISPLAY=:99
    # ждём готовности дисплея, а не фиксированный sleep
    for _ in $(seq 1 20); do [ -e /tmp/.X11-unix/X99 ] && break; sleep 0.3; done
    # exec делает python PID 1, чтобы SIGTERM шёл прямо воркеру (дренаж, SPEC §6)
    exec python -m parser_ebay.worker
    ;;
  *)
    echo "usage: entrypoint.sh {coordinator|worker}" >&2
    exit 1
    ;;
esac
