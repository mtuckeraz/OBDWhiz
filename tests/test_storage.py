from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from core.storage import Storage
from core.store import DATABASE_FILENAME


class StorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.storage = Storage(root, {})
        self.local = root / '.local'
        self.local.mkdir()
        self.external = root / 'external'
        self.external.mkdir()
        self.drives = [
            {'id': 'system', 'root': str(self.local), 'label': 'System'},
            {'id': 'usb', 'root': str(self.external), 'label': 'USB'},
        ]
        self.storage.volumes = lambda: self.drives.copy()
        self.database = self.local / DATABASE_FILENAME
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE scans (id TEXT)')
            db.execute("INSERT INTO scans VALUES ('existing-scan')")
        (self.local / 'media').mkdir()
        (self.local / 'media' / 'photo').write_bytes(b'photo')
        (self.local / 'diagnostics').mkdir()
        (self.local / 'diagnostics' / 'scan.json').write_text('{}', encoding='utf-8')

    def test_move_keeps_original_and_requires_selected_drive_on_restart(self):
        self.storage.request('usb')
        self.assertTrue(self.storage.overview()['pending'])
        with patch('core.storage.os.sync', create=True):
            new, media = self.storage.prepare()
        self.assertEqual(new.parent, self.external)
        self.assertTrue(self.database.exists())
        self.assertEqual((media / 'photo').read_bytes(), b'photo')
        self.assertTrue((new / 'diagnostics' / 'scan.json').exists())
        with closing(sqlite3.connect(new / DATABASE_FILENAME)) as db:
            self.assertEqual(db.execute('SELECT id FROM scans').fetchone(), ('existing-scan',))
        self.assertFalse(self.storage.overview()['pending'])
        self.drives.pop()
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            self.storage.prepare()

    def test_missing_destination_leaves_original_storage_active(self):
        self.storage.request('usb')
        self.drives.pop()
        self.assertEqual(self.storage.prepare(), (self.local, self.local / 'media'))
        self.assertIn('original data is still in use', self.storage.overview()['error'])
