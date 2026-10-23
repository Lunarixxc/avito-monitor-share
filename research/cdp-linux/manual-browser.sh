#!/usr/bin/env bash
# Invoked in a transient systemd service; no Playwright and no Avito navigation.
set -euo pipefail
cd "$(dirname "$0")"
umask 077
mkdir -p private/manual-profile
if [[ "${1:-}" != --inside ]]; then
    exec xvfb-run -a -s '-screen 0 1440x1000x24 -nolisten tcp' bash "$0" --inside
fi

children=()
cleanup() {
    for pid in "${children[@]}"; do kill "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

x11vnc -display "$DISPLAY" -localhost -rfbport 5901 -forever -shared -nopw \
    > private/manual-vnc.log 2>&1 &
children+=("$!")
websockify --web=/usr/share/novnc/ 127.0.0.1:6080 127.0.0.1:5901 \
    > private/manual-web.log 2>&1 &
children+=("$!")
/opt/google/chrome/chrome \
    --user-data-dir="$PWD/private/manual-profile" \
    --remote-debugging-address=127.0.0.1 --remote-debugging-port=9223 \
    --proxy-server=http://127.0.0.1:18081 \
    --no-first-run --no-default-browser-check about:blank \
    > /dev/null 2>&1 &
children+=("$!")
# Exit if any required component terminates; systemd cleans up the whole cgroup.
wait -n "${children[@]}"
