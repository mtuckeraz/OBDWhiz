import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from server import create_app


class LanApiTests(unittest.TestCase):
    key = 'test-local-network-key-for-users'

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        handler, cls.adapter, cls.run_async, cls.loop, cls.store = create_app(
            Path(cls.temp.name) / 'api.sqlite3', lan=True, access_key=cls.key)
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.httpd.server_port

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join()
        cls.run_async(cls.adapter.bluetooth.close())
        cls.run_async(cls.adapter.disconnect())
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.store.db.close()
        cls.temp.cleanup()

    def request(self, path, body=None, cookie=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        outgoing = {'X-Apex-Client': '1', 'Content-Type': 'application/json'}
        if cookie:
            outgoing['Cookie'] = cookie
        outgoing.update(headers or {})
        connection.request('POST' if body is not None else 'GET', path,
                           json.dumps(body) if body is not None else None, outgoing)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def login(self):
        result = self.request('/api/login', {'key': self.key})
        self.assertEqual(result[0], 200)
        return result[1]['Set-Cookie'].split(';', 1)[0]

    def test_private_api_requires_login_including_bluetooth_operations(self):
        self.assertEqual(self.request('/')[0], 200)
        self.assertFalse(json.loads(self.request('/api/session')[2])['authenticated'])
        for path, body in [('/api/status', None), ('/api/scans', None), ('/api/bluetooth', None),
                           ('/api/fleet', None), ('/api/fleet/events', None), ('/api/fleet/settings', {'enabled': False}),
                           ('/api/bluetooth/pair', {'id': 'address'}),
                           ('/api/bluetooth/answer', {'promptId': 'fake', 'value': '1234'}),
                           ('/api/connect', {}), ('/api/settings', {})]:
            with self.subTest(path=path):
                self.assertEqual(self.request(path, body)[0], 401)

    def test_login_cookie_security_and_logout(self):
        result = self.request('/api/login', {'key': self.key})
        self.assertNotIn(self.key.encode(), result[2])
        self.assertIn('HttpOnly', result[1]['Set-Cookie'])
        self.assertIn('SameSite=Strict', result[1]['Set-Cookie'])
        cookie = result[1]['Set-Cookie'].split(';', 1)[0]
        status = self.request('/api/status', cookie=cookie)
        self.assertEqual(status[0], 200)
        self.assertFalse(json.loads(status[2])['localOnly'])
        self.assertEqual(self.request('/api/logout', {}, cookie)[0], 200)
        self.assertEqual(self.request('/api/status', cookie=cookie)[0], 401)

    def test_lan_hostname_works_but_rebinding_and_cross_origin_stay_blocked(self):
        cookie = self.login()
        host = f'{socket.gethostname().lower()}:{self.port}'
        self.assertEqual(self.request('/api/status', cookie=cookie, headers={'Host': host})[0], 200)
        self.assertEqual(self.request('/api/status', cookie=cookie, headers={'Host': 'attacker.example'})[0], 403)
        self.assertEqual(self.request('/api/login', {'key': self.key}, headers={'Origin': 'https://attacker.example'})[0], 403)
        self.assertEqual(self.request('/api/demo', {}, cookie, {'X-Apex-Client': ''})[0], 403)

    def test_authenticated_pairing_and_answers_reach_the_pi_bluetooth_manager(self):
        cookie = self.login()
        manager = self.adapter.bluetooth
        with patch.object(manager, 'start_pair', new=AsyncMock(return_value={'status': 'pairing'})) as pair:
            self.assertEqual(self.request('/api/bluetooth/pair', {'id': 'AA:BB:CC:DD:EE:FF'}, cookie)[0], 200)
            pair.assert_awaited_once_with('AA:BB:CC:DD:EE:FF')
        with patch.object(manager, 'answer', new=AsyncMock(return_value={'accepted': True})) as answer:
            self.assertEqual(self.request('/api/bluetooth/answer', {'promptId': 'request', 'value': '1234'}, cookie)[0], 200)
            answer.assert_awaited_once_with('request', '1234')
