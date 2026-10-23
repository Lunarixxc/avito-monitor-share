#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if systemctl is-active --quiet avito-research-direct-v2; then
    exit 0
fi
sudo -n systemd-run --unit=avito-research-direct-v2 --uid=asat \
    --property=RuntimeMaxSec=7200 --property=NoNewPrivileges=yes \
    --property=ProtectSystem=strict --property=ProtectHome=read-only \
    --property='CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW' \
    --property='AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW' \
    --property='RestrictAddressFamilies=AF_INET AF_UNIX' \
    /usr/bin/python3 "$PWD/direct_proxy.py"
for attempt in {1..20}; do
    if (echo > /dev/tcp/127.0.0.1/18081) 2>/dev/null; then exit 0; fi
    sleep 0.2
done
echo 'Direct transport did not start' >&2
exit 1
