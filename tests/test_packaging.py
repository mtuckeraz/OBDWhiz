import hashlib
from pathlib import Path
import tarfile
import tempfile
import unittest

from tools.build_release import build, FILES
from tools.render_service import quoted, render


class PackagingTests(unittest.TestCase):
    def test_release_excludes_private_data_and_preserves_linux_script_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in FILES:
                (root / name).write_bytes(b'public source\r\n')
            (root / 'VERSION').write_text('0.2.0\n')
            for name in ('core', 'web', 'tools', 'tests', '.local', '.venv', 'output'):
                (root / name).mkdir()
            for name in ('.env', '.local/apex.sqlite3', '.venv/private.py', 'output/old.tar.gz', 'web/screenshot.png'):
                (root / name).write_bytes(b'PRIVATE TEST DATA')
            (root / 'core/adapter.py').write_bytes(b'public source')
            (root / 'web/index.html').write_bytes(b'public source')
            (root / 'core/__pycache__').mkdir()
            (root / 'core/__pycache__/ignored.py').write_bytes(b'PRIVATE TEST DATA')
            archive = build(root)
            with tarfile.open(archive) as bundle:
                names = bundle.getnames()
                self.assertIn('obdwhiz/core/adapter.py', names)
                self.assertIn('obdwhiz/.env.example', names)
                for name in names:
                    member = bundle.getmember(name)
                    self.assertNotIn(b'PRIVATE TEST DATA', bundle.extractfile(member).read())
                    self.assertFalse(member.issym() or member.islnk())
                script = bundle.getmember('obdwhiz/install-pi.sh')
                self.assertEqual(script.mode, 0o755)
                self.assertNotIn(b'\r', bundle.extractfile(script).read())
            checksum = archive.with_name(archive.name + '.sha256').read_text().split()[0]
            self.assertEqual(checksum, hashlib.sha256(archive.read_bytes()).hexdigest())

    def test_systemd_paths_are_quoted_without_expanding_dollars_or_specifiers(self):
        self.assertEqual(quoted('/home/pi/My "OBD" % $app', executable=True),
                         '"/home/pi/My \\"OBD\\" %% $$app"')
        unit = render('.', 'pi', 'pi')
        self.assertIn('User=pi\n', unit)
        self.assertIn('SupplementaryGroups=bluetooth dialout', unit)
        self.assertIn('KillSignal=SIGINT', unit)
        self.assertNotIn('User=root', unit)
        with self.assertRaises(ValueError):
            render('.', 'pi\nUser=root', 'pi')
        with self.assertRaises(ValueError):
            quoted('/home/pi\nExecStart=bad')


if __name__ == '__main__':
    unittest.main()
