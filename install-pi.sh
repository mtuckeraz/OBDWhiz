#!/usr/bin/env bash
# Run as your normal Pi user: bash install-pi.sh [--service]
set -euo pipefail

service=false
case "${1:-}" in
  '') ;;
  --service) service=true ;;
  -h|--help)
    printf 'Usage: bash install-pi.sh [--service]\nInstalls Python and Bluetooth dependencies. --service also starts OBDWhiz at boot.\n'
    exit 0 ;;
  *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || { printf 'Too many arguments.\n' >&2; exit 2; }
[[ "$(uname -s)" == Linux && -f /etc/debian_version ]] || {
  printf 'Use Raspberry Pi OS Bookworm or newer (Debian-based Linux).\n' >&2; exit 1;
}
[[ $EUID -ne 0 ]] || {
  printf 'Run as your normal user, without sudo. The installer requests sudo for system setup only.\n' >&2; exit 1;
}
command -v sudo >/dev/null || { printf 'sudo is required for system package installation.\n' >&2; exit 1; }
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd -- "$project_dir"
if [[ -d .venv && ! -x .venv/bin/python ]]; then
  printf 'The existing .venv is not a Linux environment. Extract a clean release on the Pi; do not copy a Windows .venv.\n' >&2
  exit 1
fi

printf 'Installing Python, BlueZ, D-Bus, Bluetooth tools, and CA certificates...\n'
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip bluez bluetooth dbus rfkill ca-certificates avahi-daemon
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11+ is required. Use Raspberry Pi OS Bookworm or newer.")'
python3 - "$(bluetoothctl --version)" <<'PY'
import re
import sys
match = re.search(r'(\d+)\.(\d+)', sys.argv[1])
if not match or tuple(map(int, match.groups())) < (5, 55):
    sys.exit('BlueZ 5.55+ is required. Upgrade Raspberry Pi OS before installing.')
PY
sudo usermod -aG bluetooth,dialout "$(id -un)"
sudo systemctl enable --now bluetooth.service
sudo systemctl enable --now avahi-daemon.service
sudo rfkill unblock bluetooth
if ! bluetoothctl power on; then
  printf 'Bluetooth is not on yet. Use the web interface to turn it on after the service starts.\n' >&2
fi

# A venv avoids changing Raspberry Pi OS's managed Python environment.
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -c 'import bleak, serial; import bleak.backends.bluezdbus.client'
chmod +x Start-OBDWhiz.sh install-pi.sh

if "$service"; then
  unit_dir="$(mktemp -d)"
  unit_file="$unit_dir/obdwhiz.service"
  trap 'rm -f -- "$unit_file"; rmdir -- "$unit_dir"' EXIT
  .venv/bin/python tools/render_service.py "$project_dir" "$(id -un)" "$(id -gn)" > "$unit_file"
  # Verify the actual generated unit before replacing an existing service.
  sudo systemd-analyze verify "$unit_file"
  sudo install -m 0644 "$unit_file" /etc/systemd/system/obdwhiz.service
  sudo systemctl daemon-reload
  sudo systemctl enable obdwhiz.service
  sudo systemctl restart obdwhiz.service
  sudo systemctl --no-pager --full status obdwhiz.service
fi

if ! .venv/bin/python tools/check_bluetooth.py; then
  printf '\nInstallation finished, but Bluetooth needs attention; see the check above.\n' >&2
fi
printf '\nOBDWhiz installed.\n'
if "$service"; then
  printf 'The boot service is running with the new groups. Open http://%s.local:8787 from your phone or laptop.\n' "$(hostname -s)"
else
  printf 'Log out/in before using the manual launcher so device groups take effect.\n'
  printf 'Then run: bash Start-OBDWhiz.sh --lan\n'
fi
printf '\nSign in to OBDWhiz with your username and password. New installations require a password change at first sign-in.\n'
printf 'Bluetooth scan check: .venv/bin/python tools/check_bluetooth.py --scan\n'
printf 'After installation, Bluetooth power, discovery, pairing and PIN entry are available in the web interface.\n'
