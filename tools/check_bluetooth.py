"""Check the Pi Bluetooth stack as the app user, with an optional BLE scan."""
import argparse
import asyncio
import importlib
import re
import shutil
import subprocess
import sys


def command(*args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f'{args[0]} failed: {exc}') from exc
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed: {(result.stderr or result.stdout).strip()}')
    return result.stdout


def check(scan=False):
    if sys.version_info < (3, 11):
        raise RuntimeError('Python 3.11+ is required.')
    for name in ('bleak', 'serial'):
        try:
            importlib.import_module(name)
        except ImportError as exc:
            raise RuntimeError('Python Bluetooth dependencies are missing. Run bash install-pi.sh.') from exc
    print('OK: Bleak and pySerial are installed.')
    if sys.platform == 'linux':
        for name in ('bluetoothctl', 'systemctl', 'rfkill'):
            if not shutil.which(name):
                raise RuntimeError(f'{name} is missing. Run bash install-pi.sh.')
        version = command('bluetoothctl', '--version').strip()
        match = re.search(r'(\d+)\.(\d+)', version)
        if not match or tuple(map(int, match.groups())) < (5, 55):
            raise RuntimeError('BlueZ 5.55+ is required.')
        command('systemctl', 'is-active', 'bluetooth.service')
        controller = command('bluetoothctl', 'show')
        if not re.search(r'^\s*Controller\s', controller, re.M):
            raise RuntimeError('No Bluetooth controller found. Check Pi Bluetooth configuration or your USB adapter.')
        if not re.search(r'^\s*Powered:\s+yes\s*$', controller, re.M):
            raise RuntimeError('Bluetooth is off. Run sudo rfkill unblock bluetooth, then bluetoothctl power on.')
        print(f'OK: {version}; Bluetooth service and controller are powered on.')
    if scan:
        from bleak import BleakScanner
        devices = asyncio.run(BleakScanner.discover(timeout=6))
        print(f'OK: BLE discovery completed ({len(devices)} devices; addresses omitted).')
        if not devices:
            print('No advertisements found. Check ignition, adapter power, and range; close other apps using the adapter.')
    else:
        print('Run with --scan to test BLE discovery as this user.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scan', action='store_true', help='Also perform a six-second BLE scan')
    args = parser.parse_args()
    try:
        check(args.scan)
    except Exception as exc:
        print(f'Bluetooth check failed: {exc}', file=sys.stderr)
        if sys.platform == 'linux':
            print('If access was denied, log out/in after installation so the bluetooth group applies. See README.md.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
