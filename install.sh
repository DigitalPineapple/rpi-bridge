#!/bin/sh
set -eu

if [ "$(uname -s)" != Linux ] || [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo on Raspberry Pi OS (Linux)." >&2
    exit 1
fi

cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
for tool in python3 nmcli systemctl ip; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Missing $tool. Install python3, network-manager, and iproute2 first." >&2
        exit 1
    fi
done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11+ required")'
if ! systemctl is-active --quiet NetworkManager; then
    echo "NetworkManager must already manage networking. See README.md." >&2
    exit 1
fi

install -d -m 755 /opt/rpi-bridge /opt/rpi-bridge/web /etc/rpi-bridge
if [ ! -e /etc/rpi-bridge/config.json ]; then
    install -m 644 config.example.json /etc/rpi-bridge/config.json
fi
python3 rpi_bridge.py check-config
install -m 755 rpi_bridge.py /opt/rpi-bridge/rpi_bridge.py
install -m 755 rpi_bridge_web.py /opt/rpi-bridge/rpi_bridge_web.py
install -m 644 web/index.html web/app.js web/style.css /opt/rpi-bridge/web/
rm -f /etc/rpi-bridge/web-token
ln -sfn /opt/rpi-bridge/rpi_bridge.py /usr/local/bin/rpi-bridge
install -m 644 rpi-bridge.service /etc/systemd/system/rpi-bridge.service
install -m 644 rpi-bridge-web.service /etc/systemd/system/rpi-bridge-web.service
systemctl daemon-reload
systemctl enable rpi-bridge.service rpi-bridge-web.service
systemctl restart rpi-bridge.service rpi-bridge-web.service
sleep 1
for service in rpi-bridge.service rpi-bridge-web.service; do
    if ! systemctl is-active --quiet "$service"; then
        journalctl -u "$service" -n 30 --no-pager >&2
        exit 1
    fi
done
echo "Installed. Configure a hub with: sudo rpi-bridge add practice 'FTC-12345'"
echo "Then connect with: sudo rpi-bridge connect practice"
echo "Or open http://PI_IP:8090/ (web_port in config); no login is required."
echo "Anyone on the LAN can manage hubs. Use only on a trusted team network."
