"""Use systemd's real parser on Linux; never install or start test services."""
import getpass
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tools.render_service import render


@unittest.skipUnless(sys.platform == 'linux' and shutil.which('systemd-analyze'),
                     'systemd unit validation requires Linux with systemd-analyze')
class SystemdTests(unittest.TestCase):
    def test_generated_unit_passes_systemd_and_quoted_working_directory_is_rejected(self):
        import grp

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            app = directory / 'OBD Whiz % test'
            (app / '.venv/bin').mkdir(parents=True)
            (app / '.venv/bin/python').symlink_to(Path(sys.executable).resolve())
            (app / 'server.py').write_text('# Validation fixture; never executed.\n')
            # Supply the dependency for hosts without a physical Bluetooth stack.
            (directory / 'bluetooth.service').write_text(
                '[Unit]\nDescription=Bluetooth validation fixture\n'
                f'[Service]\nType=oneshot\nExecStart={shutil.which("true")}\n')
            unit = directory / 'obdwhiz.service'
            content = render(str(app), getpass.getuser(), grp.getgrgid(os.getgid()).gr_name)
            environment = dict(os.environ, SYSTEMD_UNIT_PATH=f'{directory}:')

            def verify(text):
                unit.write_text(text, encoding='utf-8')
                return subprocess.run(['systemd-analyze', 'verify', '--man=no', str(unit)],
                                      env=environment, capture_output=True, text=True, timeout=30)

            correct = verify(content)
            self.assertEqual(correct.returncode, 0, correct.stdout + correct.stderr)
            # Reproduce the original defect, ensuring this check catches it.
            broken = content.replace(f'WorkingDirectory={str(app).replace("%", "%%")}\n',
                                     f'WorkingDirectory="{str(app).replace("%", "%%")}"\n')
            invalid = verify(broken)
            self.assertNotEqual(invalid.returncode, 0, invalid.stdout + invalid.stderr)
            self.assertIn('WorkingDirectory', invalid.stderr)


if __name__ == '__main__':
    unittest.main()
