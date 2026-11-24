import tempfile
from pathlib import Path
import time
import unittest

from core.access import BrowserAccess, load_access_key


class AccessTests(unittest.TestCase):
    def test_key_is_generated_once_and_survives_reinstall(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'private/access.key'
            first = load_access_key(path)
            self.assertGreaterEqual(len(first), 20)
            self.assertEqual(first, load_access_key(path))

    def test_session_cookie_expires_and_can_be_revoked(self):
        access = BrowserAccess('test-key-for-this-installation')
        self.assertIsNone(access.login('incorrect', 'client'))
        token = access.login('test-key-for-this-installation', 'client')
        cookie = f'obdwhiz_session={token}'
        self.assertTrue(access.allowed(cookie))
        self.assertFalse(access.allowed('obdwhiz_session=forged'))
        access.logout(cookie)
        self.assertFalse(access.allowed(cookie))
        token = access.login('test-key-for-this-installation', 'client')
        access.sessions[token] = time.monotonic() - 1
        self.assertFalse(access.allowed(f'obdwhiz_session={token}'))

    def test_repeated_bad_keys_are_throttled(self):
        access = BrowserAccess('the-real-key')
        for _ in range(5):
            self.assertIsNone(access.login('wrong', 'client'))
        with self.assertRaisesRegex(ValueError, 'Too many'):
            access.login('the-real-key', 'client')
        self.assertIsNotNone(access.login('the-real-key', 'different-client'))
