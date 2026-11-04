#!/bin/bash
# Installs LabMap as a system service on port 80. Only the LAN cable, Tailscale and the server itself may connect.
# Run from the repository: sudo bash install.sh
# Safe to run again, e.g. after changing labmap.env - it replaces the service and restarts it.
set -euo pipefail

main() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "Run this script with sudo: sudo bash $0"
    exit 1
  fi
  local dir user lan subnet
  dir=$(cd "$(dirname "$0")" && pwd)
  user=${SUDO_USER:-root}

  echo "1/4 Settings"
  if [ ! -f /etc/labmap.env ]; then
    install -m 644 "$dir/labmap.env.example" /etc/labmap.env
    echo "    created /etc/labmap.env – edit it if your interfaces or RAID device have other names"
  fi
  # shellcheck disable=SC1091
  set -a; . /etc/labmap.env; set +a
  lan=${LABMAP_LAN_INTERFACE:-eth0}
  [ -f "$dir/services.json" ] || install -m 644 -o "$user" "$dir/services.example.json" "$dir/services.json"

  echo "2/4 Service"
  sed -e "s|@USER@|$user|g" -e "s|@CONFIG@|$dir|g" "$dir/labmap.service" > /etc/systemd/system/labmap.service
  subnet=$(ip -j -4 addr show dev "$lan" 2>/dev/null | python3 -c \
    'import ipaddress, json, sys; a = json.load(sys.stdin)[0]["addr_info"][0]; print(ipaddress.ip_network(a["local"] + "/" + str(a["prefixlen"]), strict=False))' \
    2>/dev/null || true)
  install -d /etc/systemd/system/labmap.service.d
  printf '[Service]\nIPAddressAllow=localhost %s 100.64.0.0/10 fd7a:115c:a1e0::/48\n' "$subnet" \
    > /etc/systemd/system/labmap.service.d/10-allow.conf
  [ -n "$subnet" ] || echo "    no address on $lan – only Tailscale and localhost may connect for now"

  echo "3/4 Firewall"
  if [[ $(ufw status 2>/dev/null || true) == *"Status: active"* ]]; then
    ufw allow in on "$lan" to any port 80 proto tcp comment 'labmap (LAN)'
    ufw allow in on tailscale0 to any port 80 proto tcp comment 'labmap (Tailscale)'
  else
    echo "    ufw is not active – skipped"
  fi

  echo "4/4 Start"
  rm -f /var/lib/labmap/labmap.py  # a fresh copy from $dir
  systemctl daemon-reload
  systemctl enable labmap.service
  systemctl restart labmap.service || true  # the check below explains a failure
  sleep 3
  if systemctl is-active --quiet labmap.service; then
    echo "Done: open http://$(hostname)/ over the LAN cable or Tailscale"
  else
    echo "LabMap didn't start. The last log lines:"
    journalctl -u labmap.service -n 20 --no-pager
    exit 1
  fi
}

main "$@"; exit
