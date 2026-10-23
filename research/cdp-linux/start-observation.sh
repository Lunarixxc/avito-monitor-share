#!/usr/bin/env bash
# Start an empty manual browser, never navigate to Avito from this script.
set -euo pipefail
cd "$(dirname "$0")"
umask 077
for unit in avito-linux-manual avito-research-direct-v2 avito-observation-direct; do
    if systemctl is-active --quiet "$unit"; then
        echo "Existing research session active: $unit" >&2
        exit 1
    fi
done
python3 - <<'PY'
import socket
for port in (18081, 9223, 5901, 6080):
    with socket.socket() as s:
        s.bind(('127.0.0.1', port))
PY
mkdir -p results/manual-observation
mkdir results/manual-observation/start.claim
cp -a private/manual-profile private/manual-profile-before-observation
sudo -n systemd-run --unit=avito-observation-direct --uid=asat \
    --property=RuntimeMaxSec=7200 --property=NoNewPrivileges=yes \
    --property=ProtectSystem=strict --property=ProtectHome=read-only \
    --property='CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW' \
    --property='AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW' \
    --property='RestrictAddressFamilies=AF_INET AF_UNIX' \
    /usr/bin/python3 "$PWD/diagnostic_proxy.py"
cleanup_failure() {
    sudo -n systemctl stop avito-linux-manual avito-observation-direct || true
}
trap cleanup_failure ERR
python3 - <<'PY'
import socket, time
for attempt in range(25):
    try:
        with socket.create_connection(('127.0.0.1',18081), timeout=1):
            break
    except OSError:
        time.sleep(0.2)
else:
    raise SystemExit('Transport did not start')
PY
sudo -n systemd-run --unit=avito-linux-manual --uid=asat \
    --property=RuntimeMaxSec=7200 --property=TimeoutStopSec=20 \
    --property=BindsTo=avito-observation-direct.service \
    --property=After=avito-observation-direct.service \
    /bin/bash "$PWD/manual-browser.sh"
echo 'Manual browser starting on about:blank; CDP reader not attached.'
