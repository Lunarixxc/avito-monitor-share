#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
umask 077
mkdir -p private
if [[ "${1:-}" != --inside ]]; then
    offline=false
    for arg in "$@"; do [[ "$arg" == --offline-html ]] && offline=true; done
    if ! "$offline"; then bash start-direct.sh; fi
    exec xvfb-run -a -s '-screen 0 1440x1000x24 -nolisten tcp' bash "$0" --inside "$@"
fi
shift
x11vnc -display "$DISPLAY" -localhost -rfbport 5901 -forever -shared -nopw \
    > private/vnc.log 2>&1 &
vnc_pid=$!
websockify --web=/usr/share/novnc/ 127.0.0.1:6080 127.0.0.1:5901 \
    > private/websockify.log 2>&1 &
web_pid=$!
cleanup() {
    kill "$vnc_pid" "$web_pid" 2>/dev/null || true
    wait "$vnc_pid" "$web_pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
echo 'Open http://127.0.0.1:6080/vnc.html?autoconnect=1 after establishing the SSH tunnel.'
echo 'Wait for the Python prompt, then press Enter in the SSH terminal (not inside noVNC).'
# Without --foreground, timeout creates a background process group: input()
# receives SIGTTIN and both Python and timeout stop indefinitely in an SSH TTY.
timeout --foreground --signal=INT --kill-after=20s 3700 \
    .venv/bin/python probe.py --headed --manual "$@"
