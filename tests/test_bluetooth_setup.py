import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import check_bluetooth


class BluetoothSetupTests(unittest.TestCase):
    def check_linux(self, controller='Controller AA:BB:CC:DD:EE:FF Pi\n\tPowered: yes\n', version='bluetoothctl: 5.66', scan=False):
        responses = {('bluetoothctl', '--version'): version,
                     ('systemctl', 'is-active', 'bluetooth.service'): 'active\n',
                     ('bluetoothctl', 'show'): controller}
        with patch.object(check_bluetooth.sys, 'platform', 'linux'), \
                patch.object(check_bluetooth.importlib, 'import_module'), \
                patch.object(check_bluetooth.shutil, 'which', return_value='/usr/bin/tool'), \
                patch.object(check_bluetooth, 'command', side_effect=lambda *args: responses[args]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            check_bluetooth.check(scan)
            return output.getvalue()

    def test_powered_controller_passes_without_exposing_its_address(self):
        output = self.check_linux()
        self.assertIn('powered on', output)
        self.assertNotIn('AA:BB', output)

    def test_unpowered_or_absent_controller_has_actionable_error(self):
        with self.assertRaisesRegex(RuntimeError, 'rfkill unblock'):
            self.check_linux(controller='Controller AA Pi\n\tPowered: no\n')
        with self.assertRaisesRegex(RuntimeError, 'No Bluetooth controller'):
            self.check_linux(controller='')

    def test_old_bluez_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'BlueZ 5.55'):
            self.check_linux(version='bluetoothctl: 5.50')

    def test_missing_python_dependency_points_to_installer(self):
        with patch.object(check_bluetooth.importlib, 'import_module', side_effect=ImportError):
            with self.assertRaisesRegex(RuntimeError, 'install-pi.sh'):
                check_bluetooth.check()

    def test_failed_system_command_does_not_look_like_a_healthy_controller(self):
        with patch.object(check_bluetooth.subprocess, 'run', return_value=SimpleNamespace(
                returncode=1, stdout='', stderr='D-Bus access denied')):
            with self.assertRaisesRegex(RuntimeError, 'D-Bus access denied'):
                check_bluetooth.command('bluetoothctl', 'show')


if __name__ == '__main__':
    unittest.main()
